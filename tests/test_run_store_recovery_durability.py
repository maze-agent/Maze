import json
import os

import pytest

from maze.core.path.path import MaPath
from maze.core.workflow import dynamic_store as dynamic_store_module
from maze.core.workflow.dynamic_store import DynamicRunStore
from maze.core.workflow.static_run import StaticRunStore


def _static_snapshot(run_id: str):
    return {
        "run_id": run_id,
        "workflow_id": "workflow",
        "status": "running",
        "created_time": 1.0,
        "updated_time": 1.0,
        "finished_time": None,
        "task_nodes": {},
        "event_count": 0,
        "last_event_seq": 0,
    }


def _dynamic_snapshot(run_id: str):
    return {
        "run_id": run_id,
        "status": "running",
        "created_time": 1.0,
        "updated_time": 1.0,
        "finished_time": None,
        "event_count": 0,
        "last_event_seq": 0,
    }


def _ambiguous_initialization():
    return {
        "schema_version": 1,
        "status": "initializing",
        "phase": "root_dispatch:task:sending",
        "started_time": 1.0,
        "completed_time": None,
        "failed_time": None,
        "root_task_ids": ["task"],
        "root_dispatch": {"task": "sending"},
        "artifact_status": "none",
        "artifact_owner_id": None,
        "artifact_store_root": None,
        "cleanup_request_id": None,
        "error": None,
        "journal": [
            {"seq": 1, "event": "reserved", "phase": "artifacts", "timestamp": 1.0},
            {"seq": 2, "event": "artifacts_ready", "phase": "metrics", "timestamp": 2.0},
            {"seq": 3, "event": "event_recorded", "phase": "event", "timestamp": 3.0},
            {
                "seq": 4,
                "event": "root_sending",
                "phase": "root_dispatch:task:sending",
                "timestamp": 4.0,
                "task_id": "task",
            },
        ],
    }


def _cleanup_pending_initialization():
    root_dispatch = {"task": "pending"}
    request_id = "cleanup-request"
    return {
        "schema_version": 1,
        "status": "cleanup_pending",
        "phase": "cleanup",
        "started_time": 1.0,
        "completed_time": None,
        "failed_time": None,
        "root_task_ids": ["task"],
        "root_dispatch": root_dispatch,
        "artifact_status": "none",
        "artifact_owner_id": None,
        "artifact_store_root": None,
        "cleanup_request_id": request_id,
        "error": {
            "error_type": "workflow_initialization_failed",
            "phase": "event",
            "root_dispatch": root_dispatch,
            "message": "cleanup required",
        },
        "journal": [
            {"seq": 1, "event": "reserved", "phase": "artifacts", "timestamp": 1.0},
            {"seq": 2, "event": "artifacts_ready", "phase": "metrics", "timestamp": 2.0},
            {"seq": 3, "event": "event_recorded", "phase": "event", "timestamp": 3.0},
            {
                "seq": 4,
                "event": "cleanup_requested",
                "phase": "cleanup",
                "timestamp": 4.0,
                "request_id": request_id,
            },
        ],
    }


def _assert_reconciled(store, run_id: str, terminal_type: str):
    events = store.load_events(run_id)
    snapshot = store.load_run(run_id)
    sequences = [int(event["seq"]) for event in events]

    assert snapshot["status"] == "interrupted"
    assert snapshot["event_count"] == len(events)
    assert snapshot["last_event_seq"] == max(sequences)
    assert len(sequences) == len(set(sequences))
    assert [event["type"] for event in events].count(terminal_type) == 1


@pytest.mark.parametrize("sequence", [None, 0, -1, True, 1.0, "1"])
def test_static_event_log_rejects_non_positive_integer_sequence(
    tmp_path,
    sequence,
):
    store = StaticRunStore(tmp_path)
    event = {"type": "invalid", "seq": sequence, "data": {}}
    with pytest.raises(ValueError, match="positive integer"):
        store.append_event("static-invalid", event)

    events_path = store.events_path("static-invalid")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="positive integer"):
        store.load_events("static-invalid")


@pytest.mark.parametrize("sequence", [None, 0, -1, True, 1.0, "1"])
def test_dynamic_event_log_rejects_non_positive_integer_sequence(
    tmp_path,
    sequence,
):
    store = DynamicRunStore(tmp_path)
    event = {"type": "invalid", "seq": sequence, "data": {}}
    with pytest.raises(ValueError, match="positive integer"):
        store.append_event("dynamic-invalid", event)

    events_path = store.events_path("dynamic-invalid")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="positive integer"):
        store.load_events("dynamic-invalid")


def test_static_recovery_reuses_fsynced_interrupt_after_snapshot_save_failure(
    tmp_path,
    monkeypatch,
):
    store = StaticRunStore(tmp_path)
    run_id = "static-run"
    store.save_run(_static_snapshot(run_id))
    store.append_event(
        run_id,
        {"type": "task_started", "seq": 7, "data": {"run_id": run_id}},
    )
    real_save_run = store.save_run
    fail_once = True

    def fail_interrupt_snapshot_once(snapshot):
        nonlocal fail_once
        if fail_once and snapshot.get("status") == "interrupted":
            fail_once = False
            raise OSError("injected static snapshot save failure")
        return real_save_run(snapshot)

    monkeypatch.setattr(store, "save_run", fail_interrupt_snapshot_once)
    with pytest.raises(OSError, match="static snapshot save failure"):
        store.recover_interrupted_runs()

    assert store.load_run(run_id)["status"] == "running"
    persisted_after_failure = store.load_events(run_id)
    assert [event["seq"] for event in persisted_after_failure] == [7, 8]
    assert [event["type"] for event in persisted_after_failure].count(
        "interrupt_workflow"
    ) == 1

    assert [item["run_id"] for item in store.recover_interrupted_runs()] == [
        run_id
    ]
    _assert_reconciled(store, run_id, "interrupt_workflow")


def test_static_recovery_fails_closed_on_duplicate_event_sequence(tmp_path):
    store = StaticRunStore(tmp_path)
    run_id = "static-duplicate"
    store.save_run(_static_snapshot(run_id))
    event = {"type": "task_started", "seq": 3, "data": {"run_id": run_id}}
    store.append_event(run_id, event)
    store.append_event(run_id, event)

    with pytest.raises(ValueError, match="Non-monotonic static event sequence"):
        store.recover_interrupted_runs()

    assert store.load_run(run_id)["status"] == "running"


@pytest.mark.parametrize("sequences", [[2, 2], [2, 1]])
def test_static_load_events_rejects_duplicate_or_reverse_sequence(
    tmp_path,
    sequences,
):
    store = StaticRunStore(tmp_path)
    run_id = "static-invalid-order"
    events_path = store.events_path(run_id)
    events_path.parent.mkdir(parents=True)
    events_path.write_text(
        "".join(
            json.dumps({"type": "event", "seq": sequence, "data": {}})
            + "\n"
            for sequence in sequences
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Non-monotonic static event sequence"):
        store.load_events(run_id)


def test_static_recovery_defers_ambiguous_initialization_without_mutation(tmp_path):
    store = StaticRunStore(tmp_path)
    run_id = "static-ambiguous"
    snapshot = {
        **_static_snapshot(run_id),
        "status": "created",
        "idempotency_initialization": _ambiguous_initialization(),
    }
    MaPath._validate_idempotency_initialization(
        snapshot["idempotency_initialization"]
    )
    store.save_run(snapshot)
    store.append_event(
        run_id,
        {"type": "workflow_submitted", "seq": 3, "data": {"run_id": run_id}},
    )
    before = store.load_run(run_id)

    assert store.recover_interrupted_runs() == []

    assert store.load_run(run_id) == before
    events = store.load_events(run_id)
    assert [event["type"] for event in events] == ["workflow_submitted"]
    assert not any(
        event["type"] in {"interrupt_workflow", "workflow_initialization_failed"}
        for event in events
    )


def test_static_recovery_defers_cleanup_pending_even_when_all_roots_pending(
    tmp_path,
):
    store = StaticRunStore(tmp_path)
    run_id = "static-cleanup-pending"
    snapshot = {
        **_static_snapshot(run_id),
        "status": "created",
        "idempotency_initialization": _cleanup_pending_initialization(),
    }
    MaPath._validate_idempotency_initialization(
        snapshot["idempotency_initialization"]
    )
    store.save_run(snapshot)

    assert store.recover_interrupted_runs() == []
    assert store.load_run(run_id)["status"] == "created"
    assert store.load_events(run_id) == []


def test_dynamic_recovery_reuses_fsynced_interrupt_after_snapshot_save_failure(
    tmp_path,
    monkeypatch,
):
    store = DynamicRunStore(tmp_path)
    run_id = "dynamic-run"
    store.save_run(_dynamic_snapshot(run_id))
    store.append_event(
        run_id,
        {"type": "task_started", "seq": 11, "data": {"run_id": run_id}},
    )
    real_save_run = store.save_run
    fail_once = True

    def fail_interrupt_snapshot_once(snapshot):
        nonlocal fail_once
        if fail_once and snapshot.get("status") == "interrupted":
            fail_once = False
            raise OSError("injected dynamic snapshot save failure")
        return real_save_run(snapshot)

    monkeypatch.setattr(store, "save_run", fail_interrupt_snapshot_once)
    with pytest.raises(OSError, match="dynamic snapshot save failure"):
        store.recover_interrupted_runs()

    assert store.load_run(run_id)["status"] == "running"
    persisted_after_failure = store.load_events(run_id)
    assert [event["seq"] for event in persisted_after_failure] == [11, 12]
    assert [event["type"] for event in persisted_after_failure].count(
        "interrupt_dynamic_run"
    ) == 1

    assert [item["run_id"] for item in store.recover_interrupted_runs()] == [
        run_id
    ]
    _assert_reconciled(store, run_id, "interrupt_dynamic_run")


def test_dynamic_recovery_uses_durable_finish_instead_of_interrupting(
    tmp_path,
):
    canonical_root = tmp_path / "canonical"
    workspace_dir = tmp_path / "workspace"
    store = DynamicRunStore(canonical_root)
    run_id = "dynamic-durable-finish"
    snapshot = {
        **_dynamic_snapshot(run_id),
        "file_context": {"workspace_dir": str(workspace_dir)},
    }
    store.save_run(snapshot)
    store.append_event(
        run_id,
        {
            "type": "start_dynamic_run",
            "seq": 1,
            "timestamp": "2026-08-01T12:00:00+00:00",
            "data": {"run_id": run_id, "run_status": "running"},
        },
    )
    finish_event = {
        "type": "finish_workflow",
        "seq": 2,
        "timestamp": "2026-08-01T12:00:01+00:00",
        "data": {
            "run_id": run_id,
            "run_status": "finalized",
            "result": {"answer": "durable"},
        },
    }
    # This is the crash window in _emit_dynamic_event: the canonical event is
    # durable, while both snapshot and workspace mirror still lag behind.
    store.append_event(run_id, finish_event)

    restarted = DynamicRunStore(canonical_root)
    assert [item["run_id"] for item in restarted.recover_interrupted_runs()] == [
        run_id
    ]

    recovered = restarted.load_run(run_id)
    events = restarted.load_canonical_events(run_id)
    mirror_events = restarted._load_events_path(
        workspace_dir / "runs" / run_id / "dynamic_events.jsonl"
    )
    assert recovered["status"] == "finalized"
    assert recovered["final_result"] == {"answer": "durable"}
    assert recovered["event_count"] == 2
    assert recovered["last_event_seq"] == 2
    assert events == mirror_events
    assert [event["type"] for event in events] == [
        "start_dynamic_run",
        "finish_workflow",
    ]


@pytest.mark.parametrize(
    ("event_type", "status", "event_data", "result_field"),
    [
        (
            "task_exception",
            "failed",
            {"task_id": "task", "error": {"message": "failed"}},
            ("failure_reason", {"message": "failed"}),
        ),
        (
            "cancel_dynamic_run",
            "canceled",
            {"reason": "cancelled by user"},
            ("cancel_reason", "cancelled by user"),
        ),
        (
            "timeout_dynamic_run",
            "timed_out",
            {"timeout_seconds": 5},
            ("failure_reason", {"error_type": "timeout", "message": "Dynamic run timed out after 5 seconds"}),
        ),
        (
            "interrupt_dynamic_run",
            "interrupted",
            {"reason": "scheduler exited"},
            ("failure_reason", "scheduler exited"),
        ),
    ],
)
def test_dynamic_recovery_rebuilds_terminal_snapshot_from_event(
    tmp_path,
    event_type,
    status,
    event_data,
    result_field,
):
    store = DynamicRunStore(tmp_path)
    run_id = f"dynamic-{status}"
    snapshot = {
        **_dynamic_snapshot(run_id),
        "tasks": {
            "pending": [],
            "submitted": [],
            "running": ["task"],
            "completed": [],
            "failed": [],
        },
        "task_counts": {
            "total": 1,
            "pending": 0,
            "submitted": 0,
            "running": 1,
            "completed": 0,
            "failed": 0,
        },
        "task_nodes": {"task": {"task_id": "task", "status": "running"}},
        "task_errors": {},
    }
    store.save_run(snapshot)
    store.append_event(
        run_id,
        {
            "type": event_type,
            "seq": 1,
            "timestamp": "2026-08-01T12:00:00+00:00",
            "data": {
                "run_id": run_id,
                "run_status": status,
                **event_data,
            },
        },
    )

    store.recover_interrupted_runs()

    recovered = store.load_run(run_id)
    field, expected = result_field
    assert recovered["status"] == status
    assert recovered[field] == expected
    assert recovered["event_count"] == 1
    assert recovered["last_event_seq"] == 1
    assert [event["type"] for event in store.load_events(run_id)] == [event_type]
    if status in {"failed", "timed_out", "interrupted"}:
        assert recovered["tasks"]["running"] == []
        assert recovered["tasks"]["failed"] == ["task"]
        assert recovered["task_nodes"]["task"]["status"] == "failed"
    else:
        assert recovered["tasks"]["running"] == ["task"]


def test_dynamic_save_fsyncs_and_cleans_failed_temporary_file(
    tmp_path,
    monkeypatch,
):
    store = DynamicRunStore(tmp_path)
    fsync_calls = []
    real_fsync = dynamic_store_module.os.fsync

    def record_fsync(descriptor):
        fsync_calls.append(os.fstat(descriptor).st_mode)
        return real_fsync(descriptor)

    monkeypatch.setattr(dynamic_store_module.os, "fsync", record_fsync)
    store.save_run(_dynamic_snapshot("dynamic-fsync"))
    assert len(fsync_calls) >= 2

    def fail_dump(*_args, **_kwargs):
        raise OSError("injected dynamic snapshot serialization failure")

    monkeypatch.setattr(dynamic_store_module.json, "dump", fail_dump)
    with pytest.raises(OSError, match="snapshot serialization failure"):
        store.save_run(_dynamic_snapshot("dynamic-temp-cleanup"))
    assert list(store.run_dir("dynamic-temp-cleanup").glob("*.tmp")) == []


def test_dynamic_event_log_rejects_duplicate_and_conflicting_sequences(tmp_path):
    store = DynamicRunStore(tmp_path)
    run_id = "dynamic-conflict"
    first = {
        "type": "task_started",
        "seq": 1,
        "data": {"run_id": run_id, "task_id": "first"},
    }
    store.append_event(run_id, first)

    with pytest.raises(ValueError, match="Duplicate dynamic event sequence"):
        store.append_event(run_id, first)
    with pytest.raises(ValueError, match="Conflicting dynamic event sequence"):
        store.append_event(
            run_id,
            {
                "type": "task_started",
                "seq": 1,
                "data": {"run_id": run_id, "task_id": "different"},
            },
            deduplicate=True,
        )

    duplicate = {
        "schema_version": 1,
        **first,
    }
    with store.events_path(run_id).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(duplicate, sort_keys=True) + "\n")
    with pytest.raises(ValueError, match="Duplicate dynamic event sequence"):
        store.load_events(run_id)


def test_dynamic_recovery_uses_canonical_log_and_fills_workspace_mirror(tmp_path):
    store = DynamicRunStore(tmp_path / "canonical")
    workspace_dir = tmp_path / "external-workspace"
    run_id = "dynamic-mirror"
    snapshot = {
        **_dynamic_snapshot(run_id),
        "file_context": {"workspace_dir": str(workspace_dir)},
    }
    store.save_run(snapshot)
    canonical_event = {
        "type": "task_started",
        "seq": 4,
        "data": {"run_id": run_id},
    }
    # Simulate a crash after the canonical event fsync but before its mirror and
    # snapshot were updated.
    store.append_event(run_id, canonical_event)

    store.recover_interrupted_runs()

    canonical = store.load_canonical_events(run_id)
    mirror_path = workspace_dir / "runs" / run_id / "dynamic_events.jsonl"
    mirrored = store._load_events_path(mirror_path)
    assert mirrored == canonical
    _assert_reconciled(store, run_id, "interrupt_dynamic_run")


def test_dynamic_recovery_fails_closed_on_conflicting_workspace_mirror(tmp_path):
    store = DynamicRunStore(tmp_path)
    run_id = "dynamic-mirror-conflict"
    snapshot = {
        **_dynamic_snapshot(run_id),
        "file_context": {"workspace_dir": str(tmp_path)},
    }
    store.save_run(snapshot)
    canonical_event = {
        "type": "task_started",
        "seq": 2,
        "data": {"run_id": run_id, "task_id": "canonical"},
    }
    store.append_event(run_id, canonical_event)
    mirror_path = tmp_path / "runs" / run_id / "dynamic_events.jsonl"
    mirror_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "type": "task_started",
                "seq": 2,
                "data": {"run_id": run_id, "task_id": "conflict"},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    # Public reads and recovery sequencing use canonical state, but recovery
    # refuses to silently overwrite a divergent mirror.
    assert store.load_events(run_id)[0]["data"]["task_id"] == "canonical"
    with pytest.raises(ValueError, match="Conflicting dynamic event sequence"):
        store.recover_interrupted_runs()
    assert store.load_run(run_id)["status"] == "running"


def test_mapath_startup_reconciles_static_and_dynamic_event_logs(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    static_store = StaticRunStore(tmp_path)
    dynamic_store = DynamicRunStore(tmp_path)
    static_store.save_run(_static_snapshot("static-startup"))
    static_store.append_event(
        "static-startup",
        {"type": "task_started", "seq": 5, "data": {}},
    )
    dynamic_store.save_run(_dynamic_snapshot("dynamic-startup"))
    dynamic_store.append_event(
        "dynamic-startup",
        {"type": "task_started", "seq": 9, "data": {}},
    )

    path = MaPath()
    try:
        _assert_reconciled(
            path.static_run_store,
            "static-startup",
            "interrupt_workflow",
        )
        _assert_reconciled(
            path.dynamic_run_store,
            "dynamic-startup",
            "interrupt_dynamic_run",
        )
    finally:
        path._core_process_lease.release()

    static_lines = static_store.events_path("static-startup").read_text(
        encoding="utf-8"
    ).splitlines()
    dynamic_lines = dynamic_store.events_path("dynamic-startup").read_text(
        encoding="utf-8"
    ).splitlines()
    assert all(json.loads(line)["seq"] for line in static_lines)
    assert all(json.loads(line)["seq"] for line in dynamic_lines)


def test_mapath_startup_keeps_ambiguous_initialization_active(tmp_path, monkeypatch):
    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    store = StaticRunStore(tmp_path)
    run_id = "static-ambiguous-startup"
    store.save_run({
        **_static_snapshot(run_id),
        "status": "created",
        "idempotency_initialization": _ambiguous_initialization(),
    })
    store.append_event(
        run_id,
        {"type": "workflow_submitted", "seq": 1, "data": {"run_id": run_id}},
    )

    path = MaPath()
    try:
        snapshot = path.static_run_store.load_run(run_id)
        assert snapshot["status"] == "created"
        assert snapshot["idempotency_initialization"]["status"] == "initializing"
        events = path.static_run_store.load_events(run_id)
        assert [event["type"] for event in events] == ["workflow_submitted"]
    finally:
        path._core_process_lease.release()
