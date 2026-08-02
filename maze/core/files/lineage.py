from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import tempfile
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
        run_id = file_context["run_id"]
        parent_task_ids = file_context.get("parent_task_ids") or []
        parent_manifests = []

        if _artifact_mode(file_context):
            if parent_task_ids:
                raise ArtifactError(
                    "Artifact task context requires explicit parent_file_manifests"
                )
            return files_by_path

        for parent_task_id in parent_task_ids:
            for manifests_dir in _task_manifest_dirs(file_context, run_id):
                manifest_path = manifests_dir / f"{parent_task_id}.json"
                if not manifest_path.exists():
                    continue

                import json

                parent_manifests.append(json.loads(manifest_path.read_text(encoding="utf-8")))
                break

    for manifest in parent_manifests:
        if manifest.get("published") is not True:
            raise ArtifactError(
                f"Parent task manifest is not published: {manifest.get('task_id')}"
            )
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


def _artifact_capability(file_context: Dict[str, Any]) -> str | None:
    capability = (file_context.get("artifact_store") or {}).get("capability")
    return str(capability) if capability else None


def _artifact_headers(file_context: Dict[str, Any]) -> Dict[str, str]:
    capability = _artifact_capability(file_context)
    if not capability:
        return {}
    return {"Authorization": f"Bearer {capability}"}


def _private_artifacts(file_context: Dict[str, Any]) -> bool:
    artifact_store = file_context.get("artifact_store") or {}
    return bool(file_context.get("private") or artifact_store.get("private"))


def _worker_artifact_temp_root(file_context: Dict[str, Any]) -> Path:
    artifact_store = file_context.get("artifact_store") or {}
    configured = (
        artifact_store.get("worker_temp_root")
        or os.environ.get("MAZE_WORKER_ARTIFACT_TMP_DIR")
    )
    root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / "maze-worker-artifacts"
    )
    if root.is_symlink():
        raise ArtifactError("Worker artifact temporary root cannot be a symbolic link")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ArtifactError("Worker artifact temporary root must be a real directory")
    os.chmod(root, 0o700)
    return root.resolve()


def _artifact_work_dir(file_context: Dict[str, Any]) -> tuple[Path, Path]:
    root = _worker_artifact_temp_root(file_context)
    task_root = Path(tempfile.mkdtemp(prefix="task-", dir=root))
    os.chmod(task_root, 0o700)
    work_dir = task_root / "work"
    work_dir.mkdir(mode=0o700)
    return task_root, work_dir


def _run_dir(file_context: Dict[str, Any], run_id: str | None = None) -> Path:
    root = file_context.get("manifest_root") or file_context.get("workspace_dir")
    if not root:
        raise ArtifactError("Local file context requires workspace_dir or manifest_root")
    workspace_dir = Path(root).expanduser().resolve()
    return workspace_dir / "runs" / (run_id or file_context["run_id"])


def _attempt_path(file_context: Dict[str, Any]) -> Path:
    attempt = file_context.get("attempt")
    dispatch_id = file_context.get("dispatch_id")
    if attempt is None or not dispatch_id:
        raise ArtifactError("Task file context requires attempt and dispatch_id")
    dispatch_path = _safe_relative_path(str(dispatch_id))
    if len(dispatch_path.parts) != 1:
        raise ValueError(f"Unsafe dispatch id: {dispatch_id}")
    return Path(f"attempt-{int(attempt)}") / dispatch_path


def _legacy_static_run_dir(file_context: Dict[str, Any], run_id: str | None = None) -> Path:
    workspace_dir = Path(file_context["workspace_dir"]).expanduser().resolve()
    return workspace_dir / "workflow_runs" / "static" / (run_id or file_context["run_id"])


def _task_manifest_dir(file_context: Dict[str, Any], run_id: str | None = None) -> Path:
    return _run_dir(file_context, run_id) / "file_manifests" / "tasks"


def _task_manifest_dirs(file_context: Dict[str, Any], run_id: str | None = None) -> list[Path]:
    return [
        _task_manifest_dir(file_context, run_id),
        _legacy_static_run_dir(file_context, run_id) / "file_manifests" / "tasks",
    ]


def _download_artifact(file_context: Dict[str, Any], file_info: Dict[str, Any], target: Path):
    sha256 = file_info.get("sha256")
    if not sha256:
        raise ValueError(f"Artifact missing sha256 for {file_info.get('path')}")

    cache_dir = file_context.get("artifact_store", {}).get("cache_dir")
    if _private_artifacts(file_context):
        cache_dir = None
    if cache_dir:
        cache_store = LocalCASArtifactStore(cache_dir)
        if cache_store.exists(sha256):
            cache_store.get_file(sha256, target)
            return

    import requests

    try:
        response = requests.get(
            f"{_artifact_base_url(file_context)}/artifacts/sha256/{sha256}",
            headers=_artifact_headers(file_context),
            timeout=60,
        )
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

    if _artifact_mode(file_context) and not file_context.get("manifest_root"):
        return

    run_id = file_context["run_id"]
    task_id = file_context["task_id"]
    manifests_dir = _task_manifest_dir(file_context, run_id)
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifests_dir / f"{task_id}.json"
    tmp_path = manifest_path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
    tmp_path.write_text(f"{json.dumps(manifest, indent=2, ensure_ascii=False)}\n", encoding="utf-8")
    os.replace(tmp_path, manifest_path)


def publish_task_file_manifest(
    file_context: Dict[str, Any],
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(file_context, dict) or not file_context.get("enabled"):
        raise ArtifactError("Published task manifest requires an enabled file context")
    if not isinstance(manifest, dict) or manifest.get("published") is not True:
        raise ArtifactError("Only an accepted task manifest can be published")

    for field in ("run_id", "task_id", "attempt", "dispatch_id", "lease_id"):
        expected = file_context.get(field)
        if expected is None or manifest.get(field) != expected:
            raise ArtifactError(
                f"Published task manifest {field} does not match its accepted attempt"
            )

    published = dict(manifest)
    _write_manifest(file_context, published)
    return published


def _collect_output_manifest(file_context: Dict[str, Any], work_dir: Path, before: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    run_id = file_context["run_id"]
    task_id = file_context["task_id"]
    after = _snapshot_files(work_dir)
    artifacts_dir = None
    if not _artifact_mode(file_context):
        artifacts_dir = (
            _run_dir(file_context, run_id)
            / "artifacts"
            / "tasks"
            / task_id
            / _attempt_path(file_context)
        )
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
            assert artifacts_dir is not None
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
            "private": bool(artifact_info.get("private") or _private_artifacts(file_context)),
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
        "attempt": file_context.get("attempt"),
        "dispatch_id": file_context.get("dispatch_id"),
        "lease_id": file_context.get("lease_id"),
        "published": False,
        "node_id": file_context.get("node_id"),
        "created_time": time.time(),
        "files": files,
        "deleted_files": deleted_files,
    }
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
                headers={
                    "Content-Type": "application/octet-stream",
                    **_artifact_headers(file_context),
                },
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

    artifact_mode = _artifact_mode(file_context)
    workspace_dir = None if artifact_mode else Path(file_context["workspace_dir"]).resolve()
    run_id = file_context["run_id"]
    task_id = file_context["task_id"]
    task_root = None
    if artifact_mode:
        task_root, work_dir = _artifact_work_dir(file_context)
    else:
        work_dir = (
            _run_dir(file_context, run_id)
            / "work"
            / "tasks"
            / task_id
            / _attempt_path(file_context)
        )
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

    try:
        try:
            if artifact_mode:
                _stage_initial_artifacts(file_context, work_dir)
            else:
                assert workspace_dir is not None
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
            for key in (
                "MAZE_WORK_DIR",
                "MAZE_INPUT_DIR",
                "MAZE_OUTPUT_DIR",
                "MAZE_RUN_ID",
                "MAZE_TASK_ID",
            )
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
            raise ArtifactError(
                f"Failed to collect task output artifacts: {exc}"
            ) from exc

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
    finally:
        if task_root is not None:
            try:
                shutil.rmtree(task_root)
            except Exception as exc:
                raise ArtifactError(
                    "Failed to clean Worker artifact temporary directory"
                ) from exc
            if task_root.exists():
                raise ArtifactError(
                    "Worker artifact temporary directory still exists after cleanup"
                )
