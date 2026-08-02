import contextlib
import json
import os
import stat
from pathlib import Path

import pytest

from maze.core.workflow import dynamic_store as dynamic_store_module
from maze.core.workflow import static_run as static_run_module
from maze.core.workflow.dynamic_store import DynamicRunStore
from maze.core.workflow.static_run import StaticRunStore


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permission bits and umask are required",
)


@contextlib.contextmanager
def _umask(mask: int):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_private_paths(directories, files) -> None:
    for directory in directories:
        assert _mode(directory) == 0o700
    for path in files:
        assert _mode(path) == 0o600
        assert path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def _static_snapshot(run_id: str, capability: str) -> dict:
    return {
        "run_id": run_id,
        "workflow_id": "workflow",
        "status": "running",
        "created_time": 1.0,
        "updated_time": 1.0,
        "finished_time": None,
        "task_nodes": {},
        "event_count": 1,
        "last_event_seq": 1,
        "metadata": {"required_capability": capability},
    }


def _dynamic_snapshot(run_id: str, workspace_dir: Path, capability: str) -> dict:
    return {
        "run_id": run_id,
        "status": "running",
        "created_time": 1.0,
        "updated_time": 1.0,
        "finished_time": None,
        "event_count": 1,
        "last_event_seq": 1,
        "file_context": {
            "workspace_dir": str(workspace_dir),
            "required_capability": capability,
        },
    }


def _record_and_relax_replaces(monkeypatch, module):
    real_replace = module.os.replace
    temporary_modes = []

    def replace(source, target):
        temporary_modes.append(_mode(Path(source)))
        real_replace(source, target)
        os.chmod(target, 0o644)

    monkeypatch.setattr(module.os, "replace", replace)
    return temporary_modes


def test_static_store_forces_private_permissions_with_umask_022(
    tmp_path,
    monkeypatch,
):
    capability = "static-secret-capability"
    temporary_modes = _record_and_relax_replaces(
        monkeypatch,
        static_run_module,
    )

    with _umask(0o022):
        store = StaticRunStore(tmp_path)
        store.save_run(_static_snapshot("static-run", capability))
        store.append_event(
            "static-run",
            {"type": "created", "seq": 1, "data": {"capability": capability}},
        )

    run_path = store.run_json_path("static-run")
    events_path = store.events_path("static-run")
    _assert_private_paths(
        [store.runs_dir, store.run_dir("static-run")],
        [run_path, events_path],
    )
    assert temporary_modes == [0o600]
    assert capability in run_path.read_text(encoding="utf-8")
    assert capability in events_path.read_text(encoding="utf-8")
    assert list(store.run_dir("static-run").glob(".run.json.*.tmp")) == []


def test_dynamic_store_forces_private_canonical_and_mirror_permissions(
    tmp_path,
    monkeypatch,
):
    capability = "dynamic-secret-capability"
    workspace_dir = tmp_path / "external-workspace"
    temporary_modes = _record_and_relax_replaces(
        monkeypatch,
        dynamic_store_module,
    )
    snapshot = _dynamic_snapshot("dynamic-run", workspace_dir, capability)

    with _umask(0o022):
        store = DynamicRunStore(tmp_path / "canonical")
        store.save_run(snapshot)
        store.append_event(
            "dynamic-run",
            {"type": "created", "seq": 1, "data": {"capability": capability}},
            snapshot=snapshot,
        )

    canonical_dir = store.run_dir("dynamic-run")
    mirror_dir = workspace_dir / "runs" / "dynamic-run"
    snapshots = [
        store.run_json_path("dynamic-run"),
        store.dynamic_run_json_path(mirror_dir),
    ]
    events = [
        store.events_path("dynamic-run"),
        store.dynamic_events_path(mirror_dir),
    ]
    _assert_private_paths(
        [store.runs_dir, canonical_dir, mirror_dir],
        snapshots + events,
    )
    assert temporary_modes == [0o600, 0o600]
    for path in snapshots + events:
        assert capability in path.read_text(encoding="utf-8")


def test_static_store_tightens_existing_files_on_load_append_and_recovery(
    tmp_path,
):
    capability = "existing-static-capability"
    run_id = "static-existing"
    store = StaticRunStore(tmp_path)
    store.save_run(_static_snapshot(run_id, capability))
    store.append_event(
        run_id,
        {"type": "created", "seq": 1, "data": {"capability": capability}},
    )
    run_dir = store.run_dir(run_id)
    run_path = store.run_json_path(run_id)
    events_path = store.events_path(run_id)

    os.chmod(run_dir, 0o755)
    os.chmod(run_path, 0o644)
    os.chmod(events_path, 0o644)
    snapshot_before = run_path.read_bytes()
    events_before = events_path.read_bytes()

    assert store.load_run(run_id)["metadata"]["required_capability"] == capability
    assert store.load_events(run_id)[0]["data"]["capability"] == capability
    assert run_path.read_bytes() == snapshot_before
    assert events_path.read_bytes() == events_before
    _assert_private_paths([run_dir], [run_path, events_path])

    os.chmod(events_path, 0o644)
    store.append_event(run_id, {"type": "running", "seq": 2, "data": {}})
    assert _mode(events_path) == 0o600

    os.chmod(run_dir, 0o755)
    os.chmod(run_path, 0o644)
    os.chmod(events_path, 0o644)
    recovered = StaticRunStore(tmp_path).recover_interrupted_runs()

    assert [snapshot["run_id"] for snapshot in recovered] == [run_id]
    assert store.load_run(run_id)["status"] == "interrupted"
    assert [event["type"] for event in store.load_events(run_id)] == [
        "created",
        "running",
        "interrupt_workflow",
    ]
    _assert_private_paths([run_dir], [run_path, events_path])


def test_dynamic_recovery_tightens_existing_canonical_and_mirror_files(
    tmp_path,
):
    capability = "existing-dynamic-capability"
    run_id = "dynamic-existing"
    workspace_dir = tmp_path / "external-workspace"
    store = DynamicRunStore(tmp_path / "canonical")
    snapshot = _dynamic_snapshot(run_id, workspace_dir, capability)
    store.save_run(snapshot)
    store.append_event(
        run_id,
        {"type": "created", "seq": 1, "data": {"capability": capability}},
        snapshot=snapshot,
    )

    canonical_dir = store.run_dir(run_id)
    mirror_dir = workspace_dir / "runs" / run_id
    run_paths = [
        store.run_json_path(run_id),
        store.dynamic_run_json_path(mirror_dir),
    ]
    event_paths = [
        store.events_path(run_id),
        store.dynamic_events_path(mirror_dir),
    ]
    for directory in (canonical_dir, mirror_dir):
        os.chmod(directory, 0o755)
    for path in run_paths + event_paths:
        os.chmod(path, 0o644)

    canonical_before = run_paths[0].read_bytes()
    assert store.load_run(run_id)["file_context"]["required_capability"] == capability
    assert run_paths[0].read_bytes() == canonical_before
    assert _mode(canonical_dir) == 0o700
    assert _mode(run_paths[0]) == 0o600

    os.chmod(run_paths[0], 0o644)
    recovered = DynamicRunStore(tmp_path / "canonical").recover_interrupted_runs()

    assert [item["run_id"] for item in recovered] == [run_id]
    assert store.load_run(run_id)["status"] == "interrupted"
    assert [event["type"] for event in store.load_events(run_id)] == [
        "created",
        "interrupt_dynamic_run",
    ]
    _assert_private_paths(
        [canonical_dir, mirror_dir],
        run_paths + event_paths,
    )
    for path in run_paths + event_paths:
        assert capability in path.read_text(encoding="utf-8")
