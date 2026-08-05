import asyncio
import copy
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from maze.core.path.path import (
    MaPath,
    WorkflowIdempotencyConflictError,
    WorkflowIdempotencyStateError,
    WorkflowInitializationError,
    WorkflowNotFoundError,
)
from maze.core.files.artifact_store import LocalCASArtifactStore
from maze.core.scheduler.runtime_estimator import RuntimeEstimator
from maze.core.runs import GlobalMetrics
from maze.core.workflow.static_run import StaticRunStore
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64
TASK_RESOURCES = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}


def _workflow(workflow_id: str = "template") -> Workflow:
    workflow = Workflow(workflow_id)
    task = CodeTask(workflow_id, "task", "idempotency-test")
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


def _multi_root_workflow(
    workflow_id: str = "template",
    root_count: int = 3,
) -> Workflow:
    workflow = Workflow(workflow_id)
    for index in range(root_count):
        task_id = f"root-{index + 1}"
        task = CodeTask(workflow_id, task_id, task_id)
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


def _path_for(store, *workflows: Workflow, scheduler_alive: bool = True):
    path = object.__new__(MaPath)
    path.lock = asyncio.Lock()
    path.workflows = {workflow.id: workflow for workflow in workflows}
    path.submit_workflows = {}
    path.async_que = {}
    path.static_runs = {}
    path.dynamic_runs = {}
    path.task_attempts = {}
    path.pre_dispatch_rejections = set()
    path.llm_instance_async_que = {}
    path.cluster_resource_requests = {}
    path.cluster_queue_requests = {}
    path.worker_registration_requests = {}
    path.cluster_control_requests = {}
    path.idempotency_cleanup_requests = {}
    path.idempotency_cleanup_retries = {}
    path._scheduler_failure_handled = None
    path._scheduler_exit_progress = None
    path.strategy = "Default"
    path.static_run_store = store
    path.resource_history = SimpleNamespace(
        apply=lambda resources, *_args: resources,
    )
    path.runtime_estimator = RuntimeEstimator()
    path.global_metrics = SimpleNamespace(on_run_submitted=lambda _run_id: None)
    path.scheduler_process = SimpleNamespace(
        is_alive=lambda: scheduler_alive,
        pid=123,
        exitcode=None if scheduler_alive else 1,
    )
    sent_messages = []
    path._send_scheduler_message = sent_messages.append
    return path, sent_messages


def _cross_process_submit(
    workspace_dir,
    start_barrier,
    dispatch_entered,
    release_dispatch,
    result_queue,
):
    try:
        path, sent_messages = _path_for(
            StaticRunStore(workspace_dir),
            _workflow(),
        )

        def send(message):
            sent_messages.append(message)
            if message["type"] == "run_task":
                dispatch_entered.set()
                if not release_dispatch.wait(timeout=30):
                    raise TimeoutError("cross-process dispatch was not released")

        path._send_scheduler_message = send
        start_barrier.wait(timeout=30)
        run_id = path.run_workflow(
            "template",
            idempotency_key="submission-cross-process",
            idempotency_fingerprint=FINGERPRINT,
        )
        result_queue.put({
            "status": "ok",
            "run_id": run_id,
            "message_types": [message["type"] for message in sent_messages],
        })
    except BaseException as exc:
        result_queue.put({
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        })
        raise


def _core_constructor_probe(workspace_dir, release_event, result_queue):
    os.environ["MAZE_WORKSPACE_DIR"] = workspace_dir
    path = None
    try:
        path = MaPath()
        result_queue.put({"status": "acquired"})
        if not release_event.wait(timeout=20):
            raise TimeoutError("constructor lease release was not signaled")
    except BaseException as exc:
        result_queue.put({
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        })
    finally:
        if path is not None:
            path._release_core_process_lease()


class _RecordingStaticRunStore(StaticRunStore):
    def __init__(self, workspace_dir, actions):
        super().__init__(workspace_dir)
        self.actions = actions

    def save_run(self, snapshot):
        self.actions.append(("save", dict(snapshot)))
        super().save_run(snapshot)


class _FailNthSaveStore(StaticRunStore):
    def __init__(self, workspace_dir, fail_on):
        super().__init__(workspace_dir)
        self.fail_on = fail_on
        self.save_calls = 0

    def save_run(self, snapshot):
        self.save_calls += 1
        if self.save_calls == self.fail_on:
            raise OSError(f"injected save failure {self.fail_on}")
        super().save_run(snapshot)


class _FailAfterReservationStore(StaticRunStore):
    def __init__(self, workspace_dir):
        super().__init__(workspace_dir)
        self.save_calls = 0

    def save_run(self, snapshot):
        self.save_calls += 1
        if self.save_calls > 1:
            raise OSError("injected persistent storage outage")
        super().save_run(snapshot)


class _FailNextAppendStore(StaticRunStore):
    def __init__(self, workspace_dir):
        super().__init__(workspace_dir)
        self.fail_next_append = False

    def append_event(self, run_id, event):
        if self.fail_next_append:
            self.fail_next_append = False
            raise OSError("injected failure event append failure")
        super().append_event(run_id, event)


class _FailNextSaveStore(StaticRunStore):
    def __init__(self, workspace_dir):
        super().__init__(workspace_dir)
        self.fail_next_save = False

    def save_run(self, snapshot):
        if self.fail_next_save:
            self.fail_next_save = False
            raise OSError("injected failed snapshot save failure")
        super().save_run(snapshot)


class _FailFirstInitializationFailedSnapshotStore(StaticRunStore):
    def __init__(self, workspace_dir):
        super().__init__(workspace_dir)
        self.fail_failed_snapshot = False
        self.failed_once = False

    def append_event(self, run_id, event):
        super().append_event(run_id, event)
        if event.get("type") == "workflow_initialization_failed":
            self.fail_failed_snapshot = True

    def save_run(self, snapshot):
        if (
            self.fail_failed_snapshot
            and not self.failed_once
            and snapshot.get("status") == "failed"
        ):
            self.failed_once = True
            raise OSError("injected first initialization failed snapshot failure")
        super().save_run(snapshot)


def _start_cleanup_pending_run(store, *, keyed=False):
    path, messages = _path_for(store, _workflow())
    path.global_metrics = GlobalMetrics()

    def fail_root_send(message):
        if message["type"] == "run_task":
            raise OSError("ambiguous root dispatch")
        messages.append(copy.deepcopy(message))

    path._send_scheduler_message = fail_root_send
    kwargs = {}
    if keyed:
        kwargs = {
            "idempotency_key": "cleanup-contract",
            "idempotency_fingerprint": FINGERPRINT,
        }
    with pytest.raises(WorkflowInitializationError) as raised:
        path.run_workflow("template", **kwargs)
    return path, messages, raised.value.run_id


def _assert_initialization_failed(store, run_id):
    snapshot = store.load_run(run_id)
    assert snapshot["status"] == "failed"
    assert snapshot["error_summary"]["error_type"] == "workflow_initialization_failed"
    initialization = snapshot["idempotency_initialization"]
    assert initialization["status"] == "failed"
    assert initialization["error"] == snapshot["error_summary"]
    assert [entry["seq"] for entry in initialization["journal"]] == list(
        range(1, len(initialization["journal"]) + 1)
    )
    assert initialization["journal"][-1]["event"] == "failed"
    assert initialization["journal"][-1]["phase"] == initialization["phase"]
    assert all(
        task["status"] == "cancelled"
        for task in snapshot["task_nodes"].values()
    )
    return snapshot


def _assert_cleanup_pending(store, run_id):
    snapshot = store.load_run(run_id)
    initialization = snapshot["idempotency_initialization"]
    assert initialization["status"] == "cleanup_pending"
    assert initialization["phase"] == "cleanup"
    assert initialization["cleanup_request_id"]
    assert initialization["journal"][-1]["event"] == "cleanup_requested"
    assert snapshot["status"] in {"created", "interrupted"}
    return snapshot


def _ack_cleanup(path, store, run_id, *, ok=True):
    pending = _assert_cleanup_pending(store, run_id)
    request_id = pending["idempotency_initialization"]["cleanup_request_id"]
    path._handle_idempotent_workflow_cleanup_response({
        "request_id": request_id,
        "workflow_id": run_id,
        "ok": ok,
        "error": None if ok else "injected stop failure",
    })
    return request_id


def test_first_snapshot_reserves_identity_before_scheduler_dispatch(tmp_path):
    actions = []
    store = _RecordingStaticRunStore(tmp_path, actions)
    path, sent_messages = _path_for(store, _workflow())
    path._send_scheduler_message = lambda message: (
        actions.append(("send", message)),
        sent_messages.append(message),
    )[-1]

    run_id = path.run_workflow(
        "template",
        idempotency_key="submission-1",
        idempotency_fingerprint=FINGERPRINT,
    )

    assert actions[0][0] == "save"
    assert actions[0][1]["run_id"] == run_id
    assert actions[0][1]["workflow_id"] == "template"
    assert actions[0][1]["idempotency_key"] == "submission-1"
    assert actions[0][1]["idempotency_fingerprint"] == FINGERPRINT
    assert next(index for index, action in enumerate(actions) if action[0] == "send") > 0
    assert len(sent_messages) == 1
    snapshot = path.get_static_run_snapshot(run_id)
    assert snapshot["idempotency_key"] == "submission-1"
    initialization = snapshot["idempotency_initialization"]
    assert [entry["seq"] for entry in initialization["journal"]] == [1, 2, 3, 4, 5, 6]
    assert [entry["event"] for entry in initialization["journal"]] == [
        "reserved",
        "artifacts_ready",
        "event_recorded",
        "root_sending",
        "root_sent",
        "ready",
    ]
    assert initialization["root_dispatch"] == {"task": "sent"}


@pytest.mark.parametrize(
    ("key", "fingerprint", "message"),
    [
        ("submission-1", None, "must be provided together"),
        (None, None, "must be provided together"),
        (None, FINGERPRINT, "must be provided together"),
        (123, FINGERPRINT, "idempotency_key must be a string"),
        ("", FINGERPRINT, "non-empty string"),
        (" submission-1", FINGERPRINT, "surrounding whitespace"),
        ("submission\n1", FINGERPRINT, "control characters"),
        ("x" * 257, FINGERPRINT, "at most 256 UTF-8 bytes"),
        ("submission-1", 123, "idempotency_fingerprint must be a string"),
        ("submission-1", "A" * 64, "64 lowercase hexadecimal"),
        ("submission-1", "a" * 63, "64 lowercase hexadecimal"),
        ("submission-1", "g" * 64, "64 lowercase hexadecimal"),
    ],
)
def test_invalid_identity_is_rejected_before_run_creation_or_dispatch(
    tmp_path,
    key,
    fingerprint,
    message,
):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    with pytest.raises((TypeError, ValueError), match=message):
        path.run_workflow(
            "template",
            idempotency_key=key,
            idempotency_fingerprint=fingerprint,
        )

    assert path.static_runs == {}
    assert path.submit_workflows == {}
    assert path.async_que == {}
    assert store.list_runs() == []
    assert sent_messages == []


def test_unknown_workflow_is_not_found_before_run_creation_or_dispatch(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    with pytest.raises(WorkflowNotFoundError) as error:
        path.run_workflow(
            "missing-template",
            idempotency_key="submission-missing-workflow",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert error.value.detail() == {
        "code": "workflow_not_found",
        "message": "Workflow not found: missing-template",
        "workflow_id": "missing-template",
    }
    assert path.static_runs == {}
    assert store.list_runs() == []
    assert sent_messages == []


def test_same_identity_reuses_run_without_duplicate_scheduler_message(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    first_run_id = path.run_workflow(
        "template",
        idempotency_key="submission-1",
        idempotency_fingerprint=FINGERPRINT,
    )
    replayed_run_id = path.run_workflow(
        "template",
        idempotency_key="submission-1",
        idempotency_fingerprint=FINGERPRINT,
    )

    assert replayed_run_id == first_run_id
    assert len(sent_messages) == 1
    assert list(path.static_runs) == [first_run_id]
    assert [snapshot["run_id"] for snapshot in store.list_runs()] == [first_run_id]


def test_successful_multi_root_initialization_dispatches_every_root_once(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _multi_root_workflow())

    run_id = path.run_workflow(
        "template",
        idempotency_key="submission-multi-root",
        idempotency_fingerprint=FINGERPRINT,
    )
    replayed_run_id = path.run_workflow(
        "template",
        idempotency_key="submission-multi-root",
        idempotency_fingerprint=FINGERPRINT,
    )

    assert replayed_run_id == run_id
    assert [message["data"]["task_id"] for message in sent_messages] == [
        "root-1",
        "root-2",
        "root-3",
    ]
    initialization = store.load_run(run_id)["idempotency_initialization"]
    assert initialization["root_dispatch"] == {
        "root-1": "sent",
        "root-2": "sent",
        "root-3": "sent",
    }
    assert [
        (entry["event"], entry.get("task_id"))
        for entry in initialization["journal"]
        if entry["event"].startswith("root_")
    ] == [
        ("root_sending", "root-1"),
        ("root_sent", "root-1"),
        ("root_sending", "root-2"),
        ("root_sent", "root-2"),
        ("root_sending", "root-3"),
        ("root_sent", "root-3"),
    ]


def test_maximum_length_key_is_accepted(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    run_id = path.run_workflow(
        "template",
        idempotency_key="x" * 256,
        idempotency_fingerprint=FINGERPRINT,
    )

    assert store.load_run(run_id)["idempotency_key"] == "x" * 256
    assert len(sent_messages) == 1


@pytest.mark.parametrize(
    ("workflow_id", "fingerprint"),
    [
        ("template", OTHER_FINGERPRINT),
        ("other-template", FINGERPRINT),
    ],
    ids=["fingerprint-conflict", "workflow-conflict"],
)
def test_same_key_with_different_binding_conflicts_without_new_run(
    tmp_path,
    workflow_id,
    fingerprint,
):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow(), _workflow("other-template"))
    first_run_id = path.run_workflow(
        "template",
        idempotency_key="submission-1",
        idempotency_fingerprint=FINGERPRINT,
    )

    with pytest.raises(WorkflowIdempotencyConflictError) as error:
        path.run_workflow(
            workflow_id,
            idempotency_key="submission-1",
            idempotency_fingerprint=fingerprint,
        )

    assert error.value.detail()["code"] == "workflow_idempotency_conflict"
    assert error.value.detail()["existing_run_id"] == first_run_id
    assert len(sent_messages) == 1
    assert list(path.static_runs) == [first_run_id]
    assert len(store.list_runs()) == 1


@pytest.mark.parametrize(
    "changed_payload",
    [
        {"file_context": {"enabled": False}},
        {"timeout_seconds": 30},
        {"tags": ["changed"]},
        {"metadata": {"source": "changed"}},
        {"final_output_refs": None},
        {"inputs": {}},
    ],
)
def test_same_client_fingerprint_cannot_mask_changed_submission_payload(
    tmp_path,
    changed_payload,
):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())
    run_id = path.run_workflow(
        "template",
        idempotency_key="submission-payload-binding",
        idempotency_fingerprint=FINGERPRINT,
    )

    with pytest.raises(WorkflowIdempotencyConflictError) as error:
        path.run_workflow(
            "template",
            idempotency_key="submission-payload-binding",
            idempotency_fingerprint=FINGERPRINT,
            **changed_payload,
        )

    assert error.value.existing_run_id == run_id
    assert len(sent_messages) == 1
    assert len(store.list_runs()) == 1


def test_concurrent_same_identity_creates_and_dispatches_exactly_once(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    def submit(_index):
        return path.run_workflow(
            "template",
            idempotency_key="submission-concurrent",
            idempotency_fingerprint=FINGERPRINT,
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        run_ids = list(pool.map(submit, range(24)))

    assert len(set(run_ids)) == 1
    assert len(sent_messages) == 1
    assert len(path.static_runs) == 1
    assert len(store.list_runs()) == 1


def test_two_path_instances_share_store_lock_and_dispatch_exactly_once(tmp_path):
    first_path, first_messages = _path_for(
        StaticRunStore(tmp_path),
        _workflow(),
    )
    second_path, second_messages = _path_for(
        StaticRunStore(tmp_path),
        _workflow(),
    )
    barrier = threading.Barrier(2)

    def submit(path):
        barrier.wait()
        return path.run_workflow(
            "template",
            idempotency_key="submission-two-paths",
            idempotency_fingerprint=FINGERPRINT,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, path) for path in (first_path, second_path)]
        run_ids = [future.result() for future in futures]

    assert len(set(run_ids)) == 1
    assert len(first_messages) + len(second_messages) == 1
    assert len(StaticRunStore(tmp_path).list_runs()) == 1


def test_two_processes_claim_shared_store_and_dispatch_exactly_once(tmp_path):
    context = multiprocessing.get_context("spawn")
    start_barrier = context.Barrier(2)
    dispatch_entered = context.Event()
    release_dispatch = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_cross_process_submit,
            args=(
                str(tmp_path),
                start_barrier,
                dispatch_entered,
                release_dispatch,
                result_queue,
            ),
        )
        for _ in range(2)
    ]

    try:
        for process in processes:
            process.start()
        assert dispatch_entered.wait(timeout=30)
        release_dispatch.set()
        for process in processes:
            process.join(timeout=30)
    finally:
        release_dispatch.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    results = [result_queue.get(timeout=10) for _ in processes]
    assert {result["status"] for result in results} == {"ok"}
    assert len({result["run_id"] for result in results}) == 1
    assert sorted(result["message_types"] for result in results) == [
        [],
        ["run_task"],
    ]
    snapshots = StaticRunStore(tmp_path).list_runs()
    assert len(snapshots) == 1
    assert snapshots[0]["idempotency_initialization"]["status"] == "ready"
    assert snapshots[0]["idempotency_initialization"]["root_dispatch"] == {
        "task": "sent"
    }


def test_core_constructor_lease_is_held_for_the_process_lifetime(tmp_path):
    context = multiprocessing.get_context("spawn")
    owner_release = context.Event()
    contender_release = context.Event()
    contender_release.set()
    result_queue = context.Queue()
    owner = context.Process(
        target=_core_constructor_probe,
        args=(str(tmp_path), owner_release, result_queue),
    )
    contender = context.Process(
        target=_core_constructor_probe,
        args=(str(tmp_path), contender_release, result_queue),
    )

    try:
        owner.start()
        assert result_queue.get(timeout=20) == {"status": "acquired"}
        contender.start()
        rejected = result_queue.get(timeout=20)
        assert rejected["status"] == "error"
        assert rejected["error_type"] == "RuntimeError"
        assert "Another Maze Core process owns workflow store" in rejected["message"]
    finally:
        owner_release.set()
        for process in (owner, contender):
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert owner.exitcode == 0
    assert contender.exitcode == 0


def test_metrics_failure_terminates_reserved_run_and_replay_is_explicit(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    class Metrics:
        def __init__(self):
            self.status_changes = []

        def on_run_submitted(self, _run_id):
            raise RuntimeError("metrics failed")

        def on_run_status_change(self, *args):
            self.status_changes.append(args)

    path.global_metrics = Metrics()
    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow(
            "template",
            idempotency_key="submission-metrics-failure",
            idempotency_fingerprint=FINGERPRINT,
        )

    run_id = error.value.run_id
    snapshot = _assert_initialization_failed(store, run_id)
    assert snapshot["error_summary"]["phase"] == "metrics"
    assert sent_messages == []
    assert path.global_metrics.status_changes == [
        (run_id, "submitted", "failed")
    ]

    with pytest.raises(WorkflowInitializationError) as replay:
        path.run_workflow(
            "template",
            idempotency_key="submission-metrics-failure",
            idempotency_fingerprint=FINGERPRINT,
        )
    assert replay.value.run_id == run_id
    assert sent_messages == []


def test_event_failure_terminates_before_any_root_dispatch(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _multi_root_workflow())

    def fail_event(*_args, **_kwargs):
        raise OSError("event persistence failed")

    path._record_static_event = fail_event

    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow(
            "template",
            idempotency_key="submission-event-failure",
            idempotency_fingerprint=FINGERPRINT,
        )

    snapshot = _assert_initialization_failed(store, error.value.run_id)
    assert snapshot["error_summary"]["phase"] == "event"
    assert snapshot["idempotency_initialization"]["root_dispatch"] == {
        "root-1": "pending",
        "root-2": "pending",
        "root-3": "pending",
    }
    assert sent_messages == []


def test_torn_event_phase_snapshot_is_recovered_without_permanent_state_error(
    tmp_path,
):
    class SimulatedProcessCrash(BaseException):
        pass

    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    def persist_legacy_window_then_crash(run_id, *_args, **_kwargs):
        path._persist_static_run(run_id)
        raise SimulatedProcessCrash()

    path._record_static_event = persist_legacy_window_then_crash
    with pytest.raises(SimulatedProcessCrash):
        path.run_workflow(
            "template",
            idempotency_key="submission-torn-event-window",
            idempotency_fingerprint=FINGERPRINT,
        )

    snapshot = store.list_runs()[0]
    run_id = snapshot["run_id"]
    initialization = snapshot["idempotency_initialization"]
    assert initialization["phase"] == "event"
    assert initialization["journal"][-1]["event"] == "artifacts_ready"
    assert sent_messages == []

    restarted, restarted_messages = _path_for(store, _workflow())
    with pytest.raises(WorkflowInitializationError) as replay:
        restarted.run_workflow(
            "template",
            idempotency_key="submission-torn-event-window",
            idempotency_fingerprint=FINGERPRINT,
        )
    assert replay.value.run_id == run_id
    assert restarted_messages == []
    _assert_initialization_failed(store, run_id)


@pytest.mark.parametrize("failed_root_index", [0, 1, 2])
def test_root_send_failure_never_redispatches_confirmed_or_ambiguous_roots(
    tmp_path,
    failed_root_index,
):
    store = StaticRunStore(tmp_path)
    path, _ = _path_for(store, _multi_root_workflow())
    messages = []
    run_task_attempts = 0

    def send(message):
        nonlocal run_task_attempts
        if message["type"] == "run_task":
            current_index = run_task_attempts
            run_task_attempts += 1
            if current_index == failed_root_index:
                raise OSError(f"root send {current_index} failed")
        messages.append(message)

    path._send_scheduler_message = send
    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow(
            "template",
            idempotency_key=f"submission-send-{failed_root_index}",
            idempotency_fingerprint=FINGERPRINT,
        )

    snapshot = _assert_cleanup_pending(store, error.value.run_id)
    dispatch = snapshot["idempotency_initialization"]["root_dispatch"]
    for index in range(failed_root_index):
        assert dispatch[f"root-{index + 1}"] == "sent"
    assert dispatch[f"root-{failed_root_index + 1}"] == "sending"
    for index in range(failed_root_index + 1, 3):
        assert dispatch[f"root-{index + 1}"] == "pending"
    assert len([message for message in messages if message["type"] == "run_task"]) == failed_root_index
    assert [message["type"] for message in messages][-1] == "stop_workflow"

    run_task_messages = [
        message for message in messages if message["type"] == "run_task"
    ]
    with pytest.raises(WorkflowInitializationError) as replay:
        path.run_workflow(
            "template",
            idempotency_key=f"submission-send-{failed_root_index}",
            idempotency_fingerprint=FINGERPRINT,
        )
    assert replay.value.run_id == error.value.run_id
    assert [
        message for message in messages if message["type"] == "run_task"
    ] == run_task_messages
    assert [message["type"] for message in messages].count("stop_workflow") == 2
    _ack_cleanup(path, store, error.value.run_id)
    _assert_initialization_failed(store, error.value.run_id)


def test_failed_scheduler_cleanup_ack_keeps_run_pending_until_successful_ack(
    tmp_path,
):
    store = StaticRunStore(tmp_path)
    path, messages = _path_for(store, _workflow())

    def fail_root_send(message):
        if message["type"] == "run_task":
            raise OSError("ambiguous root send")
        messages.append(message)

    path._send_scheduler_message = fail_root_send
    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow(
            "template",
            idempotency_key="submission-cleanup-ack",
            idempotency_fingerprint=FINGERPRINT,
        )

    run_id = error.value.run_id
    request_id = _ack_cleanup(path, store, run_id, ok=False)
    still_pending = _assert_cleanup_pending(store, run_id)
    assert (
        still_pending["idempotency_initialization"]["cleanup_request_id"]
        == request_id
    )
    assert [
        entry["event"]
        for entry in still_pending["idempotency_initialization"]["journal"]
    ].count("cleanup_requested") == 1

    _ack_cleanup(path, store, run_id, ok=True)
    failed = _assert_initialization_failed(store, run_id)
    assert [
        entry["event"]
        for entry in failed["idempotency_initialization"]["journal"]
    ][-3:] == ["cleanup_requested", "cleanup_confirmed", "failed"]


def test_final_ready_save_failure_terminates_all_sent_roots(tmp_path):
    # Reservation + event snapshots + two saves per root = 9; save 10 is ready.
    store = _FailNthSaveStore(tmp_path, fail_on=10)
    path, sent_messages = _path_for(store, _multi_root_workflow())

    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow(
            "template",
            idempotency_key="submission-final-save",
            idempotency_fingerprint=FINGERPRINT,
        )

    snapshot = _assert_cleanup_pending(store, error.value.run_id)
    assert snapshot["idempotency_initialization"]["error"]["phase"] == "ready"
    assert snapshot["idempotency_initialization"]["root_dispatch"] == {
        "root-1": "sent",
        "root-2": "sent",
        "root-3": "sent",
    }
    assert [message["type"] for message in sent_messages] == [
        "run_task",
        "run_task",
        "run_task",
        "stop_workflow",
    ]
    _ack_cleanup(path, store, error.value.run_id)
    _assert_initialization_failed(store, error.value.run_id)

    restarted, restarted_messages = _path_for(
        StaticRunStore(tmp_path),
        _multi_root_workflow(),
    )
    with pytest.raises(WorkflowInitializationError) as replay:
        restarted.run_workflow(
            "template",
            idempotency_key="submission-final-save",
            idempotency_fingerprint=FINGERPRINT,
        )
    assert replay.value.run_id == error.value.run_id
    assert restarted_messages == []


def test_post_send_confirmation_save_failure_never_resends_confirmed_root(tmp_path):
    # Reservation, event snapshots, root-1 sending, then root-1 confirmation.
    store = _FailNthSaveStore(tmp_path, fail_on=5)
    path, sent_messages = _path_for(store, _multi_root_workflow())

    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow(
            "template",
            idempotency_key="submission-confirm-save",
            idempotency_fingerprint=FINGERPRINT,
        )

    snapshot = _assert_cleanup_pending(store, error.value.run_id)
    assert (
        snapshot["idempotency_initialization"]["error"]["phase"]
        == "root_dispatch:root-1:confirming"
    )
    assert snapshot["idempotency_initialization"]["root_dispatch"] == {
        "root-1": "sent",
        "root-2": "pending",
        "root-3": "pending",
    }
    assert [message["type"] for message in sent_messages] == [
        "run_task",
        "stop_workflow",
    ]

    before_run_tasks = [
        message for message in sent_messages if message["type"] == "run_task"
    ]
    with pytest.raises(WorkflowInitializationError):
        path.run_workflow(
            "template",
            idempotency_key="submission-confirm-save",
            idempotency_fingerprint=FINGERPRINT,
        )
    assert [
        message for message in sent_messages if message["type"] == "run_task"
    ] == before_run_tasks
    assert [message["type"] for message in sent_messages].count("stop_workflow") == 2
    _ack_cleanup(path, store, error.value.run_id)
    _assert_initialization_failed(store, error.value.run_id)


def test_restart_terminates_ambiguous_sending_snapshot_without_redispatch(tmp_path):
    class SimulatedProcessCrash(BaseException):
        pass

    store = StaticRunStore(tmp_path)
    path, _ = _path_for(store, _workflow())
    delivered_messages = []

    def deliver_then_crash(message):
        delivered_messages.append(message)
        if message["type"] == "run_task":
            raise SimulatedProcessCrash()

    path._send_scheduler_message = deliver_then_crash
    with pytest.raises(SimulatedProcessCrash):
        path.run_workflow(
            "template",
            idempotency_key="submission-crash",
            idempotency_fingerprint=FINGERPRINT,
        )

    reserved = store.list_runs()[0]
    run_id = reserved["run_id"]
    assert reserved["status"] == "created"
    assert reserved["idempotency_initialization"]["status"] == "initializing"
    assert reserved["idempotency_initialization"]["root_dispatch"] == {
        "task": "sending"
    }
    store.recover_interrupted_runs()

    restarted, restarted_messages = _path_for(
        StaticRunStore(tmp_path),
        _workflow(),
    )
    with pytest.raises(WorkflowInitializationError) as replay:
        restarted.run_workflow(
            "template",
            idempotency_key="submission-crash",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert replay.value.run_id == run_id
    assert [message["type"] for message in delivered_messages] == ["run_task"]
    assert [message["type"] for message in restarted_messages] == ["stop_workflow"]
    _ack_cleanup(restarted, StaticRunStore(tmp_path), run_id)
    _assert_initialization_failed(StaticRunStore(tmp_path), run_id)


def test_concurrent_retry_waits_for_failed_initialization_and_does_not_dispatch(
    tmp_path,
):
    entered_metrics = threading.Event()
    release_metrics = threading.Event()
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    class BlockingFailMetrics:
        def on_run_submitted(self, _run_id):
            entered_metrics.set()
            assert release_metrics.wait(timeout=5)
            raise RuntimeError("metrics failed after concurrent retry arrived")

        def on_run_status_change(self, *_args):
            return None

    path.global_metrics = BlockingFailMetrics()

    def submit():
        return path.run_workflow(
            "template",
            idempotency_key="submission-concurrent-failure",
            idempotency_fingerprint=FINGERPRINT,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit)
        assert entered_metrics.wait(timeout=5)
        second = pool.submit(submit)
        release_metrics.set()
        for future in (first, second):
            with pytest.raises(WorkflowInitializationError):
                future.result()

    assert len(store.list_runs()) == 1
    assert sent_messages == []


def test_retry_during_storage_outage_does_not_append_duplicate_failure(tmp_path):
    store = _FailAfterReservationStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    with pytest.raises(WorkflowInitializationError) as first_error:
        path.run_workflow(
            "template",
            idempotency_key="submission-storage-outage",
            idempotency_fingerprint=FINGERPRINT,
        )
    run_id = first_error.value.run_id
    journal_before_retry = list(
        path._static_run_snapshot(path.static_runs[run_id])[
            "idempotency_initialization"
        ]["journal"]
    )

    with pytest.raises(WorkflowInitializationError) as retry_error:
        path.run_workflow(
            "template",
            idempotency_key="submission-storage-outage",
            idempotency_fingerprint=FINGERPRINT,
        )

    journal_after_retry = path._static_run_snapshot(path.static_runs[run_id])[
        "idempotency_initialization"
    ]["journal"]
    assert retry_error.value.run_id == run_id
    assert journal_after_retry == journal_before_retry
    assert [entry["event"] for entry in journal_after_retry].count("failed") == 0
    assert path.static_runs[run_id].status == "created"
    assert store.load_run(run_id)["status"] == "created"
    assert [
        event["type"] for event in store.load_events(run_id)
    ].count("workflow_initialization_failed") == 1
    assert "run_task" not in [message["type"] for message in sent_messages]


def test_corrupt_persistent_run_fails_closed_before_claim_or_dispatch(tmp_path):
    store = StaticRunStore(tmp_path)
    corrupt_dir = store.runs_dir / "corrupt-run"
    corrupt_dir.mkdir()
    (corrupt_dir / "run.json").write_text("{not-json", encoding="utf-8")
    path, sent_messages = _path_for(store, _workflow())

    with pytest.raises(WorkflowIdempotencyStateError, match="could not be verified"):
        path.run_workflow(
            "template",
            idempotency_key="submission-after-corruption",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert path.static_runs == {}
    assert sent_messages == []


def test_partial_persistent_identity_fails_closed(tmp_path):
    store = StaticRunStore(tmp_path)
    store.save_run({
        "run_id": "partial-run",
        "workflow_id": "template",
        "status": "failed",
        "idempotency_key": "submission-partial",
    })
    path, sent_messages = _path_for(store, _workflow())

    with pytest.raises(WorkflowIdempotencyStateError, match="partial idempotency"):
        path.run_workflow(
            "template",
            idempotency_key="submission-partial",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert path.static_runs == {}
    assert sent_messages == []


def test_duplicate_persistent_claims_fail_closed(tmp_path):
    store = StaticRunStore(tmp_path)
    for run_id in ("duplicate-a", "duplicate-b"):
        store.save_run({
            "run_id": run_id,
            "workflow_id": "template",
            "status": "succeeded",
            "idempotency_key": "submission-duplicate",
            "idempotency_fingerprint": FINGERPRINT,
        })
    path, sent_messages = _path_for(store, _workflow())

    with pytest.raises(WorkflowIdempotencyStateError, match="multiple stored runs"):
        path.run_workflow(
            "template",
            idempotency_key="submission-duplicate",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert path.static_runs == {}
    assert sent_messages == []


def test_corrupt_in_memory_claim_fails_closed(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())
    path._run_workflow_idempotency_index = {
        "submission-corrupt-index": {"run_id": "missing-fields"},
    }

    with pytest.raises(WorkflowIdempotencyStateError, match="index is incomplete"):
        path.run_workflow(
            "template",
            idempotency_key="submission-after-corrupt-index",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert path.static_runs == {}
    assert sent_messages == []


def test_stale_valid_shaped_cache_fails_closed_for_an_unrelated_key(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())
    path._run_workflow_idempotency_index = {
        "submission-stale-index": {
            "idempotency_key": "submission-stale-index",
            "idempotency_fingerprint": FINGERPRINT,
            "workflow_id": "template",
            "run_id": "missing-run",
        },
    }

    with pytest.raises(WorkflowIdempotencyStateError, match="does not match persistent"):
        path.run_workflow(
            "template",
            idempotency_key="submission-after-stale-index",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert path.static_runs == {}
    assert store.list_runs() == []
    assert sent_messages == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("seq", 99, "journal sequence"),
        ("phase", "wrong-phase", "journal transitions"),
    ],
)
def test_corrupt_initialization_journal_fails_closed(
    tmp_path,
    field,
    value,
    message,
):
    store = StaticRunStore(tmp_path)
    path, _ = _path_for(store, _workflow())
    run_id = path.run_workflow(
        "template",
        idempotency_key="submission-corrupt-journal",
        idempotency_fingerprint=FINGERPRINT,
    )
    snapshot = store.load_run(run_id)
    snapshot["idempotency_initialization"]["journal"][1][field] = value
    store.save_run(snapshot)
    restarted_path, sent_messages = _path_for(store, _workflow())

    with pytest.raises(WorkflowIdempotencyStateError, match=message):
        restarted_path.run_workflow(
            "template",
            idempotency_key="submission-after-corrupt-journal",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert restarted_path.static_runs == {}
    assert sent_messages == []


def test_persistent_identity_is_reused_after_restart_without_live_scheduler(tmp_path):
    first_store = StaticRunStore(tmp_path)
    first_path, first_messages = _path_for(first_store, _workflow())
    first_run_id = first_path.run_workflow(
        "template",
        idempotency_key="submission-restart",
        idempotency_fingerprint=FINGERPRINT,
    )
    assert len(first_messages) == 1

    first_store.recover_interrupted_runs()
    restarted_store = StaticRunStore(tmp_path)
    restarted_path, restarted_messages = _path_for(
        restarted_store,
        _workflow(),
        scheduler_alive=False,
    )

    replayed_run_id = restarted_path.run_workflow(
        "template",
        idempotency_key="submission-restart",
        idempotency_fingerprint=FINGERPRINT,
    )

    assert replayed_run_id == first_run_id
    assert restarted_messages == []
    assert restarted_path.static_runs == {}
    snapshot = restarted_store.load_run(first_run_id)
    assert snapshot["status"] == "interrupted"
    assert snapshot["idempotency_key"] == "submission-restart"
    assert snapshot["idempotency_fingerprint"] == FINGERPRINT



@pytest.mark.parametrize("with_idempotency", [False, True])
@pytest.mark.parametrize(
    ("file_context", "message"),
    [
        ([], "file_context must be an object"),
        ({"artifact_store": []}, "file_context.artifact_store must be an object"),
        (
            {"enabled": False, "artifact_store": []},
            "file_context.artifact_store must be an object",
        ),
        ({"enabled": True}, "file_context.workspace_dir is required"),
        (
            {"enabled": True, "workspace_dir": ""},
            "file_context.workspace_dir is required",
        ),
        (
            {
                "enabled": True,
                "workspace_dir": "/tmp/maze-invalid-file-context",
                "artifact_store": {"base_url": "ftp://artifact.invalid"},
            },
            "file_context.artifact_store.base_url must be an absolute http(s) URL",
        ),
        (
            {
                "enabled": True,
                "workspace_dir": "/tmp/maze-invalid-file-context",
                "task_node_ids": [],
            },
            "file_context.task_node_ids must be an object",
        ),
    ],
)
def test_invalid_file_context_is_rejected_before_claim_or_dispatch(
    tmp_path,
    with_idempotency,
    file_context,
    message,
):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())
    kwargs = {}
    if with_idempotency:
        kwargs = {
            "idempotency_key": "invalid-file-context",
            "idempotency_fingerprint": FINGERPRINT,
        }

    with pytest.raises((TypeError, ValueError)) as error:
        path.run_workflow(
            "template",
            file_context=file_context,
            **kwargs,
        )

    assert message in str(error.value)
    assert path.static_runs == {}
    assert path.submit_workflows == {}
    assert path.async_que == {}
    assert store.list_runs() == []
    assert list(store.runs_dir.iterdir()) == []
    assert sent_messages == []



@pytest.mark.parametrize(
    ("endpoint_name", "payload", "parser_name", "run_id"),
    [
        ("run_app", {"spec": {}}, "app_spec_from_payload", None),
        (
            "submit_dag_workflow",
            {"spec": {}},
            "dag_spec_from_payload",
            None,
        ),
        ("create_dynamic_run", {}, None, None),
        ("retry_run", {}, "app_spec_from_payload", "existing-run"),
    ],
)
@pytest.mark.parametrize(
    ("file_context", "message"),
    [
        ([], "file_context must be an object"),
        (
            {"artifact_store": []},
            "file_context.artifact_store must be an object",
        ),
    ],
)
def test_all_run_endpoints_reject_file_context_before_side_effects(
    monkeypatch,
    endpoint_name,
    payload,
    parser_name,
    run_id,
    file_context,
    message,
):
    from maze.core import server

    calls = []

    class Request:
        async def json(self):
            return {**payload, "file_context": file_context}

    class Path:
        def create_app_workflow(self, *_args, **_kwargs):
            calls.append("create_app_workflow")
            return "app-workflow"

        def create_dag_workflow(self, *_args, **_kwargs):
            calls.append("create_dag_workflow")
            return "dag-workflow"

        def get_workflow(self, *_args, **_kwargs):
            calls.append("get_workflow")
            return SimpleNamespace(tasks={})

        def run_workflow(self, *_args, **_kwargs):
            calls.append("run_workflow")
            return "static-run"

        async def create_dynamic_run(self, *_args, **_kwargs):
            calls.append("create_dynamic_run")
            return "dynamic-run"

        async def get_cluster_resources(self, *_args, **_kwargs):
            calls.append("get_cluster_resources")
            return {}

        async def get_run_snapshot(self, requested_run_id):
            assert requested_run_id == "existing-run"
            return {
                "metadata": {"app_spec": {"name": "validation-test"}},
                "tags": [],
            }

    if parser_name is not None:
        monkeypatch.setattr(
            server,
            parser_name,
            lambda *_args, **_kwargs: {
                "name": "validation-test",
                "metadata": {},
                "tags": [],
            },
        )
    monkeypatch.setattr(server, "mapath", Path())

    with pytest.raises(HTTPException) as error:
        args = (run_id, Request()) if run_id is not None else (Request(),)
        asyncio.run(getattr(server, endpoint_name)(*args))

    assert error.value.status_code == 400
    assert error.value.detail == message
    assert calls == []


@pytest.mark.parametrize("file_context", [[], {"artifact_store": []}])
def test_dynamic_run_validates_file_context_before_scheduler_or_state_mutation(
    file_context,
):
    path = object.__new__(MaPath)
    path.dynamic_runs = {}
    path.async_que = {}
    calls = []
    path._require_scheduler_available = lambda: calls.append("scheduler")
    path._prepare_initial_artifacts = (
        lambda *_args, **_kwargs: calls.append("artifacts")
    )

    with pytest.raises((TypeError, ValueError)):
        asyncio.run(path.create_dynamic_run(file_context=file_context))

    assert calls == []
    assert path.dynamic_runs == {}
    assert path.async_que == {}


def test_non_idempotent_success_keeps_public_semantics_and_durable_journal(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _multi_root_workflow())

    first_run_id = path.run_workflow("template")
    second_run_id = path.run_workflow("template")

    assert first_run_id != second_run_id
    assert isinstance(first_run_id, str) and first_run_id
    assert isinstance(second_run_id, str) and second_run_id
    assert [message["data"]["task_id"] for message in sent_messages] == [
        "root-1",
        "root-2",
        "root-3",
        "root-1",
        "root-2",
        "root-3",
    ]
    for run_id in (first_run_id, second_run_id):
        snapshot = store.load_run(run_id)
        assert "idempotency_key" not in snapshot
        assert "idempotency_fingerprint" not in snapshot
        assert "idempotency_payload_fingerprint" not in snapshot
        assert snapshot["idempotency_initialization"]["status"] == "ready"
        assert snapshot["idempotency_initialization"]["root_dispatch"] == {
            "root-1": "sent",
            "root-2": "sent",
            "root-3": "sent",
        }


def test_non_idempotent_metrics_failure_is_terminal_before_dispatch(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())

    class Metrics:
        def __init__(self):
            self.status_changes = []

        def on_run_submitted(self, _run_id):
            raise RuntimeError("metrics failed")

        def on_run_status_change(self, *args):
            self.status_changes.append(args)

    path.global_metrics = Metrics()
    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow("template")

    snapshot = _assert_initialization_failed(store, error.value.run_id)
    assert snapshot["error_summary"]["phase"] == "metrics"
    assert "idempotency_key" not in snapshot
    assert sent_messages == []
    assert path.global_metrics.status_changes == [
        (error.value.run_id, "submitted", "failed")
    ]


def test_non_idempotent_event_failure_is_terminal_before_dispatch(tmp_path):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _multi_root_workflow())

    def fail_event(*_args, **_kwargs):
        raise OSError("event persistence failed")

    path._record_static_event = fail_event
    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow("template")

    snapshot = _assert_initialization_failed(store, error.value.run_id)
    assert snapshot["error_summary"]["phase"] == "event"
    assert snapshot["idempotency_initialization"]["root_dispatch"] == {
        "root-1": "pending",
        "root-2": "pending",
        "root-3": "pending",
    }
    assert sent_messages == []


@pytest.mark.parametrize("failed_root_index", [0, 1, 2])
def test_non_idempotent_root_send_failure_waits_for_cleanup_ack(
    tmp_path,
    failed_root_index,
):
    store = StaticRunStore(tmp_path)
    path, _ = _path_for(store, _multi_root_workflow())
    messages = []
    run_task_attempts = 0

    def send(message):
        nonlocal run_task_attempts
        if message["type"] == "run_task":
            current_index = run_task_attempts
            run_task_attempts += 1
            if current_index == failed_root_index:
                raise OSError(f"root send {current_index} failed")
        messages.append(message)

    path._send_scheduler_message = send
    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow("template")

    pending = _assert_cleanup_pending(store, error.value.run_id)
    dispatch = pending["idempotency_initialization"]["root_dispatch"]
    for index in range(failed_root_index):
        assert dispatch[f"root-{index + 1}"] == "sent"
    assert dispatch[f"root-{failed_root_index + 1}"] == "sending"
    for index in range(failed_root_index + 1, 3):
        assert dispatch[f"root-{index + 1}"] == "pending"
    assert pending["status"] == "created"
    assert [message["type"] for message in messages][-1] == "stop_workflow"

    _ack_cleanup(path, store, error.value.run_id)
    _assert_initialization_failed(store, error.value.run_id)


@pytest.mark.parametrize(
    ("failed_root_index", "fail_on_save"),
    [(0, 5), (1, 7), (2, 9)],
)
def test_non_idempotent_post_send_save_failure_waits_for_cleanup_ack(
    tmp_path,
    failed_root_index,
    fail_on_save,
):
    store = _FailNthSaveStore(tmp_path, fail_on=fail_on_save)
    path, sent_messages = _path_for(store, _multi_root_workflow())

    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow("template")

    pending = _assert_cleanup_pending(store, error.value.run_id)
    dispatch = pending["idempotency_initialization"]["root_dispatch"]
    for index in range(failed_root_index + 1):
        assert dispatch[f"root-{index + 1}"] == "sent"
    for index in range(failed_root_index + 1, 3):
        assert dispatch[f"root-{index + 1}"] == "pending"
    assert pending["idempotency_initialization"]["error"]["phase"] == (
        f"root_dispatch:root-{failed_root_index + 1}:confirming"
    )
    assert len([
        message for message in sent_messages if message["type"] == "run_task"
    ]) == failed_root_index + 1
    assert sent_messages[-1]["type"] == "stop_workflow"

    _ack_cleanup(path, store, error.value.run_id)
    _assert_initialization_failed(store, error.value.run_id)


def test_non_idempotent_final_save_failure_waits_for_cleanup_ack(tmp_path):
    store = _FailNthSaveStore(tmp_path, fail_on=10)
    path, sent_messages = _path_for(store, _multi_root_workflow())

    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow("template")

    pending = _assert_cleanup_pending(store, error.value.run_id)
    assert pending["idempotency_initialization"]["error"]["phase"] == "ready"
    assert pending["idempotency_initialization"]["root_dispatch"] == {
        "root-1": "sent",
        "root-2": "sent",
        "root-3": "sent",
    }
    assert [message["type"] for message in sent_messages] == [
        "run_task",
        "run_task",
        "run_task",
        "stop_workflow",
    ]

    _ack_cleanup(path, store, error.value.run_id)
    _assert_initialization_failed(store, error.value.run_id)


@pytest.mark.parametrize("crash_point", ["send", "post_send_save"])
def test_non_idempotent_base_exception_restart_requires_cleanup_ack(
    tmp_path,
    crash_point,
):
    class SimulatedProcessCrash(BaseException):
        pass

    class CrashNthSaveStore(StaticRunStore):
        def __init__(self, workspace_dir, fail_on):
            super().__init__(workspace_dir)
            self.fail_on = fail_on
            self.save_calls = 0

        def save_run(self, snapshot):
            self.save_calls += 1
            if self.save_calls == self.fail_on:
                raise SimulatedProcessCrash()
            super().save_run(snapshot)

    store = (
        StaticRunStore(tmp_path)
        if crash_point == "send"
        else CrashNthSaveStore(tmp_path, fail_on=7)
    )
    path, delivered_messages = _path_for(store, _multi_root_workflow())
    if crash_point == "send":
        run_task_sends = 0

        def deliver_then_crash(message):
            nonlocal run_task_sends
            delivered_messages.append(message)
            if message["type"] == "run_task":
                run_task_sends += 1
                if run_task_sends == 2:
                    raise SimulatedProcessCrash()

        path._send_scheduler_message = deliver_then_crash

    with pytest.raises(SimulatedProcessCrash):
        path.run_workflow("template")

    crashed = store.list_runs()[0]
    run_id = crashed["run_id"]
    assert "idempotency_key" not in crashed
    assert crashed["status"] == "created"
    assert crashed["idempotency_initialization"]["status"] == "initializing"
    assert crashed["idempotency_initialization"]["root_dispatch"] == {
        "root-1": "sent",
        "root-2": "sending",
        "root-3": "pending",
    }
    assert "stop_workflow" not in [
        message["type"] for message in delivered_messages
    ]

    recovery_store = StaticRunStore(tmp_path)
    assert recovery_store.recover_interrupted_runs() == []
    assert not {
        "interrupt_workflow",
        "workflow_initialization_failed",
    } & {
        event["type"] for event in recovery_store.load_events(run_id)
    }
    restarted, restarted_messages = _path_for(
        recovery_store,
        _multi_root_workflow(),
    )
    assert restarted._recover_incomplete_workflow_initializations() == [run_id]

    pending = _assert_cleanup_pending(recovery_store, run_id)
    assert pending["status"] == "created"
    assert [message["type"] for message in restarted_messages] == [
        "stop_workflow"
    ]
    assert not {
        "interrupt_workflow",
        "workflow_initialization_failed",
    } & {
        event["type"] for event in recovery_store.load_events(run_id)
    }
    _ack_cleanup(restarted, recovery_store, run_id)
    failed = _assert_initialization_failed(recovery_store, run_id)
    events = recovery_store.load_events(run_id)
    assert [event["type"] for event in events].count(
        "workflow_initialization_failed"
    ) == 1
    assert "interrupt_workflow" not in [event["type"] for event in events]
    assert failed["event_count"] == len(events)
    assert failed["last_event_seq"] == max(event["seq"] for event in events)


def test_restart_cleanup_ack_reuses_failure_event_after_snapshot_save_failure(
    tmp_path,
):
    class SimulatedProcessCrash(BaseException):
        pass

    class FailNextSaveStore(StaticRunStore):
        def __init__(self, workspace_dir):
            super().__init__(workspace_dir)
            self.failed = False

        def save_run(self, snapshot):
            if not self.failed:
                self.failed = True
                raise OSError("injected post-event snapshot save failure")
            super().save_run(snapshot)

    initial_store = StaticRunStore(tmp_path)
    path, delivered_messages = _path_for(initial_store, _workflow())

    def deliver_then_crash(message):
        delivered_messages.append(message)
        if message["type"] == "run_task":
            raise SimulatedProcessCrash()

    path._send_scheduler_message = deliver_then_crash
    with pytest.raises(SimulatedProcessCrash):
        path.run_workflow("template")
    run_id = initial_store.list_runs()[0]["run_id"]

    recovery_store = StaticRunStore(tmp_path)
    assert recovery_store.recover_interrupted_runs() == []
    restarted, _ = _path_for(recovery_store, _workflow())
    restarted._recover_incomplete_workflow_initializations()
    pending = _assert_cleanup_pending(recovery_store, run_id)
    request_id = pending["idempotency_initialization"]["cleanup_request_id"]

    restarted.static_run_store = FailNextSaveStore(tmp_path)
    with pytest.raises(OSError, match="post-event snapshot save failure"):
        restarted._handle_idempotent_workflow_cleanup_response({
            "request_id": request_id,
            "workflow_id": run_id,
            "ok": True,
        })

    durable_store = StaticRunStore(tmp_path)
    assert durable_store.load_run(run_id)["idempotency_initialization"][
        "status"
    ] == "cleanup_pending"
    events_after_failed_save = durable_store.load_events(run_id)
    failure_event = next(
        event
        for event in events_after_failed_save
        if event["type"] == "workflow_initialization_failed"
    )

    retry_path, _ = _path_for(durable_store, _workflow())
    retry_path._handle_idempotent_workflow_cleanup_response({
        "request_id": request_id,
        "workflow_id": run_id,
        "ok": True,
    })

    failed = _assert_initialization_failed(durable_store, run_id)
    events = durable_store.load_events(run_id)
    failure_events = [
        event
        for event in events
        if event["type"] == "workflow_initialization_failed"
    ]
    assert failure_events == [failure_event]
    assert failed["event_count"] == len(events)
    assert failed["last_event_seq"] == max(event["seq"] for event in events)


@pytest.mark.parametrize(
    ("store_type", "failure_attribute", "failure_message", "event_count"),
    [
        (
            _FailNextAppendStore,
            "fail_next_append",
            "failure event append failure",
            0,
        ),
        (
            _FailNextSaveStore,
            "fail_next_save",
            "failed snapshot save failure",
            1,
        ),
    ],
)
def test_cleanup_ack_persistence_failure_restores_pending_memory_and_retries_once(
    tmp_path,
    store_type,
    failure_attribute,
    failure_message,
    event_count,
):
    store = store_type(tmp_path)
    path, _, run_id = _start_cleanup_pending_run(store)
    pending = _assert_cleanup_pending(store, run_id)
    request_id = pending["idempotency_initialization"]["cleanup_request_id"]
    memory_before = copy.deepcopy(path.static_runs[run_id].__dict__)
    metrics_before = path.global_metrics.snapshot()

    setattr(store, failure_attribute, True)
    with pytest.raises(OSError, match=failure_message):
        path._handle_idempotent_workflow_cleanup_response({
            "request_id": request_id,
            "workflow_id": run_id,
            "ok": True,
        })

    assert path.static_runs[run_id].__dict__ == memory_before
    assert store.load_run(run_id)["idempotency_initialization"][
        "status"
    ] == "cleanup_pending"
    failure_events = [
        event
        for event in store.load_events(run_id)
        if event["type"] == "workflow_initialization_failed"
    ]
    assert len(failure_events) == event_count
    assert request_id in path.idempotency_cleanup_requests
    assert request_id in path.idempotency_cleanup_retries
    assert path.async_que[run_id].empty()
    assert path.global_metrics.snapshot()["static_runs"] == metrics_before[
        "static_runs"
    ]

    path._handle_idempotent_workflow_cleanup_response({
        "request_id": request_id,
        "workflow_id": run_id,
        "ok": True,
    })

    failed = _assert_initialization_failed(store, run_id)
    events = store.load_events(run_id)
    failure_events = [
        event
        for event in events
        if event["type"] == "workflow_initialization_failed"
    ]
    assert len(failure_events) == 1
    assert path.static_runs[run_id].event_log == events
    assert path.static_runs[run_id].event_seq == max(
        event["seq"] for event in events
    )
    assert failed["event_count"] == len(events)
    assert failed["last_event_seq"] == max(event["seq"] for event in events)
    metrics = path.global_metrics.snapshot()["static_runs"]
    assert metrics["total"] == 1
    assert metrics["by_status"]["submitted"] == 0
    assert metrics["by_status"]["failed"] == 1
    assert request_id not in path.idempotency_cleanup_requests
    assert request_id not in path.idempotency_cleanup_retries

    path._handle_idempotent_workflow_cleanup_response({
        "request_id": request_id,
        "workflow_id": run_id,
        "ok": True,
    })
    assert store.load_events(run_id) == events
    assert path.global_metrics.snapshot()["static_runs"] == metrics


def test_store_restart_recovers_durable_prepare_failure_without_interrupt(
    tmp_path,
    monkeypatch,
):
    run_workspace = tmp_path / "run-store"
    artifact_root = tmp_path / "artifacts"
    workspace_dir = tmp_path / "workspace"
    files_dir = workspace_dir / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "input.txt").write_text("private input", encoding="utf-8")
    store = _FailFirstInitializationFailedSnapshotStore(run_workspace)
    path, sent_messages = _path_for(store, _workflow())
    path.global_metrics = GlobalMetrics()
    prepare = path._prepare_initial_artifacts

    def prepare_then_fail(*args, **kwargs):
        prepare(*args, **kwargs)
        raise OSError("injected prepare failure")

    path._prepare_initial_artifacts = prepare_then_fail
    with pytest.raises(WorkflowInitializationError) as raised:
        path.run_workflow(
            "template",
            file_context={
                "enabled": True,
                "workspace_dir": str(workspace_dir),
                "private": True,
                "artifact_store": {
                    "root": str(artifact_root),
                    "private": True,
                },
            },
        )
    run_id = raised.value.run_id
    assert sent_messages == []
    active = store.load_run(run_id)
    assert active["status"] == "created"
    assert active["idempotency_initialization"]["status"] == "initializing"
    assert active["idempotency_initialization"]["artifact_status"] == "pending"
    events = store.load_events(run_id)
    assert [event["type"] for event in events].count(
        "workflow_initialization_failed"
    ) == 1
    assert "interrupt_workflow" not in [event["type"] for event in events]

    original_revoke = LocalCASArtifactStore.revoke_owner_capabilities

    def fail_revoke(_store, _owner_id):
        raise OSError("injected recovery revoke failure")

    monkeypatch.setattr(
        LocalCASArtifactStore,
        "revoke_owner_capabilities",
        fail_revoke,
    )
    restarted_store = StaticRunStore(run_workspace)
    with pytest.raises(OSError, match="recovery revoke failure"):
        restarted_store.recover_interrupted_runs()
    still_active = restarted_store.load_run(run_id)
    assert still_active["status"] == "created"
    assert still_active["idempotency_initialization"]["status"] == "initializing"
    assert restarted_store.load_events(run_id) == events

    monkeypatch.setattr(
        LocalCASArtifactStore,
        "revoke_owner_capabilities",
        original_revoke,
    )
    recovered = restarted_store.recover_interrupted_runs()
    assert [snapshot["run_id"] for snapshot in recovered] == [run_id]
    failed = _assert_initialization_failed(restarted_store, run_id)
    MaPath._validate_idempotency_initialization(
        failed["idempotency_initialization"]
    )
    assert failed["idempotency_initialization"]["artifact_status"] == "revoked"
    assert failed["artifact_reservation"]["status"] == "revoked"
    assert restarted_store.load_events(run_id) == events
    assert "interrupt_workflow" not in [event["type"] for event in events]

    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(run_workspace))
    restarted_path = MaPath()
    try:
        metrics = restarted_path.global_metrics.snapshot()["static_runs"]
        assert metrics["total"] == 1
        assert metrics["by_status"]["failed"] == 1
        assert metrics["by_status"]["interrupted"] == 0
    finally:
        restarted_path._release_core_process_lease()


def test_store_restart_uses_post_ack_failure_event_as_terminal_proof(tmp_path):
    class SimulatedProcessCrash(BaseException):
        pass

    store = _FailNextSaveStore(tmp_path)
    initial, delivered_messages = _path_for(store, _workflow())

    def deliver_then_crash(message):
        delivered_messages.append(copy.deepcopy(message))
        if message["type"] == "run_task":
            raise SimulatedProcessCrash()

    initial._send_scheduler_message = deliver_then_crash
    with pytest.raises(SimulatedProcessCrash):
        initial.run_workflow("template")
    run_id = store.list_runs()[0]["run_id"]

    restarted, _ = _path_for(store, _workflow())
    assert restarted._recover_incomplete_workflow_initializations() == [run_id]
    pending = _assert_cleanup_pending(store, run_id)
    request_id = pending["idempotency_initialization"]["cleanup_request_id"]
    store.fail_next_save = True
    with pytest.raises(OSError, match="failed snapshot save failure"):
        restarted._handle_idempotent_workflow_cleanup_response({
            "request_id": request_id,
            "workflow_id": run_id,
            "ok": True,
        })

    durable_events = store.load_events(run_id)
    assert [event["type"] for event in durable_events].count(
        "workflow_initialization_failed"
    ) == 1
    assert store.load_run(run_id)["idempotency_initialization"][
        "status"
    ] == "cleanup_pending"

    recovered_store = StaticRunStore(tmp_path)
    recovered = recovered_store.recover_interrupted_runs()
    assert [snapshot["run_id"] for snapshot in recovered] == [run_id]
    failed = _assert_initialization_failed(recovered_store, run_id)
    initialization = failed["idempotency_initialization"]
    assert initialization["cleanup_request_id"] == request_id
    assert [entry["event"] for entry in initialization["journal"]][
        -3:
    ] == ["cleanup_requested", "cleanup_confirmed", "failed"]
    MaPath._validate_idempotency_initialization(initialization)
    assert recovered_store.load_events(run_id) == durable_events
    assert "interrupt_workflow" not in [
        event["type"] for event in durable_events
    ]


@pytest.mark.parametrize("conflict", ["duplicate", "interrupt"])
def test_store_recovery_rejects_conflicting_initialization_terminal_events(
    tmp_path,
    conflict,
):
    store = _FailFirstInitializationFailedSnapshotStore(tmp_path)
    path, _ = _path_for(store, _workflow())
    path._prepare_initial_artifacts = lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(OSError("injected prepare failure"))
    with pytest.raises(WorkflowInitializationError) as raised:
        path.run_workflow("template", file_context={"enabled": False})
    run_id = raised.value.run_id
    failure_event = next(
        event
        for event in store.load_events(run_id)
        if event["type"] == "workflow_initialization_failed"
    )
    conflicting_event = copy.deepcopy(failure_event)
    conflicting_event["seq"] += 1
    if conflict == "interrupt":
        conflicting_event = {
            "type": "interrupt_workflow",
            "seq": conflicting_event["seq"],
            "timestamp": conflicting_event["timestamp"],
            "schema_version": 1,
            "data": {
                "run_id": run_id,
                "workflow_id": "template",
                "run_status": "interrupted",
                "reason": "conflicting recovery",
            },
        }
    store.append_event(run_id, conflicting_event)

    with pytest.raises(ValueError, match="Duplicate|Conflicting"):
        StaticRunStore(tmp_path).recover_interrupted_runs()
    assert store.load_run(run_id)["status"] == "created"


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed", [False, True])
async def test_cleanup_watchdog_reuses_request_and_caps_backoff(
    tmp_path,
    keyed,
):
    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())
    path.global_metrics = GlobalMetrics()
    clock = [0.0]
    path._idempotency_cleanup_monotonic = lambda: clock[0]
    path._idempotency_cleanup_retry_initial_seconds = 0.1
    path._idempotency_cleanup_retry_max_seconds = 0.4
    stop_outcomes = [
        OSError("injected cleanup send failure"),
        {"ok": False, "error": "injected negative ack"},
        None,
        None,
        True,
    ]

    def send(message):
        sent_messages.append(copy.deepcopy(message))
        if message["type"] == "run_task":
            raise OSError("ambiguous root dispatch")
        outcome = stop_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    path._send_scheduler_message = send
    kwargs = (
        {
            "idempotency_key": "watchdog-keyed",
            "idempotency_fingerprint": FINGERPRINT,
        }
        if keyed
        else {}
    )
    with pytest.raises(WorkflowInitializationError) as raised:
        path.run_workflow("template", timeout_seconds=0, **kwargs)
    run_id = raised.value.run_id
    pending = _assert_cleanup_pending(store, run_id)
    request_id = pending["idempotency_initialization"]["cleanup_request_id"]
    retry = path.idempotency_cleanup_retries[request_id]
    assert retry["attempts"] == 1
    assert retry["delay_seconds"] == pytest.approx(0.1)

    stop_count = len([
        message for message in sent_messages if message["type"] == "stop_workflow"
    ])
    for _ in range(3):
        await path._sweep_run_deadlines()
        path._retry_pending_idempotent_workflow_cleanups()
    assert len([
        message for message in sent_messages if message["type"] == "stop_workflow"
    ]) == stop_count

    for expected_delay in (0.2, 0.4, 0.4):
        retry = path.idempotency_cleanup_retries[request_id]
        clock[0] = retry["next_attempt"] - 0.001
        path._retry_pending_idempotent_workflow_cleanups()
        assert path.idempotency_cleanup_retries[request_id] is retry
        clock[0] = retry["next_attempt"]
        path._retry_pending_idempotent_workflow_cleanups()
        retry = path.idempotency_cleanup_retries[request_id]
        assert retry["delay_seconds"] == pytest.approx(expected_delay)

    retry = path.idempotency_cleanup_retries[request_id]
    clock[0] = retry["next_attempt"]
    path._retry_pending_idempotent_workflow_cleanups()

    failed = _assert_initialization_failed(store, run_id)
    stop_requests = [
        message["data"]
        for message in sent_messages
        if message["type"] == "stop_workflow"
    ]
    assert len(stop_requests) == 5
    assert {request["request_id"] for request in stop_requests} == {request_id}
    assert {request["workflow_id"] for request in stop_requests} == {run_id}
    journal_events = [
        entry["event"]
        for entry in failed["idempotency_initialization"]["journal"]
    ]
    assert journal_events.count("cleanup_requested") == 1
    assert journal_events.count("cleanup_confirmed") == 1
    assert journal_events.count("failed") == 1
    assert [
        event["type"] for event in store.load_events(run_id)
    ].count("workflow_initialization_failed") == 1
    metrics = path.global_metrics.snapshot()["static_runs"]
    assert metrics["total"] == 1
    assert metrics["by_status"]["failed"] == 1
    assert path.idempotency_cleanup_requests == {}
    assert path.idempotency_cleanup_retries == {}


@pytest.mark.asyncio
async def test_public_cancel_resends_only_the_pending_initialization_cleanup(
    tmp_path,
):
    store = StaticRunStore(tmp_path)
    path, sent_messages, run_id = _start_cleanup_pending_run(store)
    pending = _assert_cleanup_pending(store, run_id)
    request_id = pending["idempotency_initialization"]["cleanup_request_id"]
    run_before = copy.deepcopy(path.static_runs[run_id].__dict__)
    events_before = store.load_events(run_id)
    metrics_before = path.global_metrics.snapshot()["static_runs"]

    await path.stop_workflow(run_id)

    assert path.static_runs[run_id].__dict__ == run_before
    assert store.load_events(run_id) == events_before
    assert path.global_metrics.snapshot()["static_runs"] == metrics_before
    assert run_id in path.async_que
    stop_requests = [
        message["data"]
        for message in sent_messages
        if message["type"] == "stop_workflow"
    ]
    assert len(stop_requests) == 2
    assert {request["request_id"] for request in stop_requests} == {request_id}
    assert not {
        "cancel_workflow",
        "timeout_workflow",
        "interrupt_workflow",
    } & {event["type"] for event in store.load_events(run_id)}


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "deadline"])
async def test_ambiguous_initialization_enters_cleanup_without_ordinary_terminal(
    tmp_path,
    action,
):
    class SimulatedProcessCrash(BaseException):
        pass

    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())
    path.global_metrics = GlobalMetrics()

    def deliver_then_crash(message):
        sent_messages.append(copy.deepcopy(message))
        if message["type"] == "run_task":
            raise SimulatedProcessCrash()

    path._send_scheduler_message = deliver_then_crash
    with pytest.raises(SimulatedProcessCrash):
        path.run_workflow("template", timeout_seconds=0)
    run_id = store.list_runs()[0]["run_id"]
    assert path.static_runs[run_id]._idempotency_initialization[
        "status"
    ] == "initializing"

    if action == "cancel":
        await path.stop_workflow(run_id)
    else:
        path.static_runs[run_id].submitted_time -= 1
        await path._sweep_run_deadlines()

    pending = _assert_cleanup_pending(store, run_id)
    assert pending["status"] == "created"
    assert pending["task_nodes"]["task"]["status"] == "queued"
    assert run_id in path.async_que
    assert not {
        "cancel_workflow",
        "timeout_workflow",
        "interrupt_workflow",
        "workflow_initialization_failed",
    } & {event["type"] for event in store.load_events(run_id)}
    stop_requests = [
        message["data"]
        for message in sent_messages
        if message["type"] == "stop_workflow"
    ]
    assert len(stop_requests) == 1
    assert stop_requests[0]["request_id"] == pending[
        "idempotency_initialization"
    ]["cleanup_request_id"]
    metrics = path.global_metrics.snapshot()["static_runs"]
    assert metrics["total"] == 1
    assert metrics["by_status"]["submitted"] == 1
    assert metrics["by_status"]["failed"] == 0


class _SingleSchedulerMessage:
    def __init__(self, message):
        self.message = message
        self.delivered = False

    async def recv_multipart(self):
        if self.delivered:
            raise asyncio.CancelledError()
        self.delivered = True
        return [b"scheduler", json.dumps(self.message).encode("utf-8")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_type",
    ["start_task", "task_pending", "task_retry", "finish_task", "task_exception"],
)
async def test_cleanup_pending_ignores_late_scheduler_task_events(
    tmp_path,
    message_type,
):
    store = StaticRunStore(tmp_path)
    path, sent_messages, run_id = _start_cleanup_pending_run(store)
    static_run = path.static_runs[run_id]
    run_before = copy.deepcopy(static_run.__dict__)
    snapshot_before = store.load_run(run_id)
    events_before = store.load_events(run_id)
    metrics_before = path.global_metrics.snapshot()
    sends_before = copy.deepcopy(sent_messages)
    message = {
        "type": message_type,
        "data": {
            "workflow_id": run_id,
            "task_id": "task",
            "attempt": 1,
            "dispatch_id": "dispatch-1",
            "lease_id": "lease-1",
            "result": {"value": "late"},
            "error": {"error_type": "late", "message": "late"},
            "metrics": {"tokens_in": 7},
        },
    }
    path.socket_from_scheduler = _SingleSchedulerMessage(message)

    with pytest.raises(asyncio.CancelledError):
        await path.monitor_coroutine()

    assert static_run.__dict__ == run_before
    assert store.load_run(run_id) == snapshot_before
    assert store.load_events(run_id) == events_before
    assert path.global_metrics.snapshot()["static_runs"] == metrics_before[
        "static_runs"
    ]
    assert path.global_metrics.snapshot()["tasks"] == metrics_before["tasks"]
    assert path.task_attempts == {}
    assert path.async_que[run_id].empty()
    assert sent_messages == sends_before


@pytest.mark.asyncio
async def test_scheduler_fatal_waits_for_verified_global_cleanup_before_failure(
    tmp_path,
):
    class SimulatedProcessCrash(BaseException):
        pass

    class DeadScheduler:
        pid = 4321
        exitcode = 17

        @staticmethod
        def is_alive():
            return False

    store = StaticRunStore(tmp_path)
    path, sent_messages = _path_for(store, _workflow())
    path.global_metrics = GlobalMetrics()

    def deliver_then_crash(message):
        sent_messages.append(copy.deepcopy(message))
        if message["type"] == "run_task":
            raise SimulatedProcessCrash()

    path._send_scheduler_message = deliver_then_crash
    with pytest.raises(SimulatedProcessCrash):
        path.run_workflow("template")
    run_id = store.list_runs()[0]["run_id"]
    path.scheduler_process = DeadScheduler()
    path._send_scheduler_message = lambda _message: (_ for _ in ()).throw(
        OSError("scheduler is unavailable")
    )
    cleanup_outcomes = iter((False, True))
    path._stop_local_ray_best_effort = lambda: next(cleanup_outcomes)
    revoke_calls = []
    original_revoke = path._revoke_idempotent_artifact_reservation

    def record_revoke(candidate_run_id, initialization):
        revoke_calls.append(candidate_run_id)
        return original_revoke(candidate_run_id, initialization)

    path._revoke_idempotent_artifact_reservation = record_revoke

    await path._handle_scheduler_exit()

    pending = _assert_cleanup_pending(store, run_id)
    request_id = pending["idempotency_initialization"]["cleanup_request_id"]
    assert path.static_runs[run_id].status == "created"
    assert path.static_runs[run_id].task_nodes["task"]["status"] == "queued"
    assert path.global_metrics.snapshot()["static_runs"]["by_status"][
        "submitted"
    ] == 1
    assert revoke_calls == []
    assert path._scheduler_failure_handled is None
    assert not {
        "interrupt_workflow",
        "workflow_initialization_failed",
    } & {event["type"] for event in store.load_events(run_id)}

    await path._handle_scheduler_exit()

    failed = _assert_initialization_failed(store, run_id)
    assert failed["idempotency_initialization"]["cleanup_request_id"] == request_id
    events = store.load_events(run_id)
    assert [event["type"] for event in events].count(
        "workflow_initialization_failed"
    ) == 1
    assert "interrupt_workflow" not in [event["type"] for event in events]
    assert revoke_calls == [run_id]
    metrics = path.global_metrics.snapshot()["static_runs"]
    assert metrics["total"] == 1
    assert metrics["by_status"]["submitted"] == 0
    assert metrics["by_status"]["failed"] == 1
    assert path._scheduler_failure_handled == (4321, 17)
    assert request_id not in path.idempotency_cleanup_requests
    assert request_id not in path.idempotency_cleanup_retries


def test_mapath_startup_defers_ambiguous_running_metrics_until_cleanup_ack(
    tmp_path,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    store = StaticRunStore(tmp_path)
    initial_path, delivered_messages = _path_for(store, _workflow())

    def deliver_then_crash(message):
        delivered_messages.append(message)
        if message["type"] == "run_task":
            raise SimulatedProcessCrash()

    initial_path._send_scheduler_message = deliver_then_crash
    with pytest.raises(SimulatedProcessCrash):
        initial_path.run_workflow("template")

    snapshot = store.list_runs()[0]
    run_id = snapshot["run_id"]
    snapshot["status"] = "running"
    store.save_run(snapshot)

    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    restarted = MaPath()
    try:
        active = restarted.static_run_store.load_run(run_id)
        assert active["status"] == "running"
        metrics = restarted.global_metrics.snapshot()
        assert metrics["static_runs"]["total"] == 1
        assert metrics["static_runs"]["by_status"]["running"] == 1
        assert metrics["static_runs"]["by_status"]["failed"] == 0
        assert "interrupt_workflow" not in [
            event["type"]
            for event in restarted.static_run_store.load_events(run_id)
        ]

        restarted_messages = []
        restarted._send_scheduler_message = restarted_messages.append
        assert restarted._recover_incomplete_workflow_initializations() == [run_id]
        pending = restarted.static_run_store.load_run(run_id)
        assert pending["status"] == "running"
        metrics = restarted.global_metrics.snapshot()
        assert metrics["static_runs"]["total"] == 1
        assert metrics["static_runs"]["by_status"]["running"] == 1
        assert metrics["static_runs"]["by_status"]["failed"] == 0
        assert [message["type"] for message in restarted_messages] == [
            "stop_workflow"
        ]

        request_id = pending["idempotency_initialization"]["cleanup_request_id"]
        restarted._handle_idempotent_workflow_cleanup_response({
            "request_id": request_id,
            "workflow_id": run_id,
            "ok": True,
        })

        failed = restarted.static_run_store.load_run(run_id)
        assert failed["status"] == "failed"
        metrics = restarted.global_metrics.snapshot()
        assert metrics["static_runs"]["total"] == 1
        assert metrics["static_runs"]["by_status"]["running"] == 0
        assert metrics["static_runs"]["by_status"]["failed"] == 1
        assert "interrupt_workflow" not in [
            event["type"]
            for event in restarted.static_run_store.load_events(run_id)
        ]
    finally:
        restarted._release_core_process_lease()


def test_mapath_startup_recovers_ready_running_run_and_records_interrupted_metric(
    tmp_path,
    monkeypatch,
):
    store = StaticRunStore(tmp_path)
    initial_path, _ = _path_for(store, _workflow())
    run_id = initial_path.run_workflow("template")
    snapshot = store.load_run(run_id)
    assert snapshot["idempotency_initialization"]["status"] == "ready"
    snapshot["status"] = "running"
    store.save_run(snapshot)

    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    restarted = MaPath()
    try:
        recovered = restarted.static_run_store.load_run(run_id)
        assert recovered["status"] == "interrupted"
        metrics = restarted.global_metrics.snapshot()
        assert metrics["static_runs"]["total"] == 1
        assert metrics["static_runs"]["by_status"]["running"] == 0
        assert metrics["static_runs"]["by_status"]["interrupted"] == 1
        assert [
            event["type"]
            for event in restarted.static_run_store.load_events(run_id)
        ].count("interrupt_workflow") == 1
    finally:
        restarted._release_core_process_lease()
