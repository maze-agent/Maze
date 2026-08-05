import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from maze.core.files.lineage import (
    ArtifactError,
    TASK_RESULT_ENVELOPE,
    publish_task_file_manifest,
    run_task_with_file_context,
)
from maze.core.path.path import MaPath
from maze.core.resource_history import ResourceHistoryStore
from maze.core.scheduler import runtime as runtime_module
from maze.core.scheduler.resource import Node, ResourceManager
from maze.core.scheduler.runtime import SelectedNode, TaskRuntime, WorkflowRuntimeManager
from maze.core.scheduler.runtime_estimator import RuntimeEstimator
from maze.core.scheduler.scheduler import Scheduler
from maze.core.workflow.dynamic import DynamicRun, DynamicTaskSpec
from maze.core.workflow.dynamic_store import DynamicRunStore
from maze.core.workflow.static_run import StaticRun, StaticRunStore
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


INTERNAL_TASK_RESOURCES = {
    "cpu": 1,
    "cpu_mem": 128,
    "gpu": 0,
    "gpu_mem": 0,
}
PUBLIC_TASK_RESOURCES = {
    "cpu_num": 1,
    "gpu_mem": 0,
    "io_num": 0,
}


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
        INTERNAL_TASK_RESOURCES,
        reservation_kind=reservation_kind,
        run_id="run-1",
        task_id="task-1",
        attempt=1,
        dispatch_id="dispatch-1",
    )
    assert selection
    return manager, selection


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
        resources=PUBLIC_TASK_RESOURCES,
    )
    duplicate = TaskRuntime(
        "run-1",
        "task-1",
        task_input={"input_params": {}},
        task_output={},
        resources=PUBLIC_TASK_RESOURCES,
    )

    assert manager.add_task(first)
    assert manager.add_task(first)
    assert not manager.add_task(duplicate)
    assert manager.workflows["run-1"].tasks["task-1"] is first


def test_task_exception_does_not_expose_staged_manifest():
    sent = []
    task = SimpleNamespace(
        workflow_id="run-1",
        task_id="task-1",
        task_kind="cpu",
        queue_name="cpu",
        attempt=1,
        dispatch_id="dispatch-1",
        lease_id="lease-1",
        file_manifest={
            "published": False,
            "storage_path": "/staging/result.txt",
        },
        last_metrics=None,
        last_schedule_decision=None,
        scheduling_metadata=None,
        fault_tolerance={"enabled": True, "status": "failed", "attempts": []},
    )
    scheduler = object.__new__(Scheduler)
    scheduler._send_task_exception(
        SimpleNamespace(send=sent.append),
        task,
        {"error_type": "user_code", "message": "failed"},
    )

    message = json.loads(sent[0])
    assert message["type"] == "task_exception"
    assert message["data"]["dispatch_id"] == "dispatch-1"
    assert message["data"]["lease_id"] == "lease-1"
    assert "file_manifest" not in message["data"]


def test_stale_attempt_and_duplicate_terminal_are_ignored():
    path = object.__new__(MaPath)
    path.task_attempts = {}
    attempt_1 = {
        "workflow_id": "run-1",
        "task_id": "task-1",
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "node_id": "node-1",
        "node_ip": "10.0.0.1",
    }
    attempt_2 = {
        "workflow_id": "run-1",
        "task_id": "task-1",
        "attempt": 2,
        "dispatch_id": "dispatch-2",
        "lease_id": "lease-2",
        "node_id": "node-2",
        "node_ip": "10.0.0.2",
    }
    attempt_3 = {
        "workflow_id": "run-1",
        "task_id": "task-1",
        "attempt": 3,
        "dispatch_id": "dispatch-3",
        "lease_id": "lease-3",
    }

    assert path._accept_task_attempt_event("start_task", attempt_1)
    assert path._accept_task_attempt_event("task_retry", {**attempt_1, "error": "retry"})
    assert path._accept_task_attempt_event("start_task", attempt_2)
    assert not path._accept_task_attempt_event("finish_task", attempt_1)
    assert not path._accept_task_attempt_event(
        "task_exception",
        {**attempt_1, "error": "stale"},
    )
    assert path._accept_task_attempt_event("finish_task", attempt_2)
    terminal = path.task_attempts[("run-1", "task-1")].copy()
    assert not path._accept_task_attempt_event("finish_task", attempt_2)
    assert not path._accept_task_attempt_event("task_exception", {**attempt_2, "error": "late"})
    assert not path._accept_task_attempt_event("start_task", attempt_3)
    assert path.task_attempts[("run-1", "task-1")] == terminal
    assert terminal["selected_node"] == {
        "node_id": "node-2",
        "node_ip": "10.0.0.2",
    }


def test_pre_dispatch_rejection_is_terminal_without_creating_an_attempt():
    path = object.__new__(MaPath)
    path.task_attempts = {}
    rejection = {
        "workflow_id": "run-1",
        "task_id": "task-1",
        "pre_dispatch": True,
        "attempt": 0,
        "dispatch_id": None,
        "lease_id": None,
        "error": {"error_type": "scheduler_error"},
    }

    assert path._accept_task_attempt_event("task_exception", rejection)
    assert path.task_attempts == {}
    assert path.pre_dispatch_rejections == {("run-1", "task-1")}
    assert not path._accept_task_attempt_event("task_exception", rejection)
    assert not path._accept_task_attempt_event(
        "start_task",
        {
            "workflow_id": "run-1",
            "task_id": "task-1",
            "attempt": 1,
            "dispatch_id": "dispatch-1",
            "lease_id": "lease-1",
        },
    )


def test_pre_dispatch_rejection_requires_the_explicit_non_attempt_identity():
    path = object.__new__(MaPath)
    path.task_attempts = {}

    assert not path._accept_task_attempt_event(
        "task_exception",
        {
            "workflow_id": "run-1",
            "task_id": "task-1",
            "pre_dispatch": True,
            "attempt": 0,
            "dispatch_id": "synthetic-dispatch",
            "lease_id": None,
        },
    )
    assert path.task_attempts == {}
    assert path.pre_dispatch_rejections == set()


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
    task = TaskRuntime(
        "run-1",
        "task-1",
        task_input={"input_params": {}},
        task_output={},
        resources=PUBLIC_TASK_RESOURCES,
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

    assert captured["file_context"] == {
        **base_context,
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "node_id": "1" * 56,
    }
    assert "attempt" not in base_context
    assert "dispatch_id" not in base_context
    assert task.file_manifest is None


def test_gpu_dispatch_uses_single_call_runner(monkeypatch):
    called = []

    class FakeRemoteTask:
        @classmethod
        def options(cls, **_):
            return cls

        @classmethod
        def remote(cls, **_):
            called.append("cpu")
            return "cpu-ref"

    class FakeRemoteGpuTask(FakeRemoteTask):
        @classmethod
        def remote(cls, **_):
            called.append("gpu")
            return "gpu-ref"

    monkeypatch.setattr(runtime_module, "remote_task_runner", FakeRemoteTask)
    monkeypatch.setattr(runtime_module, "remote_gpu_task_runner", FakeRemoteGpuTask)
    task = TaskRuntime(
        "run-1",
        "task-1",
        task_input={"input_params": {}},
        task_output={},
        resources={"cpu_num": 1, "gpu_mem": 1024, "io_num": 0},
        task_kind="gpu",
        code_str="def task(): return {}",
    )
    manager = WorkflowRuntimeManager()
    manager.add_task(task)

    manager.run_task(task, SelectedNode("1" * 56, "127.0.0.1", gpu_id=0))

    assert called == ["gpu"]


def test_attempt_files_are_isolated_and_only_accepted_manifest_is_published(tmp_path):
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
                "lease_id": f"lease-{attempt}",
            },
        )

    attempt_1 = run_parent(1, "dispatch-1", "old")
    attempt_2 = run_parent(2, "dispatch-2", "new")
    manifest_1 = attempt_1["file_manifest"]
    manifest_2 = attempt_2["file_manifest"]

    assert attempt_1[TASK_RESULT_ENVELOPE] is True
    assert manifest_1["published"] is False
    assert manifest_2["published"] is False
    work_root = tmp_path / "runs" / "run-1" / "work" / "tasks" / "parent"
    assert (work_root / "attempt-1" / "dispatch-1" / "result.txt").read_text() == "old"
    assert (work_root / "attempt-2" / "dispatch-2" / "result.txt").read_text() == "new"
    assert "attempt-1/dispatch-1" in manifest_1["files"][0]["storage_path"]
    assert "attempt-2/dispatch-2" in manifest_2["files"][0]["storage_path"]

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
    bad_lease_finish = {
        **finish,
        "file_manifest": {**manifest_2, "lease_id": "wrong-lease"},
    }

    assert path._accept_task_attempt_event("start_task", start)
    with pytest.raises(ArtifactError, match="dispatch_id"):
        path._accept_task_attempt_event("finish_task", bad_finish)
    assert path.task_attempts[("run-1", "parent")]["state"] == "running"
    with pytest.raises(ArtifactError, match="lease_id"):
        path._accept_task_attempt_event("finish_task", bad_lease_finish)
    assert path.task_attempts[("run-1", "parent")]["state"] == "running"
    assert path._accept_task_attempt_event("finish_task", finish)
    published = path._publish_task_file_manifest(finish)
    assert published["published"] is True
    assert published["attempt"] == 2
    assert published["dispatch_id"] == "dispatch-2"
    assert published["lease_id"] == "lease-2"
    assert manifest_2["published"] is False

    publish_task_file_manifest(
        {
            **base_context,
            "enabled": True,
            "attempt": 2,
            "dispatch_id": "dispatch-2",
            "lease_id": "lease-2",
        },
        published,
    )
    published_path = (
        tmp_path
        / "runs"
        / "run-1"
        / "file_manifests"
        / "tasks"
        / "parent.json"
    )
    assert json.loads(published_path.read_text(encoding="utf-8")) == published

    staged_child_context = {
        **base_context,
        "task_id": "staged-child",
        "attempt": 1,
        "dispatch_id": "staged-child-dispatch",
        "lease_id": "staged-child-lease",
        "parent_file_manifests": [manifest_1],
    }
    with pytest.raises(ArtifactError, match="not published"):
        run_task_with_file_context(lambda _: {}, {}, staged_child_context)

    published_child_context = {
        **base_context,
        "task_id": "published-child",
        "attempt": 1,
        "dispatch_id": "published-child-dispatch",
        "lease_id": "published-child-lease",
        "parent_file_manifests": [published],
    }
    child = run_task_with_file_context(
        lambda _: {"content": Path("result.txt").read_text(encoding="utf-8")},
        {},
        published_child_context,
    )
    assert child["result"] == {"content": "new"}


def test_static_task_snapshot_persists_attempt_identity_node_manifest_and_error():
    workflow = Workflow("template")
    success_task = CodeTask("template", "success", "success")
    failed_task = CodeTask("template", "failed", "failed")
    for task in (success_task, failed_task):
        task.save_task(
            task_input={"input_params": {}},
            task_output={"output_params": {}},
            code_str="",
            code_ser="",
            resources=PUBLIC_TASK_RESOURCES,
        )
        workflow.add_task(task.task_id, task)
    run = StaticRun("run-1", "template", workflow)

    run.mark_task_started("success", {
        "attempt": 1,
        "dispatch_id": "dispatch-success",
        "lease_id": "lease-success",
        "node_id": "node-1",
        "node_ip": "10.0.0.1",
    })
    run.mark_task_finished(
        "success",
        result={"ok": True},
        file_manifest={"published": True, "files": []},
        node_id="node-1",
        attempt=1,
        dispatch_id="dispatch-success",
        lease_id="lease-success",
    )
    run.mark_task_started("failed", {
        "attempt": 2,
        "dispatch_id": "dispatch-failed",
        "lease_id": "lease-failed",
        "node_id": "node-2",
        "node_ip": "10.0.0.2",
    })
    run.mark_task_failed(
        "failed",
        {"error_type": "user_code", "message": "failed"},
        file_manifest={"published": False, "files": [{"path": "staged.txt"}]},
        attempt=2,
        dispatch_id="dispatch-failed",
        lease_id="lease-failed",
    )

    snapshot = run.snapshot()
    success = snapshot["task_nodes"]["success"]
    failed = snapshot["task_nodes"]["failed"]
    assert (success["attempt"], success["dispatch_id"], success["lease_id"]) == (
        1,
        "dispatch-success",
        "lease-success",
    )
    assert success["selected_node"]["node_ip"] == "10.0.0.1"
    assert success["file_manifest"]["published"] is True
    assert (failed["attempt"], failed["dispatch_id"], failed["lease_id"]) == (
        2,
        "dispatch-failed",
        "lease-failed",
    )
    assert failed["selected_node"]["node_id"] == "node-2"
    assert failed["error"]["error_type"] == "user_code"
    assert failed["file_manifest"] is None


def test_dynamic_finish_persists_attempt_before_notification_and_successor_dispatch(tmp_path):
    run_id = "dynamic-run"
    run = DynamicRun(run_id)
    parent, _ = run.append_task(
        DynamicTaskSpec(
            task_spec_id="parent-spec",
            task_name="parent",
            code_str="def parent(): return {'status': 'ok'}",
            code_ser=None,
        )
    )
    child, _ = run.append_task(
        DynamicTaskSpec(
            task_spec_id="child-spec",
            task_name="child",
            code_str="def child(): return {'status': 'ok'}",
            code_ser=None,
        ),
        parents=[parent.task_id],
    )
    run.mark_started(parent.task_id)

    path = object.__new__(MaPath)
    path.dynamic_runs = {run_id: run}
    path.submit_workflows = {}
    path.dynamic_run_store = DynamicRunStore(tmp_path / "runs")
    path.resource_history = ResourceHistoryStore(tmp_path / "resource-history.json")
    path.runtime_estimator = RuntimeEstimator()
    path.task_attempts = {}
    dispatched = []
    path._submit_dynamic_task = lambda ready_task: dispatched.append(ready_task.task_id)
    observed = {}

    class CompletionQueue:
        async def put(self, message):
            if observed:
                return
            snapshot = path.dynamic_run_store.load_run(run_id)
            task_node = snapshot["task_nodes"][parent.task_id]
            observed.update({
                "message": message,
                "completed": parent.task_id in snapshot["tasks"]["completed"],
                "attempt": task_node["attempt"],
                "dispatch_id": task_node["dispatch_id"],
                "lease_id": task_node["lease_id"],
                "node_id": task_node["selected_node"]["node_id"],
                "published": task_node["file_manifest"]["published"],
                "dispatched": list(dispatched),
            })

    path.async_que = {run_id: CompletionQueue()}
    identity = {
        "workflow_id": run_id,
        "task_id": parent.task_id,
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "node_id": "node-1",
        "node_ip": "10.0.0.1",
    }
    manifest = {
        "run_id": run_id,
        "task_id": parent.task_id,
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "published": False,
        "files": [],
    }
    assert path._accept_task_attempt_event("start_task", identity)
    finish = {
        "type": "finish_task",
        "data": {
            **identity,
            "result": {"status": "ok"},
            "file_manifest": manifest,
        },
    }
    assert path._accept_task_attempt_event("finish_task", finish["data"])

    asyncio.run(path._handle_dynamic_scheduler_event(finish))

    assert observed == {
        "message": {"type": "dynamic_event"},
        "completed": True,
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "node_id": "node-1",
        "published": True,
        "dispatched": [],
    }
    assert dispatched == [child.task_id]
    assert manifest["published"] is False
    finish_event = path.dynamic_run_store.load_events(run_id)[0]
    assert finish_event["data"]["attempt"] == 1
    assert finish_event["data"]["dispatch_id"] == "dispatch-1"
    assert finish_event["data"]["lease_id"] == "lease-1"
    assert finish_event["data"]["file_manifest"]["published"] is True


def test_dynamic_exception_drops_staged_manifest_and_persists_attempt_error(tmp_path):
    run_id = "dynamic-failure"
    run = DynamicRun(run_id)
    task, _ = run.append_task(
        DynamicTaskSpec(
            task_spec_id="task-spec",
            task_name="task",
            code_str="def task(): raise RuntimeError('failed')",
            code_ser=None,
        )
    )
    run.mark_started(task.task_id)
    path = object.__new__(MaPath)
    path.dynamic_runs = {run_id: run}
    path.submit_workflows = {}
    path.dynamic_run_store = DynamicRunStore(tmp_path / "runs")
    path.resource_history = ResourceHistoryStore(tmp_path / "resource-history.json")
    path.runtime_estimator = RuntimeEstimator()
    path.task_attempts = {}
    path.async_que = {}

    identity = {
        "workflow_id": run_id,
        "task_id": task.task_id,
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "node_id": "node-1",
    }
    assert path._accept_task_attempt_event("start_task", identity)
    message = {
        "type": "task_exception",
        "data": {
            **identity,
            "error": {"error_type": "user_code", "message": "failed"},
            "file_manifest": {
                "run_id": run_id,
                "task_id": task.task_id,
                "attempt": 1,
                "dispatch_id": "dispatch-1",
                "published": False,
            },
        },
    }
    assert path._accept_task_attempt_event("task_exception", message["data"])

    asyncio.run(path._handle_dynamic_scheduler_event(message))

    snapshot = path.dynamic_run_store.load_run(run_id)
    task_node = snapshot["task_nodes"][task.task_id]
    assert task_node["attempt"] == 1
    assert task_node["dispatch_id"] == "dispatch-1"
    assert task_node["lease_id"] == "lease-1"
    assert task_node["error"]["error_type"] == "user_code"
    assert task_node["file_manifest"] is None
    assert task.task_id not in snapshot["task_file_manifests"]
    assert "file_manifest" not in message["data"]


@pytest.mark.parametrize(
    "failure_stage",
    ["snapshot", "manifest", "post_commit_continuation"],
)
def test_static_finish_replay_after_persistence_failure_is_idempotent(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    run_id = "replayed-run"
    workflow = Workflow("template")
    for task_id in ("parent", "child"):
        task = CodeTask("template", task_id, task_id)
        task.save_task(
            task_input={"input_params": {}},
            task_output={"output_params": {}},
            code_str="",
            code_ser="",
            resources=PUBLIC_TASK_RESOURCES,
        )
        workflow.add_task(task_id, task)
    workflow.add_edge("parent", "child")
    workflow.graph.graph["file_context"] = {
        "enabled": True,
        "workspace_dir": str(tmp_path),
        "run_id": run_id,
    }

    static_run = StaticRun(run_id, "template", workflow)
    identity = {
        "workflow_id": run_id,
        "task_id": "parent",
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "node_id": "node-1",
    }
    static_run.mark_task_started("parent", identity)
    workflow.mark_task_started("parent")

    manifest = {
        "schema": "maze_task_file_manifest",
        "schema_version": 1,
        "run_id": run_id,
        "task_id": "parent",
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "published": False,
        "created_time": 123.0,
        "files": [],
        "deleted_files": [],
    }
    finish_message = {
        "type": "finish_task",
        "data": {
            **identity,
            "result": {"value": "done"},
            "metrics": {"duration_ms": 10},
            "file_manifest": manifest,
        },
    }

    class FailOnceStaticRunStore(StaticRunStore):
        def __init__(self, workspace_dir):
            super().__init__(workspace_dir)
            self.failed = False

        def save_run(self, snapshot):
            if not self.failed:
                self.failed = True
                raise OSError("injected snapshot failure")
            return super().save_run(snapshot)

    class ReplaySocket:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode("utf-8")
            self.calls = 0

        async def recv_multipart(self):
            self.calls += 1
            if self.calls == 1:
                return [b"scheduler", self.payload]
            raise asyncio.CancelledError

    class ResourceHistory:
        def __init__(self):
            self.calls = 0

        def record(self, **kwargs):
            self.calls += 1
            return {
                "run_id": kwargs["run_id"],
                "task_id": kwargs["task_id"],
                "status": kwargs["status"],
                "recorded_time": 456.0,
            }

    class Metrics:
        def on_task_finished(self, *_args, **_kwargs):
            return None

        def on_run_status_change(self, *_args, **_kwargs):
            return None

    path = object.__new__(MaPath)
    path.lock = asyncio.Lock()
    path.socket_from_scheduler = ReplaySocket(finish_message)
    path.cluster_resource_requests = {}
    path.cluster_queue_requests = {}
    path.worker_registration_requests = {}
    path.cluster_control_requests = {}
    path.llm_instance_async_que = {}
    path.dynamic_runs = {}
    path.static_runs = {run_id: static_run}
    path.submit_workflows = {run_id: workflow}
    path.async_que = {run_id: asyncio.Queue()}
    path.static_run_store = (
        FailOnceStaticRunStore(tmp_path / "run-store")
        if failure_stage == "snapshot"
        else StaticRunStore(tmp_path / "run-store")
    )
    path.resource_history = ResourceHistory()
    path.task_attempts = {}
    path.pre_dispatch_rejections = set()
    path.global_metrics = Metrics()
    path.strategy = "FCFS"
    path._observe_task_runtime = lambda *_args, **_kwargs: None
    path._task_run_payload = (
        lambda _workflow, task, workflow_id, _file_context: {
            "workflow_id": workflow_id,
            "task_id": task.task_id,
        }
    )

    async def priority(*_args, **_kwargs):
        return 0

    path._get_task_priority = priority
    sent_messages = []
    path._send_scheduler_message = sent_messages.append
    if failure_stage == "manifest":
        persist_manifest = path._persist_task_file_manifest
        manifest_calls = 0

        def fail_once(data, published_manifest):
            nonlocal manifest_calls
            manifest_calls += 1
            if manifest_calls == 1:
                raise OSError("injected manifest failure")
            return persist_manifest(data, published_manifest)

        monkeypatch.setattr(path, "_persist_task_file_manifest", fail_once)
    if failure_stage == "post_commit_continuation":
        persist_static_run = path._persist_static_run
        commit_transaction = path._commit_task_attempt_event_transaction
        committed = False
        continuation_failed = False

        def commit_then_arm(transaction):
            nonlocal committed
            commit_transaction(transaction)
            committed = True

        def fail_first_continuation_save(workflow_id):
            nonlocal continuation_failed
            if committed and not continuation_failed:
                continuation_failed = True
                raise OSError("injected post-commit continuation failure")
            return persist_static_run(workflow_id)

        monkeypatch.setattr(
            path,
            "_commit_task_attempt_event_transaction",
            commit_then_arm,
        )
        monkeypatch.setattr(
            path,
            "_persist_static_run",
            fail_first_continuation_save,
        )
    assert path._accept_task_attempt_event("start_task", identity)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(path.monitor_coroutine())

    assert path.socket_from_scheduler.calls == 2
    assert path.task_attempts[(run_id, "parent")]["state"] == "terminal"
    assert path.resource_history.calls == 1
    assert workflow.remaining_task_num == 1
    assert [message["type"] for message in sent_messages] == ["run_task"]
    assert path.async_que[run_id].qsize() == 1
    notification = path.async_que[run_id].get_nowait()
    assert notification["type"] == "finish_task"
    assert notification["data"]["file_manifest"]["published"] is True

    events = path.static_run_store.load_events(run_id)
    assert len(events) == 1
    assert events[0]["type"] == "finish_task"
    snapshot = path.static_run_store.load_run(run_id)
    assert snapshot["task_nodes"]["parent"]["status"] == "succeeded"
    assert snapshot["task_nodes"]["child"]["status"] == "queued"

    published_path = (
        tmp_path
        / "runs"
        / run_id
        / "file_manifests"
        / "tasks"
        / "parent.json"
    )
    published = json.loads(published_path.read_text(encoding="utf-8"))
    assert published["published"] is True
    assert published["lease_id"] == "lease-1"


@pytest.mark.parametrize("failure_stage", ["snapshot", "task_ready_event"])
def test_dynamic_finish_replay_after_snapshot_failure_is_idempotent(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    run_id = "replayed-dynamic-run"
    run = DynamicRun(
        run_id,
        file_context={
            "enabled": True,
            "workspace_dir": str(tmp_path),
            "run_id": run_id,
        },
    )
    parent, _ = run.append_task(
        DynamicTaskSpec(
            task_spec_id="parent-spec",
            task_name="parent",
            code_str="def parent(): return {'value': 'done'}",
            code_ser=None,
        )
    )
    child, _ = run.append_task(
        DynamicTaskSpec(
            task_spec_id="child-spec",
            task_name="child",
            code_str="def child(): return {}",
            code_ser=None,
        ),
        parents=[parent.task_id],
    )
    run.mark_started(parent.task_id)
    identity = {
        "workflow_id": run_id,
        "task_id": parent.task_id,
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "node_id": "node-1",
    }
    manifest = {
        "run_id": run_id,
        "task_id": parent.task_id,
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "published": False,
        "created_time": 123.0,
        "files": [],
    }
    message = {
        "type": "finish_task",
        "data": {
            **identity,
            "result": {"value": "done"},
            "file_manifest": manifest,
        },
    }

    class FailOnceDynamicRunStore(DynamicRunStore):
        def __init__(self, workspace_dir):
            super().__init__(workspace_dir)
            self.failed = False

        def save_run(self, snapshot):
            if not self.failed:
                self.failed = True
                raise OSError("injected dynamic snapshot failure")
            return super().save_run(snapshot)

    class ResourceHistory:
        def __init__(self):
            self.calls = 0

        def record(self, **kwargs):
            self.calls += 1
            return {
                "run_id": kwargs["run_id"],
                "task_id": kwargs["task_id"],
                "status": kwargs["status"],
                "recorded_time": 456.0,
            }

    class SingleDeliverySocket:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode("utf-8")
            self.calls = 0

        async def recv_multipart(self):
            self.calls += 1
            if self.calls == 1:
                return [b"scheduler", self.payload]
            raise asyncio.CancelledError

    path = object.__new__(MaPath)
    path.lock = asyncio.Lock()
    path.socket_from_scheduler = SingleDeliverySocket(message)
    path.cluster_resource_requests = {}
    path.cluster_queue_requests = {}
    path.worker_registration_requests = {}
    path.cluster_control_requests = {}
    path.llm_instance_async_que = {}
    path.dynamic_runs = {run_id: run}
    path.static_runs = {}
    path.submit_workflows = {}
    path.dynamic_run_store = (
        FailOnceDynamicRunStore(tmp_path / "run-store")
        if failure_stage == "snapshot"
        else DynamicRunStore(tmp_path / "run-store")
    )
    path.resource_history = ResourceHistory()
    path.runtime_estimator = RuntimeEstimator()
    path.task_attempts = {}
    path.pre_dispatch_rejections = set()
    path.async_que = {run_id: asyncio.Queue()}
    dispatched = []
    path._submit_dynamic_task = lambda task: dispatched.append(task.task_id)
    path._observe_task_runtime = lambda *_args, **_kwargs: None
    if failure_stage == "task_ready_event":
        emit_dynamic_event = path._emit_dynamic_event
        event_failed = False

        async def fail_first_task_ready_event(*args, **kwargs):
            nonlocal event_failed
            if not event_failed:
                event_failed = True
                raise OSError("injected post-commit task_ready event failure")
            return await emit_dynamic_event(*args, **kwargs)

        monkeypatch.setattr(
            path,
            "_emit_dynamic_event",
            fail_first_task_ready_event,
        )
    assert path._accept_task_attempt_event("start_task", identity)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(path.monitor_coroutine())

    assert path.socket_from_scheduler.calls == 2
    assert path.task_attempts[(run_id, parent.task_id)]["state"] == "terminal"
    assert path.resource_history.calls == 1
    assert dispatched == [child.task_id]
    events = path.dynamic_run_store.load_events(run_id)
    assert [event["type"] for event in events] == ["finish_task", "task_ready"]
    assert sum(event["type"] == "finish_task" for event in events) == 1
    published = json.loads(
        (
            tmp_path
            / "runs"
            / run_id
            / "file_manifests"
            / "tasks"
            / f"{parent.task_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert published["published"] is True
    assert published["lease_id"] == "lease-1"


def test_core_run_id_overrides_external_file_context_run_id(tmp_path):
    path = object.__new__(MaPath)
    path.resource_history = ResourceHistoryStore(tmp_path / "resource-history.json")
    path.runtime_estimator = RuntimeEstimator()

    workflow = Workflow("template")
    task = CodeTask("template", "task-1", "task-1")
    task.save_task(
        task_input={"input_params": {}},
        task_output={"output_params": {}},
        code_str="",
        code_ser="",
        resources=PUBLIC_TASK_RESOURCES,
    )
    workflow.add_task(task.task_id, task)
    external_context = {
        "enabled": True,
        "workspace_dir": str(tmp_path),
        "run_id": "playground-run",
    }

    static_payload = path._task_run_payload(
        workflow,
        task,
        "core-static-run",
        external_context,
    )
    dynamic_run = DynamicRun(
        "core-dynamic-run",
        file_context=external_context,
    )
    dynamic_task, _ = dynamic_run.append_task(
        DynamicTaskSpec(
            task_spec_id="task-spec",
            task_name="task",
            code_str="def task(): return {}",
            code_ser=None,
        )
    )
    prepared_context = path._prepare_initial_artifacts(
        external_context,
        "core-static-run",
    )
    dynamic_payload = path._dynamic_task_run_payload(dynamic_run, dynamic_task)

    assert prepared_context["run_id"] == "core-static-run"
    assert static_payload["file_context"]["run_id"] == "core-static-run"
    assert dynamic_payload["file_context"]["run_id"] == "core-dynamic-run"
    assert external_context["run_id"] == "playground-run"


def test_invalid_finish_manifest_becomes_terminal_artifact_failure(tmp_path):
    run_id = "core-run"
    workflow = Workflow("template")
    for task_id in ("parent", "child"):
        task = CodeTask("template", task_id, task_id)
        task.save_task(
            task_input={"input_params": {}},
            task_output={"output_params": {}},
            code_str="",
            code_ser="",
            resources=PUBLIC_TASK_RESOURCES,
        )
        workflow.add_task(task_id, task)
    workflow.add_edge("parent", "child")

    static_run = StaticRun(run_id, "template", workflow)
    identity = {
        "workflow_id": run_id,
        "task_id": "parent",
        "attempt": 1,
        "dispatch_id": "dispatch-1",
        "lease_id": "lease-1",
        "node_id": "node-1",
    }
    static_run.mark_task_started("parent", identity)
    workflow.mark_task_started("parent")
    finish_message = {
        "type": "finish_task",
        "data": {
            **identity,
            "result": {"value": "done"},
            "file_manifest": {
                "run_id": "playground-run",
                "task_id": "parent",
                "attempt": 1,
                "dispatch_id": "dispatch-1",
                "lease_id": "lease-1",
                "published": False,
                "files": [],
            },
        },
    }

    class SingleDeliverySocket:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode("utf-8")
            self.calls = 0

        async def recv_multipart(self):
            self.calls += 1
            if self.calls == 1:
                return [b"scheduler", self.payload]
            raise asyncio.CancelledError

    class Metrics:
        def on_task_finished(self, *_args, **_kwargs):
            return None

        def on_run_status_change(self, *_args, **_kwargs):
            return None

    path = object.__new__(MaPath)
    path.lock = asyncio.Lock()
    path.socket_from_scheduler = SingleDeliverySocket(finish_message)
    path.cluster_resource_requests = {}
    path.cluster_queue_requests = {}
    path.worker_registration_requests = {}
    path.cluster_control_requests = {}
    path.llm_instance_async_que = {}
    path.dynamic_runs = {}
    path.static_runs = {run_id: static_run}
    path.submit_workflows = {run_id: workflow}
    path.async_que = {run_id: asyncio.Queue()}
    path.static_run_store = StaticRunStore(tmp_path / "run-store")
    path.resource_history = ResourceHistoryStore(tmp_path / "resource-history.json")
    path.runtime_estimator = RuntimeEstimator()
    path.task_attempts = {}
    path.pre_dispatch_rejections = set()
    path.global_metrics = Metrics()
    path.strategy = "FCFS"
    path._observe_task_runtime = lambda *_args, **_kwargs: None
    sent_messages = []

    def send_scheduler_message(message):
        persisted = path.static_run_store.load_run(run_id)
        assert persisted["status"] == "failed"
        sent_messages.append(message)

    path._send_scheduler_message = send_scheduler_message
    assert path._accept_task_attempt_event("start_task", identity)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(path.monitor_coroutine())

    attempt = path.task_attempts[(run_id, "parent")]
    assert attempt["state"] == "terminal"
    assert attempt["event_type"] == "task_exception"
    assert static_run.status == "failed"
    assert static_run.task_nodes["parent"]["status"] == "failed"
    assert static_run.task_nodes["child"]["status"] == "pending"
    assert static_run.task_nodes["parent"]["file_manifest"] is None
    assert sent_messages == [{
        "type": "stop_workflow",
        "data": {"workflow_id": run_id},
    }]
    assert path.async_que[run_id].qsize() == 1
    failure = path.async_que[run_id].get_nowait()
    assert failure["type"] == "task_exception"
    assert failure["data"]["error"]["error_type"] == "artifact_error"
    assert failure["data"]["error"]["retryable"] is False
    assert "does not match" in failure["data"]["error"]["message"]
    assert "file_manifest" not in failure["data"]
    persisted = path.static_run_store.load_run(run_id)
    assert persisted["status"] == "failed"
    events = path.static_run_store.load_events(run_id)
    assert events[-1]["type"] == "task_exception"
    assert events[-1]["data"]["error"]["error_type"] == "artifact_error"
