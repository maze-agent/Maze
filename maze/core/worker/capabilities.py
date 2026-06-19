from __future__ import annotations

import os
import shutil
import time
from typing import Any, Dict


_CAPABILITY_CACHE: Dict[str, Any] = {
    "expires_at": 0.0,
    "capabilities": None,
}


def detect_worker_execution_capabilities(*, force: bool = False) -> Dict[str, Any]:
    now = time.time()
    ttl = _env_int("MAZE_WORKER_CAPABILITY_CACHE_SECONDS", 300, 0, 3600)
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

