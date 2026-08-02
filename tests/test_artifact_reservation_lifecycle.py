import sqlite3
from types import SimpleNamespace

import pytest

from maze.core.files.artifact_store import LocalCASArtifactStore, sha256_bytes
from maze.core.path.path import (
    MaPath,
    WorkflowIdempotencyConflictError,
    WorkflowInitializationError,
)
from maze.core.scheduler.runtime_estimator import RuntimeEstimator
from maze.core.workflow.static_run import StaticRunStore
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


FINGERPRINT = "a" * 64
TASK_RESOURCES = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}


def _workflow() -> Workflow:
    workflow = Workflow("template")
    task = CodeTask("template", "task", "artifact-reservation")
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


def _path_for(store: StaticRunStore):
    path = object.__new__(MaPath)
    workflow = _workflow()
    path.workflows = {workflow.id: workflow}
    path.submit_workflows = {}
    path.async_que = {}
    path.static_runs = {}
    path.strategy = "Default"
    path.static_run_store = store
    path.resource_history = SimpleNamespace(
        apply=lambda resources, *_args: resources,
    )
    path.runtime_estimator = RuntimeEstimator()
    path.global_metrics = SimpleNamespace(on_run_submitted=lambda _run_id: None)
    path.scheduler_process = SimpleNamespace(
        is_alive=lambda: True,
        pid=123,
        exitcode=None,
    )
    sent_messages = []
    path._send_scheduler_message = sent_messages.append
    return path, sent_messages


def _private_file_context(workspace_dir, artifact_root):
    return {
        "enabled": True,
        "workspace_dir": str(workspace_dir),
        "private": True,
        "artifact_store": {
            "root": str(artifact_root),
            "private": True,
        },
    }


def _access_counts(store: LocalCASArtifactStore):
    with sqlite3.connect(store.access_db_path) as connection:
        return {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "capabilities",
                "capability_owners",
                "artifact_grants",
                "artifact_access",
            )
        }


def _assert_no_private_reservation_state(store: LocalCASArtifactStore):
    assert _access_counts(store) == {
        "capabilities": 0,
        "capability_owners": 0,
        "artifact_grants": 0,
        "artifact_access": 0,
    }
    assert list(store.iter_blobs() or []) == []


def test_revoke_preserves_shared_sha_until_the_last_owner(tmp_path):
    store = LocalCASArtifactStore(tmp_path / "artifacts")
    payload = b"shared private artifact"
    sha256 = sha256_bytes(payload)
    first = store.create_capability(owner_id="run-1")
    second = store.create_capability(owner_id="run-2")

    store.put_bytes(sha256, payload, private=True, capability=first)
    store.put_bytes(sha256, payload, private=True, capability=second)
    assert store.can_read(sha256, first)
    assert store.can_read(sha256, second)

    assert store.revoke_owner_capabilities("run-1") == 1
    assert not store.can_read(sha256, first)
    assert store.can_read(sha256, second)
    assert store.exists(sha256)
    assert _access_counts(store) == {
        "capabilities": 1,
        "capability_owners": 1,
        "artifact_grants": 1,
        "artifact_access": 1,
    }

    assert store.revoke_owner_capabilities("run-2") == 1
    _assert_no_private_reservation_state(store)


def test_revoke_does_not_delete_a_preexisting_public_blob(tmp_path):
    store = LocalCASArtifactStore(tmp_path / "artifacts")
    payload = b"preexisting public artifact"
    sha256 = sha256_bytes(payload)
    store.put_bytes(sha256, payload)
    capability = store.create_capability(owner_id="run-private")
    store.put_bytes(sha256, payload, private=True, capability=capability)

    store.revoke_owner_capabilities("run-private")

    assert store.exists(sha256)
    assert not store.is_private(sha256)
    assert _access_counts(store) == {
        "capabilities": 0,
        "capability_owners": 0,
        "artifact_grants": 0,
        "artifact_access": 0,
    }


def test_prepare_failure_revokes_acl_rows_and_created_blobs(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "workspace"
    files_dir = workspace_dir / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "first.txt").write_text("first", encoding="utf-8")
    (files_dir / "second.txt").write_text("second", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_store = LocalCASArtifactStore(artifact_root)
    path, sent_messages = _path_for(StaticRunStore(tmp_path / "runs"))
    put_file = LocalCASArtifactStore.put_file
    calls = 0

    def fail_second_file(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second artifact import failure")
        return put_file(self, *args, **kwargs)

    monkeypatch.setattr(LocalCASArtifactStore, "put_file", fail_second_file)
    with pytest.raises(WorkflowInitializationError) as error:
        path.run_workflow(
            "template",
            file_context=_private_file_context(workspace_dir, artifact_root),
            idempotency_key="private-prepare-failure",
            idempotency_fingerprint=FINGERPRINT,
        )

    snapshot = path.static_run_store.load_run(error.value.run_id)
    assert snapshot["idempotency_initialization"]["artifact_status"] == "revoked"
    assert sent_messages == []
    _assert_no_private_reservation_state(artifact_store)


def test_crashed_prepare_is_revoked_during_idempotent_recovery(
    tmp_path,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    workspace_dir = tmp_path / "workspace"
    files_dir = workspace_dir / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "input.txt").write_text("private input", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_store = LocalCASArtifactStore(artifact_root)
    run_store = StaticRunStore(tmp_path / "runs")
    path, sent_messages = _path_for(run_store)
    prepare = path._prepare_initial_artifacts

    def prepare_then_crash(*args, **kwargs):
        prepare(*args, **kwargs)
        raise SimulatedProcessCrash()

    monkeypatch.setattr(path, "_prepare_initial_artifacts", prepare_then_crash)
    file_context = _private_file_context(workspace_dir, artifact_root)
    with pytest.raises(SimulatedProcessCrash):
        path.run_workflow(
            "template",
            file_context=file_context,
            idempotency_key="private-prepare-crash",
            idempotency_fingerprint=FINGERPRINT,
        )

    crashed = run_store.list_runs()[0]
    run_id = crashed["run_id"]
    assert crashed["idempotency_initialization"]["artifact_status"] == "pending"
    assert _access_counts(artifact_store) == {
        "capabilities": 1,
        "capability_owners": 1,
        "artifact_grants": 1,
        "artifact_access": 1,
    }
    assert len(list(artifact_store.iter_blobs() or [])) == 1
    assert sent_messages == []

    recovered = run_store.recover_interrupted_runs()
    assert [snapshot["run_id"] for snapshot in recovered] == [run_id]
    interrupted = run_store.load_run(run_id)
    assert interrupted["status"] == "interrupted"
    assert (
        interrupted["idempotency_initialization"]["artifact_status"]
        == "revoked"
    )
    _assert_no_private_reservation_state(artifact_store)

    restarted, restarted_messages = _path_for(run_store)
    with pytest.raises(WorkflowInitializationError) as replay:
        restarted.run_workflow(
            "template",
            file_context=file_context,
            idempotency_key="private-prepare-crash",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert replay.value.run_id == run_id
    assert restarted_messages == []
    recovered = run_store.load_run(run_id)
    assert recovered["idempotency_initialization"]["artifact_status"] == "revoked"
    _assert_no_private_reservation_state(artifact_store)


def test_claim_conflict_happens_before_a_second_private_capability(
    tmp_path,
    monkeypatch,
):
    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "files").mkdir(parents=True)
    artifact_root = tmp_path / "artifacts"
    path, _ = _path_for(StaticRunStore(tmp_path / "runs"))
    create_capability = LocalCASArtifactStore.create_capability
    capability_calls = 0

    def count_capability(self, *args, **kwargs):
        nonlocal capability_calls
        capability_calls += 1
        return create_capability(self, *args, **kwargs)

    monkeypatch.setattr(
        LocalCASArtifactStore,
        "create_capability",
        count_capability,
    )
    file_context = _private_file_context(workspace_dir, artifact_root)
    path.run_workflow(
        "template",
        file_context=file_context,
        idempotency_key="private-claim-conflict",
        idempotency_fingerprint=FINGERPRINT,
    )
    with pytest.raises(WorkflowIdempotencyConflictError):
        path.run_workflow(
            "template",
            file_context=file_context,
            metadata={"changed": True},
            idempotency_key="private-claim-conflict",
            idempotency_fingerprint=FINGERPRINT,
        )

    assert capability_calls == 1


def test_non_idempotent_crashed_prepare_is_revoked_by_store_recovery(
    tmp_path,
    monkeypatch,
):
    class SimulatedProcessCrash(BaseException):
        pass

    workspace_dir = tmp_path / "workspace"
    files_dir = workspace_dir / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "input.txt").write_text("private input", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_store = LocalCASArtifactStore(artifact_root)
    run_store = StaticRunStore(tmp_path / "runs")
    path, sent_messages = _path_for(run_store)
    prepare = path._prepare_initial_artifacts

    def prepare_then_crash(*args, **kwargs):
        prepare(*args, **kwargs)
        raise SimulatedProcessCrash()

    monkeypatch.setattr(path, "_prepare_initial_artifacts", prepare_then_crash)
    with pytest.raises(SimulatedProcessCrash):
        path.run_workflow(
            "template",
            file_context=_private_file_context(workspace_dir, artifact_root),
        )

    crashed = run_store.list_runs()[0]
    run_id = crashed["run_id"]
    assert crashed["artifact_reservation"]["status"] == "pending"
    assert len(list(artifact_store.iter_blobs() or [])) == 1
    assert sent_messages == []

    recovered = run_store.recover_interrupted_runs()

    assert [snapshot["run_id"] for snapshot in recovered] == [run_id]
    snapshot = run_store.load_run(run_id)
    assert snapshot["status"] == "interrupted"
    assert snapshot["artifact_reservation"]["status"] == "revoked"
    _assert_no_private_reservation_state(artifact_store)


def test_non_idempotent_crash_after_dispatch_revokes_only_after_cleanup_ack(
    tmp_path,
):
    class SimulatedProcessCrash(BaseException):
        pass

    workspace_dir = tmp_path / "workspace"
    files_dir = workspace_dir / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "input.txt").write_text("private input", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_store = LocalCASArtifactStore(artifact_root)
    run_store = StaticRunStore(tmp_path / "runs")
    path, delivered_messages = _path_for(run_store)

    def deliver_then_crash(message):
        delivered_messages.append(message)
        if message["type"] == "run_task":
            raise SimulatedProcessCrash()

    path._send_scheduler_message = deliver_then_crash
    with pytest.raises(SimulatedProcessCrash):
        path.run_workflow(
            "template",
            file_context=_private_file_context(workspace_dir, artifact_root),
        )

    crashed = run_store.list_runs()[0]
    run_id = crashed["run_id"]
    assert crashed["idempotency_initialization"]["root_dispatch"] == {
        "task": "sending"
    }
    assert crashed["artifact_reservation"]["status"] == "ready"
    assert _access_counts(artifact_store) == {
        "capabilities": 1,
        "capability_owners": 1,
        "artifact_grants": 1,
        "artifact_access": 1,
    }

    recovery_store = StaticRunStore(tmp_path / "runs")
    assert recovery_store.recover_interrupted_runs() == []
    assert not {
        "interrupt_workflow",
        "workflow_initialization_failed",
    } & {
        event["type"] for event in recovery_store.load_events(run_id)
    }
    restarted, restarted_messages = _path_for(recovery_store)
    assert restarted._recover_incomplete_workflow_initializations() == [run_id]
    pending = recovery_store.load_run(run_id)
    initialization = pending["idempotency_initialization"]
    assert initialization["status"] == "cleanup_pending"
    assert pending["status"] == "created"
    assert [message["type"] for message in restarted_messages] == [
        "stop_workflow"
    ]
    assert _access_counts(artifact_store)["capability_owners"] == 1
    assert not {
        "interrupt_workflow",
        "workflow_initialization_failed",
    } & {
        event["type"] for event in recovery_store.load_events(run_id)
    }

    restarted._handle_idempotent_workflow_cleanup_response({
        "request_id": initialization["cleanup_request_id"],
        "workflow_id": run_id,
        "ok": True,
    })

    failed = recovery_store.load_run(run_id)
    assert failed["status"] == "failed"
    assert failed["idempotency_initialization"]["status"] == "failed"
    assert failed["idempotency_initialization"]["artifact_status"] == "revoked"
    assert failed["artifact_reservation"]["status"] == "revoked"
    events = recovery_store.load_events(run_id)
    assert [event["type"] for event in events].count(
        "workflow_initialization_failed"
    ) == 1
    assert "interrupt_workflow" not in [event["type"] for event in events]
    assert failed["event_count"] == len(events)
    assert failed["last_event_seq"] == max(event["seq"] for event in events)
    _assert_no_private_reservation_state(artifact_store)
