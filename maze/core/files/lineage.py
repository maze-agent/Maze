from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict

from maze.core.files.artifact_store import LocalCASArtifactStore, artifact_uri


TASK_RESULT_ENVELOPE = "__maze_task_result_envelope__"


class ArtifactError(RuntimeError):
    """Raised when Maze cannot stage, upload, or reconcile task files."""


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
    files_by_path: Dict[str, Dict[str, Any]] = {}

    parent_manifests = file_context.get("parent_file_manifests")
    if parent_manifests is None:
        workspace_dir = Path(file_context["workspace_dir"])
        run_id = file_context["run_id"]
        parent_task_ids = file_context.get("parent_task_ids") or []
        manifests_dir = workspace_dir / "workflow_runs" / "static" / run_id / "file_manifests" / "tasks"
        parent_manifests = []

        for parent_task_id in parent_task_ids:
            manifest_path = manifests_dir / f"{parent_task_id}.json"
            if not manifest_path.exists():
                continue

            import json

            parent_manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))

    for manifest in parent_manifests:
        for file_info in manifest.get("files", []):
            relative_path = file_info.get("path")
            if not relative_path:
                continue
            _safe_relative_path(relative_path)
            existing = files_by_path.get(relative_path)
            if existing and existing.get("sha256") != file_info.get("sha256"):
                raise ArtifactError(
                    f"File lineage conflict for {relative_path}: "
                    f"{existing.get('producer_task_id')} and {file_info.get('producer_task_id')} produced different content."
                )
            files_by_path[relative_path] = file_info

    return files_by_path


def _artifact_mode(file_context: Dict[str, Any]) -> bool:
    return bool(file_context.get("artifact_store"))


def _artifact_base_url(file_context: Dict[str, Any]) -> str:
    artifact_store = file_context.get("artifact_store") or {}
    base_url = artifact_store.get("base_url") or file_context.get("artifact_base_url")
    if not base_url:
        raise ArtifactError("Artifact file context requires artifact_store.base_url")
    return str(base_url).rstrip("/")


def _download_artifact(file_context: Dict[str, Any], file_info: Dict[str, Any], target: Path):
    sha256 = file_info.get("sha256")
    if not sha256:
        raise ValueError(f"Artifact missing sha256 for {file_info.get('path')}")

    cache_dir = file_context.get("artifact_store", {}).get("cache_dir")
    if cache_dir:
        cache_store = LocalCASArtifactStore(cache_dir)
        if cache_store.exists(sha256):
            cache_store.get_file(sha256, target)
            return

    import requests

    try:
        response = requests.get(f"{_artifact_base_url(file_context)}/artifacts/sha256/{sha256}", timeout=60)
        response.raise_for_status()
    except Exception as exc:
        raise ArtifactError(f"Failed to download artifact {sha256} for {file_info.get('path')}: {exc}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
    tmp_path.write_bytes(response.content)
    actual = _file_sha256(tmp_path)
    if actual != sha256:
        tmp_path.unlink(missing_ok=True)
        raise ArtifactError(f"Artifact checksum mismatch for {file_info.get('path')}: expected {sha256}, got {actual}")
    os.replace(tmp_path, target)

    if cache_dir:
        cache_store.put_file(target)


def _stage_artifact_file(file_context: Dict[str, Any], file_info: Dict[str, Any], work_dir: Path):
    relative_path = file_info.get("path")
    if not relative_path:
        return
    target = work_dir / _safe_relative_path(relative_path)
    _download_artifact(file_context, file_info, target)


def _stage_initial_artifacts(file_context: Dict[str, Any], work_dir: Path):
    for file_info in file_context.get("initial_files") or []:
        _stage_artifact_file(file_context, file_info, work_dir)


def _stage_parent_files(file_context: Dict[str, Any], work_dir: Path):
    for relative_path, file_info in _load_parent_files(file_context).items():
        if _artifact_mode(file_context):
            _stage_artifact_file(file_context, file_info, work_dir)
            continue

        source = Path(file_info["storage_path"])
        if not source.exists():
            raise ArtifactError(f"Missing parent artifact for {relative_path}: {source}")
        target = work_dir / _safe_relative_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _write_manifest(file_context: Dict[str, Any], manifest: Dict[str, Any]):
    import json

    if _artifact_mode(file_context) and not file_context.get("write_local_manifest", False):
        return

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
        mime_type, _ = mimetypes.guess_type(relative_path)
        artifact_info: Dict[str, Any] = {}
        storage_path = None

        if _artifact_mode(file_context):
            artifact_info = _upload_artifact(file_context, source, file_info["sha256"])
        else:
            target = artifacts_dir / _safe_relative_path(relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            storage_path = str(target)
            artifact_info = {
                "artifact_id": f"sha256:{file_info['sha256']}",
                "storage_uri": artifact_uri(file_info["sha256"]),
            }

        file_record = {
            "path": relative_path,
            "name": Path(relative_path).name,
            "size": file_info["size"],
            "sha256": file_info["sha256"],
            "mime": mime_type or "application/octet-stream",
            "producer_task_id": task_id,
            "artifact_id": artifact_info.get("artifact_id"),
            "storage_uri": artifact_info.get("storage_uri"),
            "uri": f"maze://runs/{run_id}/artifacts/tasks/{task_id}/{relative_path}",
        }
        if storage_path:
            file_record["storage_path"] = storage_path
        files.append(file_record)

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


def _upload_artifact(file_context: Dict[str, Any], source: Path, expected_sha256: str) -> Dict[str, Any]:
    actual = _file_sha256(source)
    if actual != expected_sha256:
        raise ArtifactError(f"Artifact checksum changed before upload: expected {expected_sha256}, got {actual}")

    import requests

    try:
        with source.open("rb") as handle:
            response = requests.put(
                f"{_artifact_base_url(file_context)}/artifacts/sha256/{expected_sha256}",
                data=handle,
                headers={"Content-Type": "application/octet-stream"},
                timeout=120,
            )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise ArtifactError(f"Failed to upload artifact {expected_sha256}: {exc}") from exc


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

    try:
        if _artifact_mode(file_context):
            _stage_initial_artifacts(file_context, work_dir)
        else:
            _copy_tree_contents(workspace_dir / "files", work_dir)
        _stage_parent_files(file_context, work_dir)
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError(f"Failed to stage task files: {exc}") from exc
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

    task_exception: BaseException | None = None
    result: Dict[str, Any] | None = None
    try:
        os.chdir(work_dir)
        try:
            result = task_callable(task_input_data)
        except Exception as exc:
            task_exception = exc
    finally:
        os.chdir(previous_cwd)
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    try:
        manifest = _collect_output_manifest(file_context, work_dir, before)
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError(f"Failed to collect task output artifacts: {exc}") from exc

    if task_exception is not None:
        from maze.core.scheduler.error import exception_to_error_envelope, task_error_result

        error_result = task_error_result(
            exception_to_error_envelope(
                "user_code",
                task_exception,
                origin="runner",
            )
        )
        error_result["file_manifest"] = manifest
        return error_result

    return {
        TASK_RESULT_ENVELOPE: True,
        "result": result,
        "file_manifest": manifest,
    }
