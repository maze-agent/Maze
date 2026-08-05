import asyncio
from pathlib import Path

from maze.client.maze import dynamic as client_dynamic
from maze.core import server as core_server
from maze.core.path.path import MaPath
from maze.core.workflow.dynamic import DynamicRun, DynamicTaskSpec


def test_local_model_test_bypasses_instance_routing(monkeypatch):
    captured = {}

    class FakeTask:
        def __init__(self, _workflow_id, task_id, _name):
            captured["task_id"] = task_id

        def save_task(self, **kwargs):
            captured["task"] = kwargs

    class FakeWorkflow:
        def add_task(self, _task_id, _task):
            pass

    class FakePath:
        def __init__(self):
            self.workflows = {}
            self.async_que = {}

        async def get_cluster_resources(self):
            return {
                "head_node_id": "head-node",
                "nodes": [{
                    "node_id": "head-node",
                    "resources": {"gpu": {"devices": [{"total_count": 1}]}},
                }],
            }

        def create_workflow(self, workflow_id):
            self.workflows[workflow_id] = FakeWorkflow()

        def get_workflow(self, workflow_id):
            return self.workflows[workflow_id]

        def run_workflow(self, *_args, **_kwargs):
            return "model-test-run"

    async def completed_run(_run_id, _timeout_seconds):
        return {
            "status": "succeeded",
            "task_nodes": {
                captured["task_id"]: {
                    "status": "succeeded",
                    "result_summary": {
                        "device": "cuda:0",
                        "load_seconds": 1.0,
                        "generated_text": "OK",
                    },
                },
            },
        }

    monkeypatch.setattr(core_server, "mapath", FakePath())
    monkeypatch.setattr(core_server, "CodeTask", FakeTask)
    monkeypatch.setattr(core_server, "_wait_for_model_test_run", completed_run)

    result = asyncio.run(core_server._run_model_test_task(
        {
            "id": "Qwen2.5-3B-Instruct",
            "name": "Qwen2.5-3B-Instruct",
            "estimated_gpu_mem_mb": 7064,
        },
        Path("/models/Qwen2.5-3B-Instruct"),
        [],
    ))

    assert "model_anchor" not in captured["task"]
    assert captured["task"]["resources"] == {
        "cpu_num": 1,
        "gpu_mem": 7064,
        "io_num": 0,
        "target_node_id": "head-node",
    }
    assert result["ok"] is True


def test_runtime_model_anchor_flows_from_client_to_dynamic_task(monkeypatch):
    run_id = "dynamic-run"
    core_run = DynamicRun(run_id)
    core_spec = DynamicTaskSpec(
        task_spec_id="inference",
        task_name="inference",
        code_str="def inference(): return {'ok': True}",
        code_ser=None,
    )
    io_spec = DynamicTaskSpec(
        task_spec_id="io",
        task_name="io",
        code_str="def io(): return {'ok': True}",
        code_ser=None,
        task_kind="io",
        resources={"io_num": 1},
    )
    core_run.register_task_spec(core_spec)
    core_run.register_task_spec(io_spec)

    path = object.__new__(MaPath)
    path.dynamic_runs = {run_id: core_run}
    path._require_scheduler_available = lambda: None
    path._refresh_dynamic_timeout = lambda _run_id: asyncio.sleep(0)
    submitted = []
    path._submit_dynamic_task = submitted.append
    events = []

    async def capture_event(_run_id, event):
        events.append(event)

    path._emit_dynamic_event = capture_event
    monkeypatch.setattr(core_server, "mapath", path)

    payloads = []

    class Request:
        def __init__(self, payload):
            self.payload = payload

        async def json(self):
            return self.payload

    class Response:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def post(_url, json):
        payloads.append(json)
        return Response(asyncio.run(core_server.append_dynamic_task(run_id, Request(json))))

    monkeypatch.setattr(client_dynamic.requests, "post", post)
    client_run = client_dynamic.DynamicRun(run_id, "http://maze")
    client_spec = client_dynamic.DynamicTaskSpec(
        client_run,
        core_spec.task_spec_id,
        core_spec.task_name,
        ["ok"],
    )
    client_io_spec = client_dynamic.DynamicTaskSpec(
        client_run,
        io_spec.task_spec_id,
        io_spec.task_name,
        ["ok"],
    )
    anchor = {"local_model": "qwen", "estimated_gpu_mem_mb": 2048}

    model_invocation = client_run.append_task(client_spec, model_anchor=anchor)
    default_invocation = client_run.append_task(client_spec)
    io_invocation = client_run.append_task(
        client_io_spec,
        model_anchor={"instance_id": "existing-instance"},
    )

    model_task = core_run.tasks[model_invocation.task_id]
    default_task = core_run.tasks[default_invocation.task_id]
    io_task = core_run.tasks[io_invocation.task_id]
    assert payloads[0]["model_anchor"] == anchor
    assert "model_anchor" not in payloads[1]
    assert model_task.task_kind == "gpu"
    assert model_task.resources["gpu_mem"] == 2048
    assert model_task.model_anchor == anchor
    assert default_task.task_kind == "cpu"
    assert default_task.model_anchor is None
    assert io_task.task_kind == "io"
    assert core_spec.model_anchor is None
    assert submitted[0].to_json()["model_anchor"] == anchor
    assert events[0]["data"]["model_anchor"] == anchor
    assert core_run.snapshot()["task_nodes"][model_task.task_id]["model_anchor"] == anchor
