import sqlite3
from types import SimpleNamespace

import pytest

from maze.core.files.artifact_store import LocalCASArtifactStore, sha256_bytes
from maze.core.path.path import MaPath, WorkflowInitializationError, WorkflowRunConflictError
from maze.core.scheduler.runtime_estimator import RuntimeEstimator
from maze.core.workflow.static_run import StaticRunStore
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


RUN_ID = "d4c98c23-e3f3-4df8-889f-41cab7e5f2f2"
TASK_RESOURCES = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}


def _workflow():
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


def _path_for(store):
    path = object.__new__(MaPath)
    workflow = _workflow()
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


def _access_counts(store):
    with sqlite3.connect(store.access_db_path) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "capabilities",
                "capability_owners",
                "artifact_grants",
                "artifact_access",
            )
        }


def _assert_revoked(store):
    assert _access_counts(store) == {
        "capabilities": 0,
        "capability_owners": 0,
        "artifact_grants": 0,
        "artifact_access": 0,
    }
    assert list(store.iter_blobs() or []) == []


def test_revoke_preserves_shared_sha_until_last_owner(tmp_path):
    store = LocalCASArtifactStore(tmp_path / "artifacts")
    payload = b"shared private artifact"
    sha256 = sha256_bytes(payload)
    first = store.create_capability(owner_id="run-1")
    second = store.create_capability(owner_id="run-2")

    store.put_bytes(sha256, payload, private=True, capability=first)
    store.put_bytes(sha256, payload, private=True, capability=second)
    store.revoke_owner_capabilities("run-1")

    assert not store.can_read(sha256, first)
    assert store.can_read(sha256, second)
    assert store.exists(sha256)

    store.revoke_owner_capabilities("run-2")
    _assert_revoked(store)


def test_revoke_does_not_delete_preexisting_public_blob(tmp_path):
    store = LocalCASArtifactStore(tmp_path / "artifacts")
    payload = b"preexisting public artifact"
    sha256 = sha256_bytes(payload)
    store.put_bytes(sha256, payload)
    capability = store.create_capability(owner_id="run-private")
    store.put_bytes(sha256, payload, private=True, capability=capability)

    store.revoke_owner_capabilities("run-private")

    assert store.exists(sha256)
    assert not store.is_private(sha256)
    assert all(count == 0 for count in _access_counts(store).values())


def test_prepare_failure_revokes_private_capability(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "workspace"
    files_dir = workspace_dir / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "first.txt").write_text("first", encoding="utf-8")
    (files_dir / "second.txt").write_text("second", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_store = LocalCASArtifactStore(artifact_root)
    path, messages = _path_for(StaticRunStore(tmp_path / "runs"))
    put_file = LocalCASArtifactStore.put_file
    calls = 0

    def fail_second_file(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected artifact import failure")
        return put_file(self, *args, **kwargs)

    monkeypatch.setattr(LocalCASArtifactStore, "put_file", fail_second_file)
    with pytest.raises(WorkflowInitializationError):
        path.run_workflow(
            "template",
            run_id=RUN_ID,
            file_context=_private_file_context(workspace_dir, artifact_root),
        )

    snapshot = path.static_run_store.load_run(RUN_ID)
    assert snapshot["dispatch"]["artifact_status"] == "revoked"
    assert messages == []
    _assert_revoked(artifact_store)


def test_restart_revokes_crashed_pre_dispatch_capability(tmp_path, monkeypatch):
    class SimulatedCrash(BaseException):
        pass

    workspace_dir = tmp_path / "workspace"
    files_dir = workspace_dir / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "input.txt").write_text("private", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_store = LocalCASArtifactStore(artifact_root)
    store = StaticRunStore(tmp_path / "runs")
    path, messages = _path_for(store)
    prepare = path._prepare_initial_artifacts

    def prepare_then_crash(*args, **kwargs):
        prepare(*args, **kwargs)
        raise SimulatedCrash()

    monkeypatch.setattr(path, "_prepare_initial_artifacts", prepare_then_crash)
    with pytest.raises(SimulatedCrash):
        path.run_workflow(
            "template",
            run_id=RUN_ID,
            file_context=_private_file_context(workspace_dir, artifact_root),
        )

    assert store.recover_interrupted_runs() == []
    restarted, restart_messages = _path_for(store)
    assert restarted._recover_incomplete_workflow_dispatches() == [RUN_ID]

    snapshot = store.load_run(RUN_ID)
    assert snapshot["status"] == "failed"
    assert snapshot["dispatch"]["artifact_status"] == "revoked"
    assert messages == restart_messages == []
    _assert_revoked(artifact_store)


def test_partial_dispatch_revokes_only_after_cleanup_ack(tmp_path):
    workspace_dir = tmp_path / "workspace"
    files_dir = workspace_dir / "files"
    files_dir.mkdir(parents=True)
    (files_dir / "input.txt").write_text("private", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_store = LocalCASArtifactStore(artifact_root)
    store = StaticRunStore(tmp_path / "runs")
    path, messages = _path_for(store)

    def fail_dispatch(message):
        messages.append(message)
        if message["type"] == "run_task":
            raise OSError("injected dispatch failure")

    path._send_scheduler_message = fail_dispatch
    with pytest.raises(WorkflowInitializationError):
        path.run_workflow(
            "template",
            run_id=RUN_ID,
            file_context=_private_file_context(workspace_dir, artifact_root),
        )

    dispatch = store.load_run(RUN_ID)["dispatch"]
    assert dispatch["status"] == "cleanup_pending"
    assert _access_counts(artifact_store)["capability_owners"] == 1

    path._handle_workflow_cleanup_response({
        "request_id": dispatch["cleanup_request_id"],
        "workflow_id": RUN_ID,
        "ok": True,
    })

    assert store.load_run(RUN_ID)["dispatch"]["artifact_status"] == "revoked"
    _assert_revoked(artifact_store)


def test_run_id_conflict_precedes_second_private_capability(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "files").mkdir(parents=True)
    artifact_root = tmp_path / "artifacts"
    path, _ = _path_for(StaticRunStore(tmp_path / "runs"))
    create_capability = LocalCASArtifactStore.create_capability
    calls = 0

    def count_capability(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return create_capability(self, *args, **kwargs)

    monkeypatch.setattr(LocalCASArtifactStore, "create_capability", count_capability)
    context = _private_file_context(workspace_dir, artifact_root)
    path.run_workflow("template", run_id=RUN_ID, file_context=context)
    with pytest.raises(WorkflowRunConflictError):
        path.run_workflow(
            "template",
            run_id=RUN_ID,
            file_context=context,
            metadata={"changed": True},
        )

    assert calls == 1
