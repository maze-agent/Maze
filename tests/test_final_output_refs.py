import asyncio
import importlib
from types import SimpleNamespace

import pytest

from maze.client.maze.models import TaskOutput
from maze.client.maze.workflow import _encode_output_refs
from maze.core.path.path import MaPath
from maze.core.workflow.static_run import StaticRun, StaticRunStore
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


TASK_RESOURCES = {
    "cpu": 1,
    "cpu_mem": 0,
    "gpu": 0,
    "gpu_mem": 0,
}


def _output_params(*keys: str) -> dict:
    return {
        "output_params": {
            str(index): {"key": key, "data_type": "any"}
            for index, key in enumerate(keys, start=1)
        }
    }


def _add_task(workflow: Workflow, task_id: str, *output_keys: str) -> CodeTask:
    task = CodeTask(workflow.id, task_id, task_id)
    task.save_task(
        task_input={"input_params": {}},
        task_output=_output_params(*output_keys),
        code_str="",
        code_ser="",
        resources=TASK_RESOURCES,
    )
    workflow.add_task(task_id, task)
    return task


def _workflow() -> Workflow:
    workflow = Workflow("template")
    _add_task(workflow, "prepare", "dag_id")
    _add_task(workflow, "fuse", "final_answer", "score")
    workflow.add_edge("prepare", "fuse")
    return workflow


def _output_ref(task_id: str, output_key: str) -> dict:
    return {
        "__maze_output_ref__": True,
        "task_id": task_id,
        "output_key": output_key,
    }


def test_encode_output_refs_recurses_through_dict_list_and_tuple():
    literal_reference_shape = {
        "task_id": "literal-task",
        "output_key": "literal-output",
    }
    encoded = _encode_output_refs(
        {
            "answer": TaskOutput("fuse", "final_answer"),
            "nested": [
                TaskOutput("prepare", "dag_id"),
                {
                    "tuple": (
                        TaskOutput("fuse", "score"),
                        "literal",
                    )
                },
            ],
            "literal": literal_reference_shape,
        }
    )

    assert encoded == {
        "answer": _output_ref("fuse", "final_answer"),
        "nested": [
            _output_ref("prepare", "dag_id"),
            {
                "tuple": [
                    _output_ref("fuse", "score"),
                    "literal",
                ]
            },
        ],
        "literal": literal_reference_shape,
    }


def test_client_run_sends_declared_final_output_refs(monkeypatch):
    requests = []
    workflow_module = importlib.import_module("maze.client.maze.workflow")

    class Response:
        status_code = 200
        text = "ok"

        def json(self):
            return {"status": "success", "run_id": "run-1"}

    monkeypatch.setattr(
        workflow_module.requests,
        "post",
        lambda url, json: requests.append((url, json)) or Response(),
    )
    workflow = workflow_module.MaWorkflow("template", "http://maze.test")
    workflow.final_output_refs = {
        "answer": TaskOutput("fuse", "final_answer"),
    }

    assert workflow.run() == "run-1"
    assert requests[0][1]["final_output_refs"] == {
        "answer": _output_ref("fuse", "final_answer"),
    }


def test_static_run_resolves_declared_nested_final_outputs():
    final_output_refs = _encode_output_refs(
        {
            "final_answer": TaskOutput("fuse", "final_answer"),
            "details": [
                {"dag_id": TaskOutput("prepare", "dag_id")},
                (TaskOutput("fuse", "score"), "literal"),
            ],
            "literal": {
                "task_id": "not-a-reference",
                "output_key": "still-literal",
            },
        }
    )
    run = StaticRun(
        "run-1",
        "template",
        _workflow(),
        final_output_refs=final_output_refs,
    )

    run.mark_task_finished("prepare", {"dag_id": "gaia-1"})
    run.mark_task_finished(
        "fuse",
        {"final_answer": "FINAL_FUSED_SENTINEL", "score": 0.9},
    )

    assert run.status == "succeeded"
    assert run.result_summary == {
        "final_answer": "FINAL_FUSED_SENTINEL",
        "details": [
            {"dag_id": "gaia-1"},
            [0.9, "literal"],
        ],
        "literal": {
            "task_id": "not-a-reference",
            "output_key": "still-literal",
        },
    }


def test_static_run_without_final_output_refs_keeps_legacy_task_result_map():
    run = StaticRun("run-1", "template", _workflow())

    run.mark_task_finished("prepare", {"dag_id": "gaia-1"})
    run.mark_task_finished(
        "fuse",
        {"final_answer": "FINAL_FUSED_SENTINEL", "score": 0.9},
    )

    assert run.status == "succeeded"
    assert run.result_summary == {
        "fuse": {"final_answer": "FINAL_FUSED_SENTINEL", "score": 0.9},
        "prepare": {"dag_id": "gaia-1"},
    }


def test_missing_declared_final_output_atomically_fails_run_and_finishing_task():
    run = StaticRun(
        "run-1",
        "template",
        _workflow(),
        final_output_refs={
            "answer": _output_ref("fuse", "final_answer"),
        },
    )

    run.mark_task_finished("prepare", {"dag_id": "gaia-1"})
    error = run.mark_task_finished("fuse", {"score": 0.9})

    snapshot = run.snapshot()
    assert error == {
        "error_type": "final_output_resolution",
        "message": "Task fuse did not return final output 'final_answer'",
        "details": {
            "finishing_task_id": "fuse",
            "referenced_task_id": "fuse",
            "output_key": "final_answer",
        },
    }
    assert snapshot["status"] == "failed"
    assert snapshot["finished_time"] is not None
    assert snapshot["result_summary"] is None
    assert snapshot["error_summary"] == error
    assert snapshot["task_nodes"]["prepare"]["status"] == "succeeded"
    assert snapshot["task_nodes"]["fuse"]["status"] == "failed"
    assert snapshot["task_nodes"]["fuse"]["error"] == error


def test_path_persists_missing_final_output_as_terminal_exception_without_manifest(
    tmp_path,
):
    run_id = "missing-final-output"
    workflow = Workflow("template")
    _add_task(workflow, "fuse", "final_answer")
    workflow.graph.graph["file_context"] = {
        "enabled": True,
        "workspace_dir": str(tmp_path),
        "run_id": run_id,
    }
    static_run = StaticRun(
        run_id,
        "template",
        workflow,
        final_output_refs={
            "answer": _output_ref("fuse", "final_answer"),
        },
    )
    identity = {
        "workflow_id": run_id,
        "task_id": "fuse",
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "node_id": "node-1",
    }
    static_run.mark_task_started("fuse", identity)
    workflow.mark_task_started("fuse")

    class ResourceHistory:
        def record(self, **kwargs):
            return {
                "run_id": kwargs["run_id"],
                "task_id": kwargs["task_id"],
                "status": kwargs["status"],
            }

    class Metrics:
        def on_task_finished(self, *_args, **_kwargs):
            return None

        def on_run_status_change(self, *_args, **_kwargs):
            return None

    path = object.__new__(MaPath)
    path.static_runs = {run_id: static_run}
    path.dynamic_runs = {}
    path.submit_workflows = {run_id: workflow}
    path.async_que = {run_id: asyncio.Queue()}
    path.static_run_store = StaticRunStore(tmp_path / "run-store")
    path.resource_history = ResourceHistory()
    path.task_attempts = {}
    path.pre_dispatch_rejections = set()
    path.global_metrics = Metrics()
    path.strategy = "FCFS"
    path._observe_task_runtime = lambda *_args, **_kwargs: None
    sent_messages = []
    path._send_scheduler_message = sent_messages.append

    assert path._accept_task_attempt_event("start_task", identity)
    finish = {
        "type": "finish_task",
        "data": {
            **identity,
            "result": {"score": 0.9},
            "file_manifest": {
                "run_id": run_id,
                "task_id": "fuse",
                "attempt": 1,
                "dispatch_id": "dispatch-1",
                "lease_id": "lease-1",
                "published": False,
                "created_time": 123.0,
                "files": [],
            },
        },
    }
    transaction = path._begin_task_attempt_event_transaction(
        "finish_task",
        finish["data"],
    )
    assert path._accept_task_attempt_event("finish_task", finish["data"])

    asyncio.run(
        path._handle_static_finish_scheduler_event(finish, transaction)
    )

    snapshot = path.static_run_store.load_run(run_id)
    assert snapshot["status"] == "failed"
    assert snapshot["task_nodes"]["fuse"]["status"] == "failed"
    assert snapshot["error_summary"]["error_type"] == "final_output_resolution"
    assert path.task_attempts[(run_id, "fuse")]["state"] == "terminal"
    assert workflow.remaining_task_num == 1
    event = path.static_run_store.load_events(run_id)[0]
    assert event["type"] == "task_exception"
    assert event["data"]["error"]["error_type"] == "final_output_resolution"
    assert "file_manifest" not in event["data"]
    assert path.async_que[run_id].get_nowait()["type"] == "task_exception"
    assert sent_messages == [
        {"type": "clear_workflow", "data": {"workflow_id": run_id}}
    ]
    assert not (
        tmp_path
        / "runs"
        / run_id
        / "file_manifests"
        / "tasks"
        / "fuse.json"
    ).exists()


class _MemoryStaticRunStore:
    def save_run(self, _snapshot):
        return None

    def append_event(self, _run_id, _event):
        return None


def _path_for(workflow: Workflow) -> tuple[MaPath, list[dict]]:
    path = object.__new__(MaPath)
    path.workflows = {workflow.id: workflow}
    path.submit_workflows = {}
    path.async_que = {}
    path.static_runs = {}
    path.strategy = "Default"
    path.static_run_store = _MemoryStaticRunStore()
    path.global_metrics = SimpleNamespace(on_run_submitted=lambda _run_id: None)
    path.scheduler_process = SimpleNamespace(
        is_alive=lambda: True,
        pid=123,
        exitcode=None,
    )
    sent_messages = []
    path._send_scheduler_message = sent_messages.append
    return path, sent_messages


@pytest.mark.parametrize(
    "final_output_refs",
    [
        {"answer": _output_ref("missing-task", "final_answer")},
        {"answer": _output_ref("fuse", "missing-output")},
    ],
    ids=["unknown-task", "unknown-output"],
)
def test_invalid_final_output_ref_is_rejected_before_dispatch(final_output_refs):
    path, sent_messages = _path_for(_workflow())

    with pytest.raises(ValueError):
        path.run_workflow("template", final_output_refs=final_output_refs)

    assert sent_messages == []
    assert path.static_runs == {}
    assert path.submit_workflows == {}
    assert path.async_que == {}
