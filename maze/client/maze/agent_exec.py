from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

from maze.client.maze.agent_permissions import permission_error_payload
from maze.client.maze.agent_sandbox import (
    build_workspace_sandbox,
    detect_agent_sandbox_capabilities,
    resolve_workspace_file as sandbox_resolve_workspace_file,
    resolve_workspace_files_dir as sandbox_resolve_workspace_files_dir,
    runtime_node_metadata,
)


DEFAULT_EXEC_TIMEOUT_SECONDS = 60
DEFAULT_MAX_CODE_BYTES = 200000
DEFAULT_MAX_OUTPUT_CHARS = 12000
DEFAULT_EXEC_ENV_ALLOWLIST = (
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "LD_LIBRARY_PATH",
    "CUDA_HOME",
    "CUDA_VISIBLE_DEVICES",
    "NVIDIA_VISIBLE_DEVICES",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "MAZE_RUN_ID",
    "MAZE_TASK_ID",
    "MAZE_WORK_DIR",
    "MAZE_NODE_ID",
    "MAZE_NODE_IP",
)
SENSITIVE_ENV_KEY_RE = re.compile(r"(api[_-]?key|token|secret|credential|password|passwd|auth|cookie)", re.IGNORECASE)
CANONICAL_EXEC_BACKENDS = {"workspace_sandbox", "docker"}
LEGACY_EXEC_BACKEND_ALIASES = {
    "local": "workspace_sandbox",
    "ray_docker": "docker",
}
SUPPORTED_EXEC_BACKENDS = CANONICAL_EXEC_BACKENDS | set(LEGACY_EXEC_BACKEND_ALIASES)


@dataclass
class AgentExecBackendResolution:
    requested_backend: str
    resolved_backend: str
    deprecated_backend: str | None = None
    unsupported_backend: str | None = None

    @property
    def metadata(self) -> Dict[str, Any]:
        data = {
            "requested_backend": self.requested_backend,
            "resolved_backend": self.resolved_backend,
            "sandbox_backend": self.resolved_backend,
            "scheduler": "maze_ray",
        }
        if self.deprecated_backend:
            data["deprecated_backend"] = self.deprecated_backend
        if self.unsupported_backend:
            data["unsupported_backend"] = self.unsupported_backend
        return data


@dataclass
class AgentExecFile:
    path: str
    size: int
    sha256: str
    modified: bool = False
    created: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "created": self.created,
            "modified": self.modified,
        }


@dataclass
class AgentExecResult:
    path: str
    backend: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    generated_files: List[AgentExecFile] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "backend": self.backend,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "timed_out": self.timed_out,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "generated_files": [file.to_dict() for file in self.generated_files],
            "metadata": dict(self.metadata),
        }


@dataclass
class AgentExecProjection:
    source_files_dir: Path
    output_dir: Path
    execution_dir: Path
    exec_path: Path
    normalized_path: str
    generated_code: bool
    materialized_files: List[Dict[str, Any]] = field(default_factory=list)
    before_execution: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "mode": "explicit_materialization",
            "source_files_dir": str(self.source_files_dir),
            "output_dir": str(self.output_dir),
            "execution_dir": str(self.execution_dir),
            "executed_path": self.normalized_path,
            "generated_code": self.generated_code,
            "materialized_files": [dict(item) for item in self.materialized_files],
        }


def run_agent_exec_code(
    *,
    path: str = "",
    code: str = "",
    timeout_seconds: int | float = 20,
    workspace_dir: str = "",
    backend: str = "local",
    max_output_chars: int | None = None,
    input_paths: List[str] | str | None = None,
) -> Dict[str, Any]:
    backend_info = _normalize_backend(backend)
    if not _env_flag("MAZE_ENABLE_AGENT_EXEC_CODE", True):
        return _error_result(
            path=path,
            backend=backend_info,
            error="exec_code is disabled by MAZE_ENABLE_AGENT_EXEC_CODE",
        ).to_dict()

    timeout_value = _bounded_float(
        timeout_seconds,
        default=20,
        minimum=1,
        maximum=_env_int("MAZE_AGENT_EXEC_TIMEOUT_SECONDS", DEFAULT_EXEC_TIMEOUT_SECONDS, 1, 600),
    )
    max_chars = int(max_output_chars or _env_int("MAZE_AGENT_EXEC_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS, 1, 1_000_000))
    code_text = str(code or "")
    code_size = len(code_text.encode("utf-8"))
    max_code_bytes = _env_int("MAZE_AGENT_EXEC_CODE_MAX_BYTES", DEFAULT_MAX_CODE_BYTES, 1, 5_000_000)
    if code_size > max_code_bytes:
        return _error_result(
            path=path,
            backend=backend_info,
            error=f"code is too large for exec_code ({code_size} > {max_code_bytes} bytes)",
        ).to_dict()

    try:
        sandbox = build_workspace_sandbox(workspace_dir)
        projection = _prepare_workspace_projection(
            sandbox=sandbox,
            target_path=path,
            code_text=code_text,
            input_paths=input_paths,
        )
        before = _snapshot_files(projection.output_dir)
        target_path = str(path or "").strip()
        if code_text and not target_path:
            target_path = f"generated/exec_{int(time.time() * 1000)}.py"
        if not target_path:
            return _error_result(
                path="",
                backend=backend_info,
                error="path or code is required",
            ).to_dict()

        sandbox.policy.require_allowed(
            "exec_code",
            projection.normalized_path if not code_text else f"python {projection.normalized_path}",
        )

        try:
            if backend_info.resolved_backend == "workspace_sandbox":
                result = _run_workspace_sandbox(
                    full_path=projection.exec_path,
                    normalized_path=projection.normalized_path,
                    files_dir=projection.execution_dir,
                    timeout_seconds=timeout_value,
                    max_output_chars=max_chars,
                )
            else:
                result = _run_docker(
                    normalized_path=projection.normalized_path,
                    files_dir=projection.execution_dir,
                    timeout_seconds=timeout_value,
                    max_output_chars=max_chars,
                    use_visible_gpus=backend_info.requested_backend == "ray_docker",
                )

            execution_after = _snapshot_files(projection.execution_dir)
            execution_changes = _changed_files(projection.before_execution, execution_after)
            _sync_execution_outputs(
                execution_dir=projection.execution_dir,
                output_dir=projection.output_dir,
                changed_files=execution_changes,
            )
        finally:
            shutil.rmtree(projection.execution_dir, ignore_errors=True)

        after = _snapshot_files(projection.output_dir)
        result.generated_files = _changed_files(before, after)
        result.metadata = {
            **result.metadata,
            **backend_info.metadata,
            "workspace_projection": projection.to_metadata(),
            "workspace_sandbox": {
                key: value
                for key, value in sandbox.to_dict().items()
                if key != "policy"
            },
        }
        return result.to_dict()
    except Exception as exc:
        return _error_result(
            path=path,
            backend=backend_info,
            error=str(exc),
            metadata=permission_error_payload(exc),
        ).to_dict()


def _run_workspace_sandbox(
    *,
    full_path: Path,
    normalized_path: str,
    files_dir: Path,
    timeout_seconds: float,
    max_output_chars: int,
) -> AgentExecResult:
    exec_env, env_metadata = _build_workspace_exec_env({
        "MAZE_WORK_DIR": str(files_dir),
        "MAZE_INPUT_DIR": str(files_dir),
        "MAZE_OUTPUT_DIR": str(files_dir),
    })
    started_at = time.time()
    try:
        completed = subprocess.run(
            [sys.executable, str(full_path)],
            cwd=str(files_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=exec_env,
        )
        duration_ms = int((time.time() - started_at) * 1000)
        stdout, stdout_truncated, stdout_log = _compact_output(
            completed.stdout,
            max_output_chars,
            files_dir=files_dir,
            executed_path=normalized_path,
            stream="stdout",
        )
        stderr, stderr_truncated, stderr_log = _compact_output(
            completed.stderr,
            max_output_chars,
            files_dir=files_dir,
            executed_path=normalized_path,
            stream="stderr",
        )
        return AgentExecResult(
            path=normalized_path,
            backend="workspace_sandbox",
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            metadata={
                "workdir": str(files_dir),
                "timeout_seconds": timeout_seconds,
                "env_policy": env_metadata,
                "execution": _workspace_execution_metadata(
                    argv=[sys.executable, str(full_path)],
                    cwd=files_dir,
                    duration_ms=duration_ms,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                ),
                **_runtime_metadata(),
                **_output_log_metadata(stdout_log, stderr_log),
            },
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - started_at) * 1000)
        raw_stdout = _decode_process_output(exc.stdout)
        raw_stderr = _decode_process_output(exc.stderr)
        stdout, stdout_truncated, stdout_log = _compact_output(
            raw_stdout,
            max_output_chars,
            files_dir=files_dir,
            executed_path=normalized_path,
            stream="stdout",
        )
        stderr, stderr_truncated, stderr_log = _compact_output(
            raw_stderr,
            max_output_chars,
            files_dir=files_dir,
            executed_path=normalized_path,
            stream="stderr",
        )
        return AgentExecResult(
            path=normalized_path,
            backend="workspace_sandbox",
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            error=f"Execution timed out after {timeout_seconds:g} seconds",
            timed_out=True,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            metadata={
                "workdir": str(files_dir),
                "timeout_seconds": timeout_seconds,
                "env_policy": env_metadata,
                "execution": _workspace_execution_metadata(
                    argv=[sys.executable, str(full_path)],
                    cwd=files_dir,
                    duration_ms=duration_ms,
                    stdout=raw_stdout,
                    stderr=raw_stderr,
                    stdout_truncated=stdout_truncated,
                    stderr_truncated=stderr_truncated,
                ),
                **_runtime_metadata(),
                **_output_log_metadata(stdout_log, stderr_log),
            },
        )


def _run_docker(
    *,
    normalized_path: str,
    files_dir: Path,
    timeout_seconds: float,
    max_output_chars: int,
    use_visible_gpus: bool,
) -> AgentExecResult:
    container = None
    image = os.environ.get("MAZE_AGENT_EXEC_DOCKER_IMAGE", "python:3.11-slim")
    try:
        import docker

        client = docker.from_env()
        client.ping()
        image_error = _ensure_docker_image(client, docker, image)
        if image_error:
            return _error_result(
                path=normalized_path,
                backend="docker",
                error=f"docker exec backend failed: {image_error}",
                metadata={"docker_image": image, **_runtime_metadata()},
            )

        run_kwargs: Dict[str, Any] = {}
        visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
        if use_visible_gpus and visible_devices:
            device_ids = [
                item.strip()
                for item in visible_devices.split(",")
                if item.strip() and item.strip() not in {"-1", "none", "None"}
            ]
            if device_ids:
                run_kwargs["device_requests"] = [
                    docker.types.DeviceRequest(device_ids=device_ids, capabilities=[["gpu"]])
                ]

        container = client.containers.run(
            image=image,
            command=["python", f"/workspace/{normalized_path}"],
            working_dir="/workspace",
            volumes={str(files_dir): {"bind": "/workspace", "mode": "rw"}},
            detach=True,
            remove=False,
            network_disabled=not _env_flag("MAZE_AGENT_EXEC_DOCKER_NETWORK", False),
            mem_limit=f"{_env_int('MAZE_AGENT_EXEC_DOCKER_MEMORY_MB', 512, 64, 65536)}m",
            **run_kwargs,
        )
        try:
            status = container.wait(timeout=timeout_seconds)
            returncode = int(status.get("StatusCode", 1))
            timed_out = False
            error = None
        except Exception:
            timed_out = True
            returncode = None
            error = f"Execution timed out after {timeout_seconds:g} seconds"
            try:
                container.kill()
            except Exception:
                pass

        logs = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        stdout, stdout_truncated, stdout_log = _compact_output(
            logs,
            max_output_chars,
            files_dir=files_dir,
            executed_path=normalized_path,
            stream="stdout",
        )
        return AgentExecResult(
            path=normalized_path,
            backend="docker",
            returncode=returncode,
            stdout=stdout,
            stderr="",
            error=error,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=False,
            metadata={
                "workdir": str(files_dir),
                "timeout_seconds": timeout_seconds,
                "docker_image": image,
                "docker_pull": _env_flag("MAZE_AGENT_EXEC_DOCKER_PULL", False),
                "container_id": getattr(container, "id", None),
                **_runtime_metadata(),
                **_output_log_metadata(stdout_log, None),
            },
        )
    except Exception as exc:
        return _error_result(
            path=normalized_path,
            backend="docker",
            error=f"docker exec backend failed: {exc}",
            metadata={"docker_image": image, **_runtime_metadata()},
        )
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass


def _ensure_docker_image(client: Any, docker_module: Any, image: str) -> str | None:
    try:
        client.images.get(image)
        return None
    except Exception as exc:
        if not _is_docker_image_not_found(docker_module, exc):
            return f"failed to inspect Docker image {image!r}: {exc}"

    if not _env_flag("MAZE_AGENT_EXEC_DOCKER_PULL", False):
        return (
            f"Docker image {image!r} is not available locally; "
            "pre-pull it or set MAZE_AGENT_EXEC_DOCKER_PULL=1"
        )

    try:
        client.images.pull(image)
        return None
    except Exception as exc:
        return f"failed to pull Docker image {image!r}: {exc}"


def _is_docker_image_not_found(docker_module: Any, exc: Exception) -> bool:
    errors = getattr(docker_module, "errors", None)
    image_not_found = getattr(errors, "ImageNotFound", None)
    not_found = getattr(errors, "NotFound", None)
    if image_not_found and isinstance(exc, image_not_found):
        return True
    if not_found and isinstance(exc, not_found):
        return True
    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    return "imagenotfound" in name or "notfound" in name or "no such image" in message


def _normalize_backend(backend: str) -> AgentExecBackendResolution:
    requested = str(backend or os.environ.get("MAZE_AGENT_EXEC_BACKEND") or "workspace_sandbox").strip().lower().replace("-", "_")
    if requested in CANONICAL_EXEC_BACKENDS:
        return AgentExecBackendResolution(
            requested_backend=requested,
            resolved_backend=requested,
        )
    if requested in LEGACY_EXEC_BACKEND_ALIASES:
        return AgentExecBackendResolution(
            requested_backend=requested,
            resolved_backend=LEGACY_EXEC_BACKEND_ALIASES[requested],
            deprecated_backend=requested,
        )
    return AgentExecBackendResolution(
        requested_backend=requested,
        resolved_backend="workspace_sandbox",
        unsupported_backend=requested,
    )


def _error_result(
    *,
    path: str,
    backend: str | AgentExecBackendResolution,
    error: str,
    metadata: Dict[str, Any] | None = None,
) -> AgentExecResult:
    backend_info = backend if isinstance(backend, AgentExecBackendResolution) else _normalize_backend(backend)
    return AgentExecResult(
        path=str(path or ""),
        backend=backend_info.resolved_backend,
        returncode=None,
        stdout="",
        stderr="",
        error=error,
        metadata={
            **backend_info.metadata,
            **(metadata or {}),
        },
    )


def _prepare_workspace_projection(
    *,
    sandbox: Any,
    target_path: str,
    code_text: str,
    input_paths: List[str] | str | None,
) -> AgentExecProjection:
    source_files_dir = sandbox.files_dir
    output_dir = sandbox.run_work_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    execution_dir = Path(tempfile.mkdtemp(prefix=_exec_temp_prefix()))

    requested_path = str(target_path or "").strip()
    if code_text and not requested_path:
        requested_path = f"generated/exec_{int(time.time() * 1000)}.py"
    if not requested_path:
        raise ValueError("path or code is required")

    permission = "write" if code_text else "read"
    _, normalized, _ = sandbox_resolve_workspace_file(
        requested_path,
        source_files_dir,
        policy=sandbox.policy,
        permission=permission,
    )

    materialized_files: List[Dict[str, Any]] = []
    materialized_paths: set[str] = set()

    if not code_text:
        _materialize_projection_file(
            sandbox=sandbox,
            normalized_path=normalized,
            execution_dir=execution_dir,
            materialized_files=materialized_files,
            materialized_paths=materialized_paths,
            reason="entrypoint",
        )

    for input_path in _normalize_input_paths(input_paths):
        _, input_normalized, _ = sandbox_resolve_workspace_file(
            input_path,
            source_files_dir,
            policy=sandbox.policy,
            permission="read",
        )
        _materialize_projection_file(
            sandbox=sandbox,
            normalized_path=input_normalized,
            execution_dir=execution_dir,
            materialized_files=materialized_files,
            materialized_paths=materialized_paths,
            reason="input",
        )

    before_execution = _snapshot_files(execution_dir)
    exec_path = execution_dir / normalized
    if code_text:
        exec_path.parent.mkdir(parents=True, exist_ok=True)
        exec_path.write_text(code_text, encoding="utf-8")

    return AgentExecProjection(
        source_files_dir=source_files_dir,
        output_dir=output_dir,
        execution_dir=execution_dir,
        exec_path=exec_path,
        normalized_path=normalized,
        generated_code=bool(code_text),
        materialized_files=materialized_files,
        before_execution=before_execution,
    )


def _exec_temp_prefix() -> str:
    run_id = _safe_temp_token(os.environ.get("MAZE_RUN_ID") or "manual")
    task_id = _safe_temp_token(os.environ.get("MAZE_TASK_ID") or "exec")
    return f"maze-exec-{run_id}-{task_id}-"


def _safe_temp_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "")).strip("-")[:64] or "x"


def _normalize_input_paths(input_paths: List[str] | str | None) -> List[str]:
    if input_paths is None:
        return []
    if isinstance(input_paths, str):
        raw_items = re.split(r"[,\n]+", input_paths)
    elif isinstance(input_paths, (list, tuple, set)):
        raw_items = list(input_paths)
    else:
        raw_items = [input_paths]

    normalized = []
    for item in raw_items:
        value = str(item or "").strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _materialize_projection_file(
    *,
    sandbox: Any,
    normalized_path: str,
    execution_dir: Path,
    materialized_files: List[Dict[str, Any]],
    materialized_paths: set[str],
    reason: str,
) -> None:
    if normalized_path in materialized_paths:
        return

    source, source_scope = _projection_source_for_path(sandbox, normalized_path)
    if not source.exists():
        raise FileNotFoundError(f"Workspace file not found for exec_code materialization: {normalized_path}")
    if not source.is_file():
        raise IsADirectoryError(f"exec_code materialization currently requires file paths: {normalized_path}")

    target = execution_dir / normalized_path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    materialized_paths.add(normalized_path)
    stat = target.stat()
    materialized_files.append({
        "path": normalized_path,
        "reason": reason,
        "source": source_scope,
        "size": stat.st_size,
        "sha256": _sha256_file(target),
    })


def _projection_source_for_path(sandbox: Any, normalized_path: str) -> Tuple[Path, str]:
    candidates = [
        (sandbox.run_work_dir, "run_work_dir"),
        (sandbox.files_dir, "workspace_files"),
    ]
    fallback = None
    for base_dir, source_scope in candidates:
        source, _, _ = sandbox_resolve_workspace_file(normalized_path, base_dir)
        if fallback is None:
            fallback = (source, source_scope)
        if source.exists():
            return source, source_scope
    assert fallback is not None
    return fallback


def _sync_execution_outputs(
    *,
    execution_dir: Path,
    output_dir: Path,
    changed_files: List[AgentExecFile],
) -> None:
    for file_info in changed_files:
        source = execution_dir / file_info.path
        if not source.exists() or not source.is_file():
            continue
        target = output_dir / file_info.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _resolve_workspace_files_dir(workspace_dir: str = "") -> Path:
    work_dir = os.environ.get("MAZE_WORK_DIR")
    if work_dir:
        resolved = Path(work_dir).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    requested = str(workspace_dir or "").strip()
    if not requested:
        requested = os.environ.get("MAZE_WORKSPACE_DIR") or str(Path.cwd() / "workspace")
    root = Path(requested).expanduser().resolve()
    files_dir = (root / "files").resolve()
    files_dir.mkdir(parents=True, exist_ok=True)
    return files_dir


def _resolve_workspace_file(path: str, files_dir: Path) -> Tuple[Path, str]:
    cleaned = str(path or "").strip().replace("\\", "/").lstrip("/")
    normalized = os.path.normpath(cleaned).replace("\\", "/")
    if not normalized or normalized == ".":
        raise ValueError("path is required")
    if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        raise ValueError("path must stay inside workspace/files")

    full_path = (files_dir / normalized).resolve()
    if full_path != files_dir and not str(full_path).startswith(str(files_dir) + os.sep):
        raise ValueError("path must stay inside workspace/files")
    return full_path, normalized


def _snapshot_files(files_dir: Path) -> Dict[str, Dict[str, Any]]:
    snapshot: Dict[str, Dict[str, Any]] = {}
    if not files_dir.exists():
        return snapshot

    for file_path in files_dir.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            stat = file_path.stat()
            relative = file_path.relative_to(files_dir).as_posix()
            snapshot[relative] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": _sha256_file(file_path),
            }
        except OSError:
            continue
    return snapshot


def _changed_files(
    before: Dict[str, Dict[str, Any]],
    after: Dict[str, Dict[str, Any]],
) -> List[AgentExecFile]:
    files: List[AgentExecFile] = []
    for path, current in sorted(after.items()):
        previous = before.get(path)
        created = previous is None
        modified = (not created) and (
            previous.get("size") != current.get("size")
            or previous.get("sha256") != current.get("sha256")
        )
        if created or modified:
            files.append(AgentExecFile(
                path=path,
                size=int(current["size"]),
                sha256=str(current["sha256"]),
                created=created,
                modified=modified,
            ))
    return files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _compact_output(
    value: Any,
    max_chars: int,
    *,
    files_dir: Path,
    executed_path: str,
    stream: str,
) -> Tuple[str, bool, str | None]:
    text = _decode_process_output(value)
    if len(text) <= max_chars:
        return text, False, None
    log_path = _write_output_log(files_dir, executed_path, stream, text)
    return text[-max_chars:], True, log_path


def _write_output_log(files_dir: Path, executed_path: str, stream: str, text: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", executed_path).strip("-") or "exec"
    relative_path = f"logs/exec_code/{safe_name}.{int(time.time() * 1000)}.{stream}"
    target = files_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return relative_path


def _output_log_metadata(stdout_log: str | None, stderr_log: str | None) -> Dict[str, Any]:
    logs = {}
    if stdout_log:
        logs["stdout_log_path"] = stdout_log
    if stderr_log:
        logs["stderr_log_path"] = stderr_log
    return logs


def _build_workspace_exec_env(overrides: Dict[str, str] | None = None) -> Tuple[Dict[str, str], Dict[str, Any]]:
    allowlist, source = _workspace_exec_env_allowlist()
    allowed_keys = []
    env: Dict[str, str] = {}
    for key in allowlist:
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
            allowed_keys.append(key)

    overrides = dict(overrides or {})
    for key, value in overrides.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(key or "")):
            continue
        env[str(key)] = str(value)
        if key not in allowed_keys:
            allowed_keys.append(str(key))

    allowed_key_set = set(allowed_keys)
    blocked_sensitive_keys = [
        key
        for key in os.environ
        if key not in allowed_key_set and _looks_sensitive_env_key(key)
    ]
    allowed_sensitive_keys = [
        key
        for key in allowed_keys
        if _looks_sensitive_env_key(key)
    ]
    metadata = {
        "mode": "allowlist",
        "source": source,
        "allowed_keys": sorted(allowed_keys),
        "allowed_count": len(allowed_keys),
        "blocked_sensitive_key_count": len(blocked_sensitive_keys),
        "allowed_sensitive_key_count": len(allowed_sensitive_keys),
        "override_keys": sorted(overrides),
    }
    return env, metadata


def _workspace_exec_env_allowlist() -> Tuple[List[str], str]:
    configured = os.environ.get("MAZE_AGENT_EXEC_ENV_ALLOWLIST")
    extra = _split_env_key_list(os.environ.get("MAZE_AGENT_EXEC_ENV_EXTRA", ""))
    if configured is not None:
        return _dedupe_env_keys([*_split_env_key_list(configured), *extra]), "MAZE_AGENT_EXEC_ENV_ALLOWLIST"
    return _dedupe_env_keys([*DEFAULT_EXEC_ENV_ALLOWLIST, *extra]), "default"


def _split_env_key_list(value: str) -> List[str]:
    keys: List[str] = []
    for item in re.split(r"[,\s]+", str(value or "")):
        key = item.strip()
        if key and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            keys.append(key)
    return keys


def _dedupe_env_keys(keys: List[str]) -> List[str]:
    seen = set()
    deduped = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _looks_sensitive_env_key(key: str) -> bool:
    return bool(SENSITIVE_ENV_KEY_RE.search(str(key or "")))


def _workspace_execution_metadata(
    *,
    argv: List[str],
    cwd: Path,
    duration_ms: int,
    stdout: Any,
    stderr: Any,
    stdout_truncated: bool,
    stderr_truncated: bool,
) -> Dict[str, Any]:
    stdout_text = _decode_process_output(stdout)
    stderr_text = _decode_process_output(stderr)
    return {
        "argv": list(argv),
        "cwd": str(cwd),
        "duration_ms": duration_ms,
        "stdout_bytes": len(stdout_text.encode("utf-8")),
        "stderr_bytes": len(stderr_text.encode("utf-8")),
        "stdout_truncated": bool(stdout_truncated),
        "stderr_truncated": bool(stderr_truncated),
    }


def _runtime_metadata() -> Dict[str, Any]:
    import socket

    return {
        "hostname": socket.gethostname(),
        **runtime_node_metadata(),
        "capabilities": detect_agent_sandbox_capabilities(),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", ""),
        "maze_run_id": os.environ.get("MAZE_RUN_ID", ""),
        "maze_task_id": os.environ.get("MAZE_TASK_ID", ""),
        "maze_work_dir": os.environ.get("MAZE_WORK_DIR", ""),
    }


def _decode_process_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)
