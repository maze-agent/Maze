from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict


TASK_RESULT_ENVELOPE = "__maze_task_result_envelope__"


def _safe_relative_path(path: str) -> Path:
    normalized = Path(str(path).replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"Unsafe file path: {path}")
    return normalized


def _copy_tree_contents(source: Path, destination: Path):
    if not source.exists():
        return

    for item in source.rglob("*"):
        if not item.is_file() or _is_ignored(item):
            continue
        relative = item.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _is_ignored(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix == ".pyc"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_files(root: Path) -> Dict[str, Dict[str, Any]]:
    snapshot: Dict[str, Dict[str, Any]] = {}
    if not root.exists():
        return snapshot

    for file_path in root.rglob("*"):
        if not file_path.is_file() or _is_ignored(file_path):
            continue
        relative = file_path.relative_to(root).as_posix()
        stat = file_path.stat()
        snapshot[relative] = {
            "path": relative,
            "size": stat.st_size,
            "sha256": _file_sha256(file_path),
            "mtime": stat.st_mtime,
        }
    return snapshot


def _load_parent_files(file_context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    workspace_dir = Path(file_context["workspace_dir"])
    run_id = file_context["run_id"]
    parent_task_ids = file_context.get("parent_task_ids") or []
    manifests_dir = workspace_dir / "workflow_runs" / "static" / run_id / "file_manifests" / "tasks"
    files_by_path: Dict[str, Dict[str, Any]] = {}

    for parent_task_id in parent_task_ids:
        manifest_path = manifests_dir / f"{parent_task_id}.json"
        if not manifest_path.exists():
            continue

        import json

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for file_info in manifest.get("files", []):
            relative_path = file_info.get("path")
            if not relative_path:
                continue
            _safe_relative_path(relative_path)
            existing = files_by_path.get(relative_path)
            if existing and existing.get("sha256") != file_info.get("sha256"):
                raise RuntimeError(
                    f"File lineage conflict for {relative_path}: "
                    f"{existing.get('producer_task_id')} and {file_info.get('producer_task_id')} produced different content."
                )
            files_by_path[relative_path] = file_info

    return files_by_path


def _stage_parent_files(file_context: Dict[str, Any], work_dir: Path):
    for relative_path, file_info in _load_parent_files(file_context).items():
        source = Path(file_info["storage_path"])
        if not source.exists():
            raise FileNotFoundError(f"Missing parent artifact for {relative_path}: {source}")
        target = work_dir / _safe_relative_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _write_manifest(file_context: Dict[str, Any], manifest: Dict[str, Any]):
    import json

    workspace_dir = Path(file_context["workspace_dir"])
    run_id = file_context["run_id"]
    task_id = file_context["task_id"]
    manifests_dir = workspace_dir / "workflow_runs" / "static" / run_id / "file_manifests" / "tasks"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifests_dir / f"{task_id}.json"
    tmp_path = manifest_path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
    tmp_path.write_text(f"{json.dumps(manifest, indent=2, ensure_ascii=False)}\n", encoding="utf-8")
    os.replace(tmp_path, manifest_path)


def _collect_output_manifest(file_context: Dict[str, Any], work_dir: Path, before: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    workspace_dir = Path(file_context["workspace_dir"])
    run_id = file_context["run_id"]
    task_id = file_context["task_id"]
    after = _snapshot_files(work_dir)
    artifacts_dir = workspace_dir / "workflow_runs" / "static" / run_id / "artifacts" / "tasks" / task_id
    files = []

    for relative_path, file_info in sorted(after.items()):
        before_info = before.get(relative_path)
        if before_info and before_info.get("sha256") == file_info.get("sha256"):
            continue

        source = work_dir / _safe_relative_path(relative_path)
        target = artifacts_dir / _safe_relative_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        mime_type, _ = mimetypes.guess_type(relative_path)
        files.append({
            "path": relative_path,
            "name": Path(relative_path).name,
            "size": file_info["size"],
            "sha256": file_info["sha256"],
            "mime": mime_type or "application/octet-stream",
            "producer_task_id": task_id,
            "storage_path": str(target),
            "uri": f"maze://runs/{run_id}/artifacts/tasks/{task_id}/{relative_path}",
        })

    deleted_files = sorted(path for path in before if path not in after)
    manifest = {
        "schema": "maze_task_file_manifest",
        "schema_version": 1,
        "run_id": run_id,
        "task_id": task_id,
        "node_id": file_context.get("node_id"),
        "created_time": time.time(),
        "files": files,
        "deleted_files": deleted_files,
    }
    _write_manifest(file_context, manifest)
    return manifest


def run_task_with_file_context(
    task_callable: Callable[[Dict[str, Any] | None], Dict[str, Any]],
    task_input_data: Dict[str, Any] | None,
    file_context: Dict[str, Any] | None,
) -> Dict[str, Any]:
    if not file_context or not file_context.get("enabled"):
        return task_callable(task_input_data)

    workspace_dir = Path(file_context["workspace_dir"]).resolve()
    run_id = file_context["run_id"]
    task_id = file_context["task_id"]
    work_dir = workspace_dir / "workflow_runs" / "static" / run_id / "sandboxes" / "tasks" / task_id / "work"

    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    _copy_tree_contents(workspace_dir / "files", work_dir)
    _stage_parent_files(file_context, work_dir)
    before = _snapshot_files(work_dir)

    previous_cwd = Path.cwd()
    previous_env = {
        key: os.environ.get(key)
        for key in ("MAZE_WORK_DIR", "MAZE_INPUT_DIR", "MAZE_OUTPUT_DIR", "MAZE_RUN_ID", "MAZE_TASK_ID")
    }

    os.environ["MAZE_WORK_DIR"] = str(work_dir)
    os.environ["MAZE_INPUT_DIR"] = str(work_dir)
    os.environ["MAZE_OUTPUT_DIR"] = str(work_dir)
    os.environ["MAZE_RUN_ID"] = run_id
    os.environ["MAZE_TASK_ID"] = task_id

    try:
        os.chdir(work_dir)
        result = task_callable(task_input_data)
    finally:
        os.chdir(previous_cwd)
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    manifest = _collect_output_manifest(file_context, work_dir, before)
    return {
        TASK_RESULT_ENVELOPE: True,
        "result": result,
        "file_manifest": manifest,
    }
