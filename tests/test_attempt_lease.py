import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from maze.core.files.lineage import (
    ArtifactError,
    TASK_RESULT_ENVELOPE,
    run_task_with_file_context,
)
from maze.core.path.path import MaPath
from maze.core.scheduler import runtime as runtime_module
from maze.core.scheduler.resource import Node, ResourceManager
from maze.core.scheduler.runtime import SelectedNode, TaskRuntime, WorkflowRuntimeManager
from maze.core.scheduler.scheduler import Scheduler
from maze.core.workflow.workflow import Workflow


TASK_RESOURCES = {
    "cpu": 1,
    "cpu_mem": 128,
    "gpu": 0,
    "gpu_mem": 0,
}


def _task(task_id):
    return SimpleNamespace(
        task_id=task_id,
        task_name=task_id,
        completed=False,
        finish_time=None,
        start_time=None,
        created_time=0,
        can_predict=False,
    )


def _reservation(reservation_kind="task"):
    manager = ResourceManager()
    capacity = {
        "cpu": 4,
        "cpu_mem": 1024,
        "gpu_resource": {},
    }
    manager.nodes["node-1"] = Node(
        "node-1",
        "127.0.0.1",
        capacity,
        capacity,
    )
    manager.running_task_counts["node-1"] = 0
    manager._ray_node_index = lambda: {}
    manager._is_node_alive = lambda *_: True
    selection = manager.select_node(
        TASK_RESOURCES,
        reservation_kind=reservation_kind,
        run_id="run-1",
        task_id="task-1",
        attempt=1,
        dispatch_id="dispatch-1",
    )
    assert selection
    return manager, selection


def test_duplicate_completion_only_releases_successor_once():
    workflow = Workflow("run-1")
    parent = _task("parent")
    child = _task("child")
    workflow.tasks = {"parent": parent, "child": child}
    workflow.graph.add_edge("parent", "child")
    workflow.remaining_task_num = 2

    assert workflow.finish_task("parent", {}, "default") == [child]
    assert workflow.remaining_task_num == 1
    assert workflow.finish_task("parent", {}, "default") == []
    assert workflow.remaining_task_num == 1


def test_stale_attempt_and_duplicate_terminal_are_ignored():
    path = object.__new__(MaPath)
    path.task_attempts = {}
    attempt_1 = {
        "workflow_id": "run-1",
        "task_id": "task-1",
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
    }
    attempt_2 = {
        "workflow_id": "run-1",
        "task_id": "task-1",
        "attempt": 2,
        "dispatch_id": "dispatch-2",
        "lease_id": "lease-2",
    }
    attempt_3 = {
        "workflow_id": "run-1",
        "task_id": "task-1",
        "attempt": 3,
        "dispatch_id": "dispatch-3",
        "lease_id": "lease-3",
    }

    assert path._accept_task_attempt_event("start_task", attempt_1)
    assert path._accept_task_attempt_event("task_retry", attempt_1)
    assert path._accept_task_attempt_event("start_task", attempt_2)
    assert not path._accept_task_attempt_event("finish_task", attempt_1)
    assert path._accept_task_attempt_event("finish_task", attempt_2)
    assert not path._accept_task_attempt_event("finish_task", attempt_2)
    assert not path._accept_task_attempt_event("start_task", attempt_3)


def test_releasing_a_lease_twice_does_not_inflate_capacity():
    manager, selection = _reservation()
    node = manager.nodes["node-1"]
    assert node.available_resources["cpu"] == 3

    assert manager.release_lease(selection.lease_id)
    assert node.available_resources["cpu"] == 4
    assert not manager.release_lease(selection.lease_id)
    assert node.available_resources["cpu"] == 4

    manager, selection = _reservation("instance")
    detail = {"lease_id": selection.lease_id}
    manager.release_instance_resource(detail)
    manager.release_instance_resource(detail)
    assert manager.nodes["node-1"].available_resources["cpu"] == 4
    assert manager.running_task_counts["node-1"] == 0


def test_dispatch_failure_releases_provisional_lease():
    manager, selection = _reservation()
    scheduler = object.__new__(Scheduler)
    scheduler.resource_manager = manager
    scheduler.workflow_manager = SimpleNamespace(
        run_task=lambda **_: (_ for _ in ()).throw(RuntimeError("dispatch failed"))
    )

    with pytest.raises(RuntimeError, match="dispatch failed"):
        scheduler._run_task_with_lease(object(), selection, "dispatch-1")

    assert selection.lease_id not in manager.active_leases
    assert manager.nodes["node-1"].available_resources["cpu"] == 4


def test_duplicate_dispatch_is_rejected_before_resource_selection():
    manager = WorkflowRuntimeManager()
    first = TaskRuntime(
        "run-1",
        "task-1",
        task_input={"input_params": {}},
        task_output={},
        resources=TASK_RESOURCES,
    )
    duplicate = TaskRuntime(
        "run-1",
        "task-1",
        task_input={"input_params": {}},
        task_output={},
        resources=TASK_RESOURCES,
    )

    resource_selections = 0
    for task in (first, duplicate):
        if manager.add_task(task):
            resource_selections += 1

    assert resource_selections == 1
    assert manager.workflows["run-1"].tasks["task-1"] is first
    assert manager.add_task(first)


def test_dispatch_adds_attempt_identity_without_mutating_base_file_context(
    monkeypatch,
    tmp_path,
):
    captured = {}

    class FakeRemoteTask:
        @classmethod
        def options(cls, **_):
            return cls

        @classmethod
        def remote(cls, **kwargs):
            captured.update(kwargs)
            return "object-ref"

    monkeypatch.setattr(runtime_module, "remote_task_runner", FakeRemoteTask)
    base_context = {
        "enabled": True,
        "workspace_dir": str(tmp_path),
        "run_id": "run-1",
        "task_id": "task-1",
    }
    payload_task = SimpleNamespace(
        task_id="task-1",
        file_context={**base_context, "run_id": "caller-run-id"},
        to_json=lambda: {"file_context": base_context},
    )
    payload_workflow = Workflow("template")
    payload_workflow.graph.add_node("task-1")
    payload = object.__new__(MaPath)._task_run_payload(
        payload_workflow,
        payload_task,
        "run-1",
    )
    assert payload["file_context"]["run_id"] == "run-1"
    assert payload["file_context"]["parent_file_manifests"] == []

    task = TaskRuntime(
        "run-1",
        "task-1",
        task_input={"input_params": {}},
        task_output={},
        resources=TASK_RESOURCES,
        code_str="def task(): return {}",
        file_context=base_context,
    )
    task.file_manifest = {"attempt": 0}
    manager = WorkflowRuntimeManager()
    manager.add_task(task)

    manager.run_task(
        task,
        SelectedNode("1" * 56, "127.0.0.1"),
        dispatch_id="dispatch-1",
        lease_id="lease-1",
    )

    assert captured["file_context"]["attempt"] == 1
    assert captured["file_context"]["dispatch_id"] == "dispatch-1"
    assert "attempt" not in base_context
    assert "dispatch_id" not in base_context
    assert task.file_manifest is None


def test_attempt_files_are_isolated_and_only_published_manifest_reaches_child(
    tmp_path,
):
    base_context = {
        "enabled": True,
        "workspace_dir": str(tmp_path),
        "run_id": "run-1",
        "task_id": "parent",
    }

    def run_parent(attempt, dispatch_id, content):
        def task(_):
            Path("result.txt").write_text(content, encoding="utf-8")
            return {"content": content}

        return run_task_with_file_context(
            task,
            {},
            {
                **base_context,
                "attempt": attempt,
                "dispatch_id": dispatch_id,
            },
        )

    attempt_1 = run_parent(1, "dispatch-1", "old")
    attempt_2 = run_parent(2, "dispatch-2", "new")
    manifest_1 = attempt_1["file_manifest"]
    manifest_2 = attempt_2["file_manifest"]

    assert attempt_1[TASK_RESULT_ENVELOPE] is True
    assert manifest_1["attempt"] == 1
    assert manifest_1["dispatch_id"] == "dispatch-1"
    assert manifest_1["published"] is False
    assert manifest_2["attempt"] == 2
    assert manifest_2["dispatch_id"] == "dispatch-2"
    assert manifest_2["published"] is False

    work_root = tmp_path / "runs" / "run-1" / "work" / "tasks" / "parent"
    assert (work_root / "attempt-1" / "dispatch-1" / "result.txt").read_text() == "old"
    assert (work_root / "attempt-2" / "dispatch-2" / "result.txt").read_text() == "new"
    assert "attempt-1/dispatch-1" in manifest_1["files"][0]["storage_path"]
    assert "attempt-2/dispatch-2" in manifest_2["files"][0]["storage_path"]
    assert not (tmp_path / "runs" / "run-1" / "file_manifests").exists()

    path = object.__new__(MaPath)
    path.task_attempts = {}
    start = {
        "workflow_id": "run-1",
        "task_id": "parent",
        "attempt": 2,
        "dispatch_id": "dispatch-2",
        "lease_id": "lease-2",
    }
    finish = {**start, "file_manifest": manifest_2}
    bad_finish = {
        **finish,
        "file_manifest": {**manifest_2, "dispatch_id": "wrong-dispatch"},
    }

    assert path._accept_task_attempt_event("start_task", start)
    with pytest.raises(ArtifactError, match="dispatch_id"):
        path._accept_task_attempt_event("finish_task", bad_finish)
    assert path.task_attempts[("run-1", "parent")]["state"] == "running"
    assert path._accept_task_attempt_event("finish_task", finish)
    published = path._publish_task_file_manifest(finish)
    assert published["published"] is True
    assert published["attempt"] == 2
    assert published["dispatch_id"] == "dispatch-2"
    assert manifest_2["published"] is False

    staged_child_context = {
        **base_context,
        "task_id": "staged-child",
        "attempt": 1,
        "dispatch_id": "staged-child-dispatch",
        "parent_file_manifests": [manifest_1],
    }
    with pytest.raises(ArtifactError, match="not published"):
        run_task_with_file_context(lambda _: {}, {}, staged_child_context)

    published_child_context = {
        **base_context,
        "task_id": "published-child",
        "attempt": 1,
        "dispatch_id": "published-child-dispatch",
        "parent_file_manifests": [published],
    }
    child = run_task_with_file_context(
        lambda _: {"content": Path("result.txt").read_text(encoding="utf-8")},
        {},
        published_child_context,
    )
    assert child["result"] == {"content": "new"}


def test_task_exception_does_not_expose_staged_manifest():
    sent = []
    task = SimpleNamespace(
        workflow_id="run-1",
        task_id="task-1",
        attempt=1,
        dispatch_id="dispatch-1",
        lease_id="lease-1",
        file_manifest={"published": False, "storage_path": "/staging/result.txt"},
    )
    scheduler = object.__new__(Scheduler)
    scheduler._send_task_exception(
        SimpleNamespace(send=sent.append),
        task,
        {"error_type": "user_code", "message": "failed"},
    )

    message = json.loads(sent[0])
    assert message["type"] == "task_exception"
    assert "file_manifest" not in message["data"]
