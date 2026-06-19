#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from maze.core.files.lineage import TASK_RESULT_ENVELOPE, run_task_with_file_context
from maze.core.path.path import MaPath
from maze.core.scheduler import llm_instance
from maze.core.scheduler.llm_instance import LlmInstanceManager
from maze.core.scheduler.runtime import TaskRuntime
from maze.core.worker.capabilities import detect_worker_execution_capabilities
from maze.core.workflow.dag_spec import build_dag_workflow, dag_spec_from_payload
from maze.core.workflow.static_run import StaticRun


def code_returning(body: str) -> str:
    return f"def task(**kwargs):\n{body}\n"


def static_workflow_smoke() -> None:
    spec = {
        "name": "phase1-static-smoke",
        "nodes": [
            {
                "id": "produce",
                "task_name": "produce",
                "code_str": code_returning("    return {'value': 'maze'}"),
                "outputs": ["value"],
                "resources": {"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0},
            },
            {
                "id": "consume",
                "task_name": "consume",
                "code_str": code_returning("    return {'result': kwargs['value'].upper()}"),
                "inputs": {"value": {"from": "produce.value"}},
                "outputs": ["result"],
                "resources": {"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0},
                "timeout_seconds": 10,
            },
        ],
        "edges": [{"from": "produce.value", "to": "consume.value"}],
        "run": {"timeout_seconds": 30},
    }
    normalized = dag_spec_from_payload(spec)
    workflow = build_dag_workflow("static-smoke", normalized)
    assert workflow.get_total_task_num() == 2
    assert list(workflow.graph.successors("produce")) == ["consume"]
    assert workflow.tasks["consume"].resources["cpu"] == 1
    assert workflow.tasks["consume"].timeout_seconds == 10

    run = StaticRun("run-static-smoke", "static-smoke", workflow, timeout_seconds=30)
    run.mark_task_started("produce", {"node_id": "node-1", "node_ip": "127.0.0.1"})
    run.mark_task_finished("produce", {"value": "maze"})
    snapshot = run.snapshot()
    assert snapshot["status"] in {"created", "running"}
    assert snapshot["task_nodes"]["produce"]["status"] == "succeeded"
    assert snapshot["task_nodes"]["produce"]["selected_node"]["node_id"] == "node-1"


async def dynamic_append_smoke() -> None:
    sent_messages = []
    mapath = MaPath()
    mapath._send_scheduler_message = sent_messages.append

    run_id = await mapath.create_dynamic_run(max_tasks=4, timeout_seconds=60)
    task, idempotent = await mapath.append_dynamic_task(
        run_id,
        task_spec_payload={
            "task_spec_id": "dyn-echo",
            "task_name": "dyn_echo",
            "code_str": code_returning("    return {'echo': kwargs.get('text', '')}"),
            "inputs": [{"name": "text"}],
            "outputs": [{"name": "echo"}],
            "resources": {"cpu": 1, "cpu_mem": 32, "gpu": 0, "gpu_mem": 0},
            "timeout_seconds": 5,
        },
        inputs={"text": "maze"},
        request_id="append-1",
    )
    snapshot = await mapath.get_dynamic_run_snapshot(run_id)
    events = await mapath.get_dynamic_run_events(run_id)

    assert not idempotent
    assert task.task_id in snapshot["task_nodes"]
    assert snapshot["task_nodes"][task.task_id]["resources"]["cpu"] == 1
    assert snapshot["task_specs"]["dyn-echo"]["timeout_seconds"] == 5
    assert any(event["type"] == "append_task" for event in events)
    assert any(message["type"] == "run_task" for message in sent_messages)

    child, child_idempotent = await mapath.append_dynamic_task(
        run_id,
        task_spec_payload={
            "task_spec_id": "dyn-child",
            "task_name": "dyn_child",
            "code_str": code_returning("    return {'child': True}"),
            "outputs": [{"name": "child"}],
            "resources": {"cpu": 1, "cpu_mem": 32, "gpu": 0, "gpu_mem": 0},
        },
        parents=[task.task_id],
        request_id="append-child",
    )
    snapshot = await mapath.get_dynamic_run_snapshot(run_id)
    assert not child_idempotent
    assert child.task_id in snapshot["task_nodes"]
    assert snapshot["task_nodes"][child.task_id]["parents"] == [task.task_id]
    assert {"source": task.task_id, "target": child.task_id} in snapshot["graph"]["edges"]
    assert child.task_id in snapshot["tasks"]["pending"]


async def cluster_api_shape_smoke() -> None:
    sent_messages = []
    mapath = MaPath()

    def fake_send(message):
        sent_messages.append(message)
        request_id = message["data"]["request_id"]
        if message["type"] == "get_cluster_resources":
            queue = mapath.cluster_resource_requests[request_id]
            queue.put_nowait({"cluster": {"nodes": []}})
        elif message["type"] == "get_cluster_queues":
            queue = mapath.cluster_queue_requests[request_id]
            queue.put_nowait({"queues": {"ready_tasks": [], "running_tasks": []}})
        elif message["type"] == "start_worker":
            queue = mapath.worker_registration_requests[request_id]
            queue.put_nowait({"worker": {"registration_status": "created", "node_id": "node-1"}})

    mapath._send_scheduler_message = fake_send
    resources = await mapath.get_cluster_resources(timeout=0.1)
    queues = await mapath.get_cluster_queues(timeout=0.1)
    worker = await mapath.start_worker(
        "127.0.0.1",
        "node-1",
        {"cpu": 1, "cpu_mem": 1024, "gpu_resource": {}},
        {"workspace_sandbox": True, "docker_sandbox": False},
        timeout=0.1,
    )

    assert resources["cluster"]["nodes"] == []
    assert queues["queues"]["ready_tasks"] == []
    assert worker["worker"]["registration_status"] == "created"
    assert [message["type"] for message in sent_messages] == [
        "get_cluster_resources",
        "get_cluster_queues",
        "start_worker",
    ]


def worker_execution_controls_smoke() -> None:
    runtime = TaskRuntime(
        "wf",
        "task",
        task_input={},
        task_output={},
        resources={"cpu": 1, "cpu_mem": 1, "gpu": 0, "gpu_mem": 0},
        code_str=code_returning("    return {'ok': True}"),
        timeout_seconds=0.001,
    )
    runtime.begin_attempt()
    runtime.set_task_status("running")
    assert runtime.timeout_seconds == 0.001
    assert runtime.has_timed_out(now=runtime.started_time + 1)

    caps = detect_worker_execution_capabilities(force=True)
    assert caps["workspace_sandbox"] is True
    assert "docker_sandbox" in caps


def artifact_and_log_surface_smoke() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        file_context = {
            "enabled": True,
            "workspace_dir": str(workspace),
            "run_id": "run-artifact-smoke",
            "task_id": "task-artifact-smoke",
        }

        def task_callable(_input):
            Path("logs").mkdir(exist_ok=True)
            Path("logs/maze-command.stdout").write_text("hello stdout\n", encoding="utf-8")
            Path("result.txt").write_text("artifact body\n", encoding="utf-8")
            return {"ok": True}

        result = run_task_with_file_context(task_callable, {}, file_context)
        assert result[TASK_RESULT_ENVELOPE] is True
        files = result["file_manifest"]["files"]
        paths = {item["path"] for item in files}
        assert "result.txt" in paths
        assert "logs/maze-command.stdout" in paths


def llm_instance_lifecycle_smoke() -> None:
    class FakeRemoteCall:
        def __init__(self, value=None):
            self.value = value

    class FakeRemoteMethod:
        def __init__(self, value=None):
            self.calls = 0
            self.value = value

        def remote(self, *args, **kwargs):
            self.calls += 1
            return FakeRemoteCall(self.value)

    class FakeActor:
        def __init__(self):
            self.start_server = FakeRemoteMethod(True)
            self.get_port = FakeRemoteMethod("8123")
            self.stop_server = FakeRemoteMethod(None)

    class FakeActorOptions:
        def __init__(self, actor):
            self.actor = actor

        def remote(self, **kwargs):
            self.kwargs = kwargs
            return self.actor

    class FakeLLMServerActor:
        actor = FakeActor()
        options_kwargs = None

        @classmethod
        def options(cls, **kwargs):
            cls.options_kwargs = kwargs
            return FakeActorOptions(cls.actor)

    def fake_ray_get(value):
        return value.value if isinstance(value, FakeRemoteCall) else value

    manager = LlmInstanceManager()
    node_id = "0" * 56
    with (
        patch.object(llm_instance, "LLMServerActor", FakeLLMServerActor),
        patch.object(llm_instance.ray, "get", fake_ray_get),
    ):
        port = manager.start_llm_instance(
            instance_id="llm-smoke",
            model="mock-model",
            node_ip="127.0.0.1",
            node_id=node_id,
            gpu_id=0,
            resources={"cpu": 1, "cpu_mem": 1024, "gpu": 1, "gpu_mem": 4096},
        )
        assert port == "8123"
        assert manager.id_to_instance_addr["llm-smoke"] == "127.0.0.1:8123"
        assert manager.get_instance_resource_detail("llm-smoke")["node_id"] == node_id
        manager.stop_llm_instance("llm-smoke")
        assert "llm-smoke" not in manager.id_to_instance_actor
        assert FakeLLMServerActor.actor.stop_server.calls == 1


def main() -> None:
    static_workflow_smoke()
    asyncio.run(dynamic_append_smoke())
    asyncio.run(cluster_api_shape_smoke())
    worker_execution_controls_smoke()
    artifact_and_log_surface_smoke()
    llm_instance_lifecycle_smoke()
    print("core boundary smoke passed")


if __name__ == "__main__":
    main()
