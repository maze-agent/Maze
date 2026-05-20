"""
Built-in agent utility tasks for workspace file and code execution workflows.
"""

from maze.client.front.decorator import task


def _resolve_workspace_files_dir(workspace_dir: str = ""):
    import os

    requested = str(workspace_dir or "").strip()
    if not requested:
        requested = os.environ.get("MAZE_WORKSPACE_DIR") or os.path.join(os.getcwd(), "workspace")

    root = os.path.abspath(os.path.expanduser(requested))
    files_dir = os.path.abspath(os.path.join(root, "files"))
    os.makedirs(files_dir, exist_ok=True)
    return files_dir


def _resolve_workspace_file(path: str, workspace_dir: str = ""):
    import os

    files_dir = _resolve_workspace_files_dir(workspace_dir)
    cleaned = str(path or "").strip().replace("\\", "/").lstrip("/")
    normalized = os.path.normpath(cleaned).replace("\\", "/")
    if not normalized or normalized == ".":
        raise ValueError("path is required")
    if normalized == ".." or normalized.startswith("../") or "/../" in normalized:
        raise ValueError("path must stay inside workspace/files")

    full_path = os.path.abspath(os.path.join(files_dir, normalized))
    if full_path != files_dir and not full_path.startswith(files_dir + os.sep):
        raise ValueError("path must stay inside workspace/files")
    return full_path, normalized, files_dir


@task(
    data_types={"path": "str", "content": "str", "append": "bool", "workspace_dir": "str"},
    resources={"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0},
)
def write_file(path: str, content: str, append: bool = False, workspace_dir: str = ""):
    """Write text content to a file under workspace/files."""
    try:
        import os

        full_path, normalized, _ = _resolve_workspace_file(path, workspace_dir)
        append_flag = append
        if isinstance(append_flag, str):
            append_flag = append_flag.strip().lower() in {"1", "true", "yes", "y", "on"}
        else:
            append_flag = bool(append_flag)

        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        mode = "a" if append_flag else "w"
        text = str(content or "")
        with open(full_path, mode, encoding="utf-8") as handle:
            handle.write(text)

        return {
            "path": normalized,
            "bytes": len(text.encode("utf-8")),
            "appended": append_flag,
            "error": None,
        }
    except Exception as exc:
        return {
            "path": str(path or ""),
            "bytes": 0,
            "appended": False,
            "error": str(exc),
        }


@task(
    data_types={"path": "str", "max_bytes": "int", "workspace_dir": "str"},
    resources={"cpu": 1, "cpu_mem": 64, "gpu": 0, "gpu_mem": 0},
)
def read_file(path: str, max_bytes: int = 20000, workspace_dir: str = ""):
    """Read text content from a file under workspace/files."""
    try:
        full_path, normalized, _ = _resolve_workspace_file(path, workspace_dir)
        limit = int(max_bytes or 20000)
        limit = min(max(limit, 1), 200000)
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
        }
    except Exception as exc:
        return {
            "path": str(path or ""),
            "content": "",
            "bytes": 0,
            "truncated": False,
            "error": str(exc),
        }


@task(
    data_types={"path": "str", "code": "str", "timeout_seconds": "int", "workspace_dir": "str"},
    resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0},
)
def exec_code(path: str = "", code: str = "", timeout_seconds: int = 20, workspace_dir: str = ""):
    """Run a Python file under workspace/files, optionally writing code first."""
    try:
        import os
        import subprocess
        import sys
        import time

        timeout_value = float(timeout_seconds or 20)
        timeout_value = min(max(timeout_value, 1), 60)
        code_text = str(code or "")
        target_path = str(path or "").strip()

        if code_text and not target_path:
            target_path = f"generated/exec_{int(time.time() * 1000)}.py"

        if not target_path:
            return {
                "path": "",
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "error": "path or code is required",
            }

        full_path, normalized, files_dir = _resolve_workspace_file(target_path, workspace_dir)
        if code_text:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as handle:
                handle.write(code_text)

        completed = subprocess.run(
            [sys.executable, full_path],
            cwd=files_dir,
            capture_output=True,
            text=True,
            timeout=timeout_value,
        )
        return {
            "path": normalized,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-12000:],
            "stderr": completed.stderr[-12000:],
            "error": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "path": str(path or ""),
            "returncode": None,
            "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-12000:] if isinstance(exc.stderr, str) else "",
            "error": f"Execution timed out after {timeout_seconds} seconds",
        }
    except Exception as exc:
        return {
            "path": str(path or ""),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": str(exc),
        }
