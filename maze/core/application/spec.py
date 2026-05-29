from __future__ import annotations

import base64
import json
import re
import shlex
from pathlib import Path
from typing import Any, Dict

from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


DEFAULT_RESOURCES = {
    "cpu": 1,
    "cpu_mem": 128,
    "gpu": 0,
    "gpu_mem": 0,
}


class AppSpecError(ValueError):
    """Raised when a Maze application spec is invalid."""


def _load_yaml(text: str) -> Any:
    try:
        import yaml
    except Exception as exc:
        raise AppSpecError("YAML app specs require PyYAML to be installed") from exc

    return yaml.safe_load(text)


def load_app_spec_file(path: str | Path) -> Dict[str, Any]:
    spec_path = Path(path).expanduser().resolve()
    text = spec_path.read_text(encoding="utf-8")
    if spec_path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        payload = _load_yaml(text)
    return app_spec_from_payload(payload, source_path=str(spec_path))


def _ensure_mapping(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise AppSpecError(f"{label} must be an object")
    return value


def _safe_task_name(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", str(name or "").strip()).strip("_").lower()
    if not normalized:
        normalized = "app"
    if normalized[0].isdigit():
        normalized = f"app_{normalized}"
    return normalized[:80]


def _normalize_resources(resources: Dict[str, Any] | None) -> Dict[str, Any]:
    resources = dict(resources or {})
    if "memory" in resources and "cpu_mem" not in resources:
        resources["cpu_mem"] = resources["memory"]
    if "mem" in resources and "cpu_mem" not in resources:
        resources["cpu_mem"] = resources["mem"]

    normalized = dict(DEFAULT_RESOURCES)
    for key in DEFAULT_RESOURCES:
        if key not in resources or resources[key] is None:
            continue
        value = resources[key]
        if key in {"cpu", "gpu"}:
            normalized[key] = int(value)
        else:
            normalized[key] = float(value)

    if normalized["cpu"] < 0 or normalized["gpu"] < 0:
        raise AppSpecError("resources.cpu and resources.gpu must be non-negative")
    if normalized["cpu_mem"] < 0 or normalized["gpu_mem"] < 0:
        raise AppSpecError("resources.cpu_mem and resources.gpu_mem must be non-negative")
    return normalized


def _normalize_env(env: Any) -> Dict[str, Any]:
    if env is None:
        return {"vars": {}, "conda": None}
    env = _ensure_mapping(env, "env")
    env_vars = dict(env.get("vars") or {})
    for key, value in env.items():
        if key in {"vars", "conda", "conda_sh"}:
            continue
        env_vars[key] = value
    return {
        "vars": {str(key): str(value) for key, value in env_vars.items()},
        "conda": env.get("conda"),
        "conda_sh": env.get("conda_sh"),
    }


def _normalize_retries(payload: Dict[str, Any]) -> Dict[str, Any]:
    retry_payload = dict(payload.get("retries") or {})
    max_retries = payload.get("max_retries", retry_payload.get("max"))
    retry_backoff = payload.get("retry_backoff_seconds", retry_payload.get("backoff_seconds", 0))
    retry_on = payload.get("retry_on", retry_payload.get("on"))
    if retry_on is not None and not isinstance(retry_on, list):
        raise AppSpecError("retry_on/retries.on must be a list")
    return {
        "max_retries": None if max_retries is None else max(0, int(max_retries)),
        "retry_backoff_seconds": max(0.0, float(retry_backoff or 0)),
        "retry_on": [str(item) for item in retry_on] if retry_on is not None else None,
    }


def _normalize_timeout(payload: Dict[str, Any]) -> float | None:
    run_payload = payload.get("run") or {}
    timeout = payload.get("timeout_seconds", payload.get("timeout", run_payload.get("timeout_seconds")))
    return None if timeout is None else float(timeout)


def _normalize_command(command: Any) -> str | list[str]:
    if isinstance(command, str):
        command = command.strip()
        if command:
            return command
    elif isinstance(command, list) and command and all(str(item).strip() for item in command):
        return [str(item) for item in command]
    raise AppSpecError("command must be a non-empty string or string list")


def _resolve_workspace(payload: Dict[str, Any], source_path: str | None) -> str | None:
    workspace = payload.get("workspace") or payload.get("workspace_dir")
    if workspace is None and source_path:
        workspace = str(Path(source_path).resolve().parent)
    if workspace is None:
        return None
    workspace_path = Path(str(workspace)).expanduser()
    if not workspace_path.is_absolute() and source_path:
        workspace_path = Path(source_path).resolve().parent / workspace_path
    return str(workspace_path.resolve())


def app_spec_from_payload(
    payload: Dict[str, Any],
    *,
    source_path: str | None = None,
    overrides: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    payload = dict(_ensure_mapping(payload, "app spec"))
    if overrides:
        payload.update({key: value for key, value in overrides.items() if value is not None})

    name = str(payload.get("name") or "maze-app").strip()
    if not name:
        raise AppSpecError("name must not be empty")
    command = _normalize_command(payload.get("command"))
    env = _normalize_env(payload.get("env"))
    resources = _normalize_resources(payload.get("resources"))
    retries = _normalize_retries(payload)
    timeout_seconds = _normalize_timeout(payload)

    artifacts = payload.get("artifacts") or []
    if isinstance(artifacts, str):
        artifacts = [artifacts]
    if not isinstance(artifacts, list):
        raise AppSpecError("artifacts must be a list")

    return {
        "schema": payload.get("schema") or "maze.app/v1",
        "name": name,
        "description": payload.get("description"),
        "command": command,
        "workspace": _resolve_workspace(payload, source_path),
        "workdir": payload.get("workdir") or ".",
        "resources": resources,
        "env": env,
        "artifacts": [str(item) for item in artifacts],
        "timeout_seconds": timeout_seconds,
        **retries,
        "metadata": dict(payload.get("metadata") or {}),
        "tags": [str(item) for item in payload.get("tags") or []],
        "source_path": source_path,
        "task_name": _safe_task_name(payload.get("task_name") or name),
    }


def _app_task_code(spec: Dict[str, Any]) -> str:
    task_config = {
        "name": spec["name"],
        "command": spec["command"],
        "workdir": spec.get("workdir") or ".",
        "env": spec.get("env") or {},
        "artifacts": spec.get("artifacts") or [],
        "timeout_seconds": spec.get("timeout_seconds"),
    }
    encoded = base64.b64encode(json.dumps(task_config, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return f'''
import base64
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

def run_{spec["task_name"]}():
    config = json.loads(base64.b64decode({encoded!r}).decode("utf-8"))
    started = time.time()
    env = os.environ.copy()
    env.update({{str(key): str(value) for key, value in (config.get("env", {{}}).get("vars") or {{}}).items()}})
    workdir = Path(str(config.get("workdir") or "."))
    if not workdir.exists():
        raise FileNotFoundError(f"Application workdir does not exist: {{workdir}}")

    raw_command = config["command"]
    conda_env = (config.get("env") or {{}}).get("conda")
    if isinstance(raw_command, list):
        command_for_log = " ".join(shlex.quote(str(item)) for item in raw_command)
        command = [str(item) for item in raw_command]
        shell = False
        executable = None
    else:
        command_for_log = str(raw_command)
        command = command_for_log
        shell = True
        executable = "/bin/bash"

    if conda_env:
        conda_sh = (config.get("env") or {{}}).get("conda_sh") or str(Path.home() / "miniconda3/etc/profile.d/conda.sh")
        command = (
            f"if [ -f {{shlex.quote(conda_sh)}} ]; then source {{shlex.quote(conda_sh)}}; fi; "
            f"conda activate {{shlex.quote(str(conda_env))}}; "
            f"{{command_for_log}}"
        )
        command_for_log = command
        shell = True
        executable = "/bin/bash"

    completed = subprocess.run(
        command,
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        shell=shell,
        executable=executable,
        timeout=config.get("timeout_seconds"),
    )
    finished = time.time()

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "maze-command.stdout").write_text(completed.stdout or "", encoding="utf-8")
    (log_dir / "maze-command.stderr").write_text(completed.stderr or "", encoding="utf-8")
    (log_dir / "maze-command.json").write_text(json.dumps({{
        "name": config.get("name"),
        "command": command_for_log,
        "returncode": completed.returncode,
        "started_time": started,
        "finished_time": finished,
        "duration_seconds": round(max(0.0, finished - started), 6),
        "artifacts": config.get("artifacts") or [],
    }}, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {{
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "command": command_for_log,
        "duration_seconds": round(max(0.0, finished - started), 6),
        "stdout_tail": (completed.stdout or "")[-4000:],
        "stderr_tail": (completed.stderr or "")[-4000:],
        "log_files": [
            "logs/maze-command.stdout",
            "logs/maze-command.stderr",
            "logs/maze-command.json",
        ],
        "declared_artifacts": config.get("artifacts") or [],
    }}
    if completed.returncode != 0:
        raise RuntimeError(
            "Application command failed with exit code "
            + str(completed.returncode)
            + "\\nstdout tail:\\n"
            + result["stdout_tail"]
            + "\\nstderr tail:\\n"
            + result["stderr_tail"]
        )
    return result
'''.strip()


def build_app_workflow(workflow_id: str, spec: Dict[str, Any]) -> Workflow:
    workflow = Workflow(workflow_id)
    task_id = "app"
    task = CodeTask(workflow_id, task_id, spec["task_name"])
    task.save_task(
        task_input={"input_params": {}},
        task_output={
            "output_params": {
                "1": {"key": "status", "data_type": "str"},
                "2": {"key": "returncode", "data_type": "int"},
                "3": {"key": "stdout_tail", "data_type": "str"},
                "4": {"key": "stderr_tail", "data_type": "str"},
                "5": {"key": "log_files", "data_type": "list"},
            }
        },
        code_str=_app_task_code(spec),
        code_ser=None,
        resources=spec["resources"],
        max_retries=spec.get("max_retries"),
        retry_backoff_seconds=spec.get("retry_backoff_seconds", 0),
        retry_on=spec.get("retry_on"),
        timeout_seconds=spec.get("timeout_seconds"),
    )
    workflow.add_task(task_id, task)
    return workflow


def app_file_context(
    spec: Dict[str, Any],
    *,
    artifact_base_url: str | None = None,
    artifact_mode: bool = True,
) -> Dict[str, Any] | None:
    workspace = spec.get("workspace")
    if not workspace:
        return None
    context: Dict[str, Any] = {
        "enabled": True,
        "workspace_dir": workspace,
    }
    if artifact_mode and artifact_base_url:
        context["artifact_store"] = {
            "type": "head_http",
            "base_url": artifact_base_url.rstrip("/"),
        }
    return context
