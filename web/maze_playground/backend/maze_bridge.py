"""Small Python bridge for Playground workspace task authoring."""

import hashlib
import importlib.util
import inspect
import io
import json
import os
import re
import sys
import tempfile
import traceback


sys.dont_write_bytecode = True
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.insert(0, PROJECT_ROOT)

WORKSPACE_ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("MAZE_WORKSPACE_ROOT_DIR")
    or os.environ.get("MAZE_WORKSPACE_DIR")
    or os.path.join(PROJECT_ROOT, "workspaces")
))
WORKSPACES_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("MAZE_WORKSPACES_DIR") or WORKSPACE_ROOT
))
DEFAULT_WORKSPACE_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("MAZE_DEFAULT_WORKSPACE_DIR")
    or os.path.join(WORKSPACES_DIR, os.environ.get("MAZE_DEFAULT_WORKSPACE_ID", "default"))
))

from maze import get_task_metadata


def _task_description(func, metadata):
    description = inspect.getdoc(func) or ""
    if description:
        return description
    inputs = ", ".join(metadata.inputs) or "none"
    outputs = ", ".join(metadata.outputs) or "none"
    return f"Inputs: {inputs}. Outputs: {outputs}."


def _task_metadata_payload(func, name, code, workspace_dir=None, relative_path=None):
    metadata = get_task_metadata(func)
    payload = {
        "name": name,
        "displayName": name.replace("_", " ").title(),
        "description": _task_description(func, metadata),
        "inputs": [
            {"name": value, "dataType": metadata.data_types.get(value, "str")}
            for value in metadata.inputs
        ],
        "outputs": [
            {"name": value, "dataType": metadata.data_types.get(value, "str")}
            for value in metadata.outputs
        ],
        "resources": metadata.resources,
        "taskKind": metadata.task_kind,
        "functionName": name,
        "code": code,
        "codeStr": metadata.code_str,
        "codeSer": metadata.code_ser,
        "modelAnchor": getattr(metadata, "model_anchor", None),
        "maxRetries": metadata.max_retries,
        "retryBackoffSeconds": metadata.retry_backoff_seconds,
        "retryOn": metadata.retry_on,
        "timeoutSeconds": metadata.timeout_seconds,
    }
    if workspace_dir is not None:
        payload["workspaceDir"] = workspace_dir
    if relative_path is not None:
        payload["relativePath"] = relative_path
    return payload


def _load_module(file_path, workspace_dir):
    module_name = f"maze_workspace_task_{hashlib.sha1(file_path.encode()).hexdigest()[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    try:
        sys.path[:0] = [workspace_dir, os.path.dirname(file_path)]
        spec.loader.exec_module(module)
    finally:
        sys.path = original_path
    return module


def _extract_tasks(file_path, workspace_dir, relative_path):
    with open(file_path, "r", encoding="utf-8") as handle:
        code = handle.read()
    module = _load_module(file_path, workspace_dir)
    tasks = [
        _task_metadata_payload(
            value,
            name,
            code,
            workspace_dir=workspace_dir,
            relative_path=relative_path,
        )
        for name, value in inspect.getmembers(module)
        if hasattr(value, "_maze_task_metadata")
    ]
    if len(tasks) > 1:
        names = ", ".join(task["functionName"] for task in tasks)
        raise ValueError(
            f"Workspace task files must define exactly one @task function. "
            f"{relative_path} defines {len(tasks)} tasks: {names}"
        )
    return tasks


def parse_custom_function(code):
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(code)
            temp_path = handle.name
        module = _load_module(temp_path, PROJECT_ROOT)
        for name, value in inspect.getmembers(module):
            if hasattr(value, "_maze_task_metadata"):
                return _task_metadata_payload(value, name, code)
        return {"error": "No function decorated with @task found"}
    except SyntaxError as exc:
        return {"error": f"Syntax error: {exc}", "traceback": traceback.format_exc()}
    except ImportError as exc:
        return {
            "error": f"Import failed: {exc}. Please use 'from maze import task'",
            "traceback": traceback.format_exc(),
        }
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _workspace_id_to_dir(workspace_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(workspace_id or "").strip()).strip("-")
    return os.path.join(WORKSPACES_DIR, safe[:80] or "default")


def _resolve_workspace_dir(workspace_dir=None, workspace_id=None):
    raw = str(workspace_dir or "").strip()
    if workspace_id and not raw:
        raw = _workspace_id_to_dir(workspace_id)
    if not raw:
        raw = DEFAULT_WORKSPACE_DIR
    if sys.platform != "win32" and (re.match(r"^[A-Za-z]:[\\/]", raw) or "\\" in raw):
        return None, {"error": "Workspace paths must use POSIX-style paths on this service"}
    if not os.path.isabs(os.path.expanduser(raw)) and "/" not in raw and "\\" not in raw:
        raw = _workspace_id_to_dir(raw)
    resolved = os.path.abspath(os.path.expanduser(raw))
    if resolved == PROJECT_ROOT:
        return None, {"error": "Project root cannot be used as a workspace directory"}
    try:
        for name in ("files", "workflows", "tasks", "policies", "runs"):
            os.makedirs(os.path.join(resolved, name), exist_ok=True)
    except Exception as exc:
        return None, {"error": f"Failed to initialize workspace directory {resolved}: {exc}"}
    return resolved, None


def _task_file_path(workspace_dir, relative_path):
    relative = (relative_path or "tasks/custom_task.py").replace("\\", "/").strip().lstrip("/")
    if not relative.startswith("tasks/"):
        relative = f"tasks/{relative}"
    relative = os.path.normpath(relative).replace("\\", "/")
    if not relative.endswith(".py"):
        relative = f"{relative}.py"
    full_path = os.path.abspath(os.path.join(workspace_dir, relative))
    tasks_dir = os.path.abspath(os.path.join(workspace_dir, "tasks"))
    if not full_path.startswith(tasks_dir + os.sep):
        raise ValueError("Task path must stay inside the workspace tasks directory")
    return relative, full_path


def get_workspace_tasks(workspace_dir):
    workspace_dir, error = _resolve_workspace_dir(workspace_dir)
    if error:
        return error
    tasks_dir = os.path.join(workspace_dir, "tasks")
    tasks = []
    errors = []
    for root, _, files in os.walk(tasks_dir):
        for name in files:
            if not name.endswith(".py") or name.startswith("__"):
                continue
            file_path = os.path.join(root, name)
            relative_path = os.path.relpath(file_path, workspace_dir).replace("\\", "/")
            try:
                tasks.extend(_extract_tasks(file_path, workspace_dir, relative_path))
            except Exception as exc:
                errors.append({
                    "relativePath": relative_path,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                })
    return {"workspaceDir": workspace_dir, "tasksDir": tasks_dir, "tasks": tasks, "errors": errors}


def save_workspace_task(workspace_dir, relative_path, code, parse=True):
    workspace_dir, error = _resolve_workspace_dir(workspace_dir)
    if error:
        return error
    if code is None or (parse and not code.strip()):
        return {"error": "Task code cannot be empty"}
    try:
        relative_path, file_path = _task_file_path(workspace_dir, relative_path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write(code)
        response = {
            "success": True,
            "workspaceDir": workspace_dir,
            "tasksDir": os.path.join(workspace_dir, "tasks"),
            "relativePath": relative_path,
        }
        if parse:
            tasks = _extract_tasks(file_path, workspace_dir, relative_path)
            if not tasks:
                return {**response, "success": False, "error": "No function decorated with @task found"}
            response.update({"tasks": tasks, "task": tasks[0]})
        return response
    except Exception as exc:
        return {"error": str(exc), "traceback": traceback.format_exc()}


def main():
    if len(sys.argv) < 2:
        result = {"error": "Missing action parameter"}
    else:
        action = sys.argv[1]
        params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        actions = {
            "get_workspace_tasks": lambda: get_workspace_tasks(params.get("workspaceDir", "")),
            "save_workspace_task": lambda: save_workspace_task(
                params.get("workspaceDir", ""),
                params.get("relativePath", ""),
                params.get("code", ""),
                params.get("parse", True),
            ),
            "parse_custom_function": lambda: parse_custom_function(params.get("code", "")),
        }
        try:
            result = actions[action]() if action in actions else {"error": f"Unknown action: {action}"}
        except Exception as exc:
            result = {"error": str(exc), "traceback": traceback.format_exc()}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
