"""
Built-in agent utility tasks for workspace file and code execution workflows.
"""

from maze.client.front.decorator import task
from maze.client.maze.agent_permissions import permission_error_payload
from maze.client.maze.agent_sandbox import build_workspace_sandbox
from maze.client.maze.agent_sandbox import resolve_workspace_file as sandbox_resolve_workspace_file


def _env_flag(name: str, default: bool = True) -> bool:
    import os

    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    import os

    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _resolve_workspace_files_dir(workspace_dir: str = ""):
    sandbox = build_workspace_sandbox(workspace_dir)
    return str(sandbox.files_dir)


def _resolve_workspace_file(path: str, workspace_dir: str = "", permission: str = ""):
    sandbox = build_workspace_sandbox(workspace_dir)
    full_path, normalized, decision = sandbox_resolve_workspace_file(
        path,
        sandbox.files_dir,
        policy=sandbox.policy,
        permission=permission or None,
    )
    return str(full_path), normalized, str(sandbox.files_dir), decision


@task(
    data_types={"path": "str", "content": "str", "append": "bool", "workspace_dir": "str"},
    resources={"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0},
)
def write_file(path: str, content: str, append: bool = False, workspace_dir: str = ""):
    """Write text content to a file under workspace/files."""
    try:
        import os

        full_path, normalized, _, decision = _resolve_workspace_file(path, workspace_dir, "write")
        append_flag = append
        if isinstance(append_flag, str):
            append_flag = append_flag.strip().lower() in {"1", "true", "yes", "y", "on"}
        else:
            append_flag = bool(append_flag)

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        mode = "a" if append_flag else "w"
        text = str(content or "")
        max_bytes = _env_int("MAZE_AGENT_WRITE_MAX_BYTES", 200000, 1, 5_000_000)
        text_bytes = text.encode("utf-8")
        if len(text_bytes) > max_bytes:
            raise ValueError(f"content is too large for write_file ({len(text_bytes)} > {max_bytes} bytes)")
        with open(full_path, mode, encoding="utf-8") as handle:
            handle.write(text)

        return {
            "path": normalized,
            "bytes": len(text_bytes),
            "appended": append_flag,
            "error": None,
            "metadata": {
                "permission": decision.to_dict() if decision is not None else None,
            },
        }
    except Exception as exc:
        return {
            "path": str(path or ""),
            "bytes": 0,
            "appended": False,
            "error": str(exc),
            "metadata": permission_error_payload(exc),
        }


@task(
    data_types={"path": "str", "max_bytes": "int", "workspace_dir": "str"},
    resources={"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0},
)
def read_file(path: str, max_bytes: int = 20000, workspace_dir: str = ""):
    """Read text content from a file under workspace/files."""
    try:
        full_path, normalized, _, decision = _resolve_workspace_file(path, workspace_dir, "read")
        limit = int(max_bytes or 20000)
        env_limit = _env_int("MAZE_AGENT_READ_MAX_BYTES", 200000, 1, 5_000_000)
        limit = min(max(limit, 1), env_limit)
        with open(full_path, "rb") as handle:
            raw = handle.read(limit + 1)
        truncated = len(raw) > limit
        content = raw[:limit].decode("utf-8", errors="replace")
        return {
            "path": normalized,
            "content": content,
            "bytes": len(raw[:limit]),
            "truncated": truncated,
            "error": None,
            "metadata": {
                "permission": decision.to_dict() if decision is not None else None,
            },
        }
    except Exception as exc:
        return {
            "path": str(path or ""),
            "content": "",
            "bytes": 0,
            "truncated": False,
            "error": str(exc),
            "metadata": permission_error_payload(exc),
        }


@task(
    data_types={
        "path": "str",
        "code": "str",
        "timeout_seconds": "int",
        "workspace_dir": "str",
        "backend": "str",
        "input_paths": "list",
        "cpu": "int",
        "cpu_mem": "int",
        "gpu": "int",
        "gpu_mem": "int",
        "target_node_id": "str",
    },
    resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0},
)
def exec_code(
    path: str = "",
    code: str = "",
    timeout_seconds: int = 20,
    workspace_dir: str = "",
    backend: str = "workspace_sandbox",
    input_paths: list | str | None = None,
    cpu: int = 1,
    cpu_mem: int = 128,
    gpu: int = 0,
    gpu_mem: int = 0,
    target_node_id: str = "",
):
    """Run a Python file under workspace/files, optionally writing code first."""
    from maze.client.maze.agent_exec import run_agent_exec_code

    result = run_agent_exec_code(
        path=path,
        code=code,
        timeout_seconds=timeout_seconds,
        workspace_dir=workspace_dir,
        backend=backend,
        input_paths=input_paths,
    )
    metadata = dict(result.get("metadata", {}) or {})
    metadata["resource_request"] = {
        "cpu": cpu,
        "cpu_mem": cpu_mem,
        "gpu": gpu,
        "gpu_mem": gpu_mem,
        "target_node_id": target_node_id,
    }
    return {
        "path": result.get("path", str(path or "")),
        "backend": result.get("backend", backend),
        "returncode": result.get("returncode"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "error": result.get("error"),
        "timed_out": result.get("timed_out", False),
        "stdout_truncated": result.get("stdout_truncated", False),
        "stderr_truncated": result.get("stderr_truncated", False),
        "generated_files": result.get("generated_files", []),
        "metadata": metadata,
    }
