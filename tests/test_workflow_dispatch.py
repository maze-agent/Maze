from types import SimpleNamespace

import pytest

from maze.core.path.path import (
    RUN_WORKFLOW_CLEANUP_MAX_ATTEMPTS,
    MaPath,
    WorkflowInitializationError,
)
from maze.core.scheduler.runtime_estimator import RuntimeEstimator
from maze.core.workflow.static_run import StaticRunStore
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


RUN_ID = "d4c98c23-e3f3-4df8-889f-41cab7e5f2f2"
TASK_RESOURCES = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}


def _workflow(task_count=1):
    workflow = Workflow("template")
    for index in range(task_count):
        task = CodeTask("template", f"task-{index}", "dispatch-test")
        task.save_task(
            task_input={"input_params": {}},
            task_output={"output_params": {}},
            code_str="",
            code_ser="",
            resources=TASK_RESOURCES,
            task_kind="cpu",
        )
        workflow.add_task(task.task_id, task)
    return workflow


def _path(store, task_count=1):
    path = object.__new__(MaPath)
    workflow = _workflow(task_count)
    path.workflows = {workflow.id: workflow}
    path.submit_workflows = {}
    path.async_que = {}
    path.static_runs = {}
    path.static_run_store = store
    path.strategy = "Default"
    path.resource_history = SimpleNamespace(
        apply=lambda resources, *_args: resources,
    )
    path.runtime_estimator = RuntimeEstimator()
    path.global_metrics = SimpleNamespace(
        on_run_submitted=lambda _run_id: None,
        on_run_status_change=lambda *_args: None,
    )
    path.scheduler_process = SimpleNamespace(
        is_alive=lambda: True,
        pid=123,
        exitcode=None,
    )
    messages = []
    path._send_scheduler_message = messages.append
    return path, messages


def test_submission_persists_dispatch_boundary_once(tmp_path, monkeypatch):
    store = StaticRunStore(tmp_path)
    path, messages = _path(store, task_count=2)
    states = []
    save_run = store.save_run

    def capture(snapshot):
        states.append(snapshot["dispatch"]["status"])
        save_run(snapshot)

    monkeypatch.setattr(store, "save_run", capture)

    assert path.run_workflow("template", run_id=RUN_ID) == RUN_ID

    assert states == ["prepared", "prepared", "dispatching", "active"]
    assert [message["type"] for message in messages] == ["run_task", "run_task"]
    assert store.load_run(RUN_ID)["dispatch"]["status"] == "active"


def test_first_snapshot_failure_never_dispatches(tmp_path, monkeypatch):
    store = StaticRunStore(tmp_path)
    path, messages = _path(store)

    def fail_save(_snapshot):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "save_run", fail_save)

    with pytest.raises(OSError, match="disk unavailable"):
        path.run_workflow("template", run_id=RUN_ID)

    assert messages == []
    assert path.static_runs == {}
    assert path.submit_workflows == {}


def test_partial_root_send_waits_for_scheduler_cleanup_ack(tmp_path):
    store = StaticRunStore(tmp_path)
    path, messages = _path(store, task_count=2)
    root_messages = 0

    def fail_second_root(message):
        nonlocal root_messages
        messages.append(message)
        if message["type"] == "run_task":
            root_messages += 1
            if root_messages == 2:
                raise OSError("injected root send failure")

    path._send_scheduler_message = fail_second_root

    with pytest.raises(WorkflowInitializationError):
        path.run_workflow("template", run_id=RUN_ID)

    pending = store.load_run(RUN_ID)
    dispatch = pending["dispatch"]
    assert pending["status"] == "created"
    assert dispatch["status"] == "cleanup_pending"
    assert [message["type"] for message in messages] == [
        "run_task",
        "run_task",
        "stop_workflow",
    ]

    path._handle_workflow_cleanup_response({
        "request_id": dispatch["cleanup_request_id"],
        "workflow_id": RUN_ID,
        "ok": True,
    })

    failed = store.load_run(RUN_ID)
    assert failed["status"] == "failed"
    assert failed["dispatch"]["status"] == "terminal"
    assert [event["type"] for event in store.load_events(RUN_ID)].count(
        "workflow_submission_failed"
    ) == 1


def test_restart_cleans_ambiguous_dispatch_without_redispatch(tmp_path):
    class SimulatedCrash(BaseException):
        pass

    store = StaticRunStore(tmp_path)
    path, messages = _path(store, task_count=2)

    def crash_after_delivery(message):
        messages.append(message)
        if message["type"] == "run_task":
            raise SimulatedCrash()

    path._send_scheduler_message = crash_after_delivery
    with pytest.raises(SimulatedCrash):
        path.run_workflow("template", run_id=RUN_ID)

    assert store.load_run(RUN_ID)["dispatch"]["status"] == "dispatching"
    assert store.recover_interrupted_runs() == []

    restarted, restart_messages = _path(store, task_count=2)
    assert restarted._recover_incomplete_workflow_dispatches() == [RUN_ID]
    pending = store.load_run(RUN_ID)["dispatch"]
    assert [message["type"] for message in restart_messages] == ["stop_workflow"]

    restarted._handle_workflow_cleanup_response({
        "request_id": pending["cleanup_request_id"],
        "workflow_id": RUN_ID,
        "ok": True,
    })
    assert store.load_run(RUN_ID)["status"] == "failed"
    assert all(message["type"] != "run_task" for message in restart_messages)


def test_unconfirmed_cleanup_is_bounded_and_stops_scheduler(tmp_path):
    store = StaticRunStore(tmp_path)
    path, messages = _path(store, task_count=2)
    root_messages = 0

    def fail_second_root(message):
        nonlocal root_messages
        messages.append(message)
        if message["type"] == "run_task":
            root_messages += 1
            if root_messages == 2:
                raise OSError("injected root send failure")

    path._send_scheduler_message = fail_second_root
    path._scheduler_shutdown_requested = False
    with pytest.raises(WorkflowInitializationError):
        path.run_workflow("template", run_id=RUN_ID)

    for _ in range(RUN_WORKFLOW_CLEANUP_MAX_ATTEMPTS):
        retry = next(iter(path.workflow_cleanup_retries.values()))
        retry["next_attempt"] = 0.0
        path._retry_pending_workflow_cleanups()

    failed = store.load_run(RUN_ID)
    assert failed["status"] == "failed"
    assert "could not be confirmed" in failed["error_summary"]["message"]
    assert any(message["type"] == "shutdown" for message in messages)
    assert path.workflow_cleanup_retries == {}
