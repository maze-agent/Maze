from __future__ import annotations

import os
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Tuple

from maze.client.maze.agent_permissions import (
    AgentPermissionDecision,
    AgentPermissionPolicy,
    AgentSandboxPathError,
    normalize_workspace_relative_path,
)


@dataclass
class WorkspaceSandbox:
    workspace_root: Path
    files_dir: Path
    run_work_dir: Path
    logs_dir: Path
    artifacts_dir: Path
    policy: AgentPermissionPolicy = field(default_factory=AgentPermissionPolicy.default)
    artifact_mode: str = "dynamic_run"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_root": str(self.workspace_root),
            "files_dir": str(self.files_dir),
            "run_work_dir": str(self.run_work_dir),
            "logs_dir": str(self.logs_dir),
            "artifacts_dir": str(self.artifacts_dir),
            "artifact_mode": self.artifact_mode,
            "policy": self.policy.to_dict(),
        }


_CAPABILITY_CACHE: Dict[str, Any] = {
    "expires_at": 0.0,
    "capabilities": None,
}


def build_workspace_sandbox(workspace_dir: str = "") -> WorkspaceSandbox:
    workspace_root = resolve_workspace_root(workspace_dir)
    files_dir = (workspace_root / "files").resolve()
    files_dir.mkdir(parents=True, exist_ok=True)

    work_override = os.environ.get("MAZE_WORK_DIR")
    if work_override:
        run_work_dir = Path(work_override).expanduser().resolve()
    else:
        run_id = os.environ.get("MAZE_RUN_ID") or "manual"
        task_id = os.environ.get("MAZE_TASK_ID") or "exec"
        run_work_dir = (workspace_root / "runs" / run_id / "work" / task_id).resolve()
    run_work_dir.mkdir(parents=True, exist_ok=True)

    logs_dir = (workspace_root / "runs" / (os.environ.get("MAZE_RUN_ID") or "manual") / "logs").resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)

    artifacts_dir = (workspace_root / "runs" / (os.environ.get("MAZE_RUN_ID") or "manual") / "artifacts").resolve()
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    policy = AgentPermissionPolicy.load_for_workspace(str(workspace_root))
    return WorkspaceSandbox(
        workspace_root=workspace_root,
        files_dir=files_dir,
        run_work_dir=run_work_dir,
        logs_dir=logs_dir,
        artifacts_dir=artifacts_dir,
        policy=policy,
    )


def resolve_workspace_root(workspace_dir: str = "") -> Path:
    requested = str(workspace_dir or "").strip()
    if not requested:
        requested = os.environ.get("MAZE_WORKSPACE_DIR") or str(Path.cwd() / "workspace")
    root = Path(requested).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_workspace_files_dir(workspace_dir: str = "") -> Path:
    sandbox = build_workspace_sandbox(workspace_dir)
    if os.environ.get("MAZE_WORK_DIR"):
        return sandbox.run_work_dir
    return sandbox.files_dir


def resolve_workspace_file(
    path: str,
    base_dir: Path,
    *,
    policy: AgentPermissionPolicy | None = None,
    permission: str | None = None,
) -> Tuple[Path, str, AgentPermissionDecision | None]:
    normalized = normalize_workspace_relative_path(path)
    decision = None
    if policy is not None and permission:
        decision = policy.require_allowed(permission, normalized)

    base = Path(base_dir).expanduser().resolve()
    full_path = (base / normalized).resolve(strict=False)
    try:
        common = os.path.commonpath([str(base), str(full_path)])
    except ValueError as exc:
        raise AgentSandboxPathError("path must stay inside workspace/files", target=str(path or "")) from exc
    if common != str(base):
        raise AgentSandboxPathError("path must stay inside workspace/files", target=str(path or ""))
    return full_path, normalized, decision


def detect_agent_sandbox_capabilities(*, force: bool = False) -> Dict[str, Any]:
    now = time.time()
    ttl = _env_int("MAZE_AGENT_CAPABILITY_CACHE_SECONDS", 300, 0, 3600)
    cached = _CAPABILITY_CACHE.get("capabilities")
    if cached is not None and not force and now < float(_CAPABILITY_CACHE.get("expires_at") or 0):
        return dict(cached)

    capabilities: Dict[str, Any] = {
        "workspace_sandbox": True,
        "docker_sandbox": False,
        "docker_reason": "",
    }

    docker_bin = shutil.which("docker")
    if not docker_bin:
        capabilities["docker_reason"] = "docker CLI is not installed"
        return _cache_capabilities(capabilities, ttl)

    try:
        import docker
    except Exception as exc:
        capabilities["docker_reason"] = f"docker Python SDK is unavailable: {exc}"
        return _cache_capabilities(capabilities, ttl)

    image = os.environ.get("MAZE_AGENT_EXEC_DOCKER_IMAGE", "python:3.11-slim")
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        capabilities["docker_reason"] = f"docker daemon is not reachable: {exc}"
        return _cache_capabilities(capabilities, ttl)

    try:
        client.images.get(image)
    except Exception as exc:
        capabilities["docker_reason"] = (
            f"Docker image {image!r} is not available locally; "
            "pre-pull it or set MAZE_AGENT_EXEC_DOCKER_IMAGE to a local image"
        )
        capabilities["docker_error"] = str(exc)
        return _cache_capabilities(capabilities, ttl)

    try:
        output = client.containers.run(
            image=image,
            command=["python", "-c", "print('maze-docker-capability-ok')"],
            detach=False,
            remove=True,
            network_disabled=True,
            mem_limit="64m",
            stdout=True,
            stderr=True,
        )
        text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        if "maze-docker-capability-ok" not in text:
            capabilities["docker_reason"] = f"docker container probe returned unexpected output: {text[:200]}"
            return _cache_capabilities(capabilities, ttl)
    except Exception as exc:
        capabilities["docker_reason"] = f"docker container execution failed: {exc}"
        return _cache_capabilities(capabilities, ttl)

    capabilities["docker_sandbox"] = True
    capabilities["docker_reason"] = "docker container probe succeeded"
    return _cache_capabilities(capabilities, ttl)


def runtime_node_metadata() -> Dict[str, Any]:
    node_id = os.environ.get("MAZE_NODE_ID", "")
    node_ip = os.environ.get("MAZE_NODE_IP", "")
    try:
        import ray

        if ray.is_initialized():
            node_id = node_id or ray.get_runtime_context().get_node_id()
            node_ip = node_ip or ray.util.get_node_ip_address()
    except Exception:
        pass

    if not node_ip:
        try:
            node_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            node_ip = ""

    return {
        "node_id": node_id,
        "node_ip": node_ip,
    }


def _cache_capabilities(capabilities: Dict[str, Any], ttl: int) -> Dict[str, Any]:
    _CAPABILITY_CACHE["capabilities"] = dict(capabilities)
    _CAPABILITY_CACHE["expires_at"] = time.time() + ttl
    return dict(capabilities)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)
