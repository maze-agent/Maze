import ast
from pathlib import Path

import pytest

from maze.core.workflow import static_run as static_run_module
from maze.core.workflow.static_run import StaticRunStore


def test_save_run_fsyncs_file_and_directories_around_atomic_replace(
    tmp_path,
    monkeypatch,
):
    store = StaticRunStore(tmp_path)
    actions = []
    replace = static_run_module.os.replace

    monkeypatch.setattr(
        static_run_module,
        "_fsync_directory",
        lambda path: actions.append(("directory_fsync", Path(path))),
    )
    monkeypatch.setattr(
        static_run_module.os,
        "fsync",
        lambda _descriptor: actions.append(("file_fsync", None)),
    )

    def record_replace(source, target):
        actions.append(("replace", Path(target)))
        return replace(source, target)

    monkeypatch.setattr(static_run_module.os, "replace", record_replace)
    store.save_run({"run_id": "run-1", "status": "created"})

    assert actions == [
        ("directory_fsync", store.runs_dir),
        ("file_fsync", None),
        ("replace", store.run_json_path("run-1")),
        ("directory_fsync", store.run_dir("run-1")),
    ]


def test_snapshot_file_fsync_failure_preserves_previous_snapshot(
    tmp_path,
    monkeypatch,
):
    store = StaticRunStore(tmp_path)
    store.save_run({"run_id": "run-1", "status": "created", "version": 1})
    monkeypatch.setattr(static_run_module, "_fsync_directory", lambda _path: None)

    def fail_fsync(_descriptor):
        raise OSError("injected snapshot fsync failure")

    monkeypatch.setattr(static_run_module.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="snapshot fsync failure"):
        store.save_run({"run_id": "run-1", "status": "running", "version": 2})

    assert store.load_run("run-1")["version"] == 1
    assert list(store.run_dir("run-1").glob(".run.json.*.tmp")) == []


def test_post_replace_directory_fsync_failure_is_reported_with_new_snapshot(
    tmp_path,
    monkeypatch,
):
    store = StaticRunStore(tmp_path)
    calls = []

    def fail_run_directory(path):
        calls.append(Path(path))
        if Path(path) == store.run_dir("run-1"):
            raise OSError("injected run directory fsync failure")

    monkeypatch.setattr(
        static_run_module,
        "_fsync_directory",
        fail_run_directory,
    )
    with pytest.raises(OSError, match="run directory fsync failure"):
        store.save_run({"run_id": "run-1", "status": "created"})

    assert calls == [store.runs_dir, store.run_dir("run-1")]
    assert store.load_run("run-1")["status"] == "created"


def test_first_event_fsyncs_event_file_and_parent_directories(
    tmp_path,
    monkeypatch,
):
    store = StaticRunStore(tmp_path)
    actions = []
    monkeypatch.setattr(
        static_run_module,
        "_fsync_directory",
        lambda path: actions.append(("directory_fsync", Path(path))),
    )
    monkeypatch.setattr(
        static_run_module.os,
        "fsync",
        lambda _descriptor: actions.append(("file_fsync", None)),
    )

    store.append_event("run-1", {"type": "created", "seq": 1})

    assert actions == [
        ("directory_fsync", store.runs_dir),
        ("file_fsync", None),
        ("directory_fsync", store.run_dir("run-1")),
    ]
    assert [event["type"] for event in store.load_events("run-1")] == [
        "created"
    ]


def test_delete_run_fsyncs_runs_directory(tmp_path, monkeypatch):
    store = StaticRunStore(tmp_path)
    store.save_run({"run_id": "run-1", "status": "created"})
    synced = []
    monkeypatch.setattr(
        static_run_module,
        "_fsync_directory",
        lambda path: synced.append(Path(path)),
    )

    store.delete_run("run-1")

    assert synced == [store.runs_dir]
    assert not store.run_dir("run-1").exists()


def test_fcntl_is_not_imported_unconditionally():
    source_path = Path(static_run_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    unconditional_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    conditional_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.If)
        for branch in (node.body, node.orelse)
        for child in branch
        if isinstance(child, ast.Import)
        for alias in child.names
    }

    assert "fcntl" not in unconditional_imports
    assert {"fcntl", "msvcrt"}.issubset(conditional_imports)
