from __future__ import annotations

import base64
import functools
import uuid
from typing import Any, Callable, Dict

import cloudpickle

from maze.client.maze.client import MaClient


RUN_INPUT_REF_MARKER = "__maze_run_input__"


def _dumps(value: Any) -> str:
    return base64.b64encode(cloudpickle.dumps(value)).decode("ascii")


def _loads(value: str) -> Any:
    return cloudpickle.loads(base64.b64decode(value))


def _execute_langgraph_callable(task_input_data: Dict[str, Any] | None = None) -> dict:
    payload = dict(task_input_data or {})
    func = _loads(payload["callable"])
    args = _loads(payload["args"])
    kwargs = _loads(payload["kwargs"])
    return {"result": _dumps(func(*args, **kwargs))}


class LanggraphClient:
    """Run LangGraph node functions as ordinary one-node Maze workflows."""

    def __init__(self, addr: str = "localhost:8000") -> None:
        server_url = addr.rstrip("/")
        if not server_url.startswith(("http://", "https://")):
            server_url = f"http://{server_url}"
        self.maze_server_addr = addr
        self.workflow_id = str(uuid.uuid4())
        self.default_resources = {"cpu_num": 1, "gpu_mem": 0, "io_num": 0}
        self._client = MaClient(server_url)

    def _normalize_resources(self, resources: Dict[str, Any] | None) -> Dict[str, int]:
        raw = dict(resources or {})
        normalized = {
            "cpu_num": int(raw.get("cpu_num", raw.get("cpu", 1)) or 1),
            "gpu_mem": int(raw.get("gpu_mem", 0) or 0),
            "io_num": int(raw.get("io_num", 0) or 0),
        }
        normalized["cpu_num"] = max(1, normalized["cpu_num"])
        normalized["gpu_mem"] = max(0, normalized["gpu_mem"])
        normalized["io_num"] = max(0, normalized["io_num"])
        return normalized

    def _normalize_task_kind(
        self,
        task_kind: str | None,
        resources: Dict[str, int],
    ) -> str:
        normalized = (
            task_kind
            or ("gpu" if resources.get("gpu_mem", 0) > 0 else "cpu")
        ).strip().lower()
        if normalized not in {"cpu", "gpu", "io"}:
            raise ValueError("task_kind must be one of: cpu, gpu, io")
        if normalized == "gpu" and resources.get("gpu_mem", 0) <= 0:
            raise ValueError("gpu LangGraph tasks must declare resources.gpu_mem")
        return normalized

    def task(
        self,
        func_or_resources=None,
        *,
        resources=None,
        task_kind: str | None = None,
    ):
        if callable(func_or_resources):
            normalized_resources = self.default_resources.copy()
            normalized_task_kind = self._normalize_task_kind(
                task_kind,
                normalized_resources,
            )
            return self._decorate(
                func_or_resources,
                normalized_resources,
                normalized_task_kind,
            )

        if resources is None:
            resources = func_or_resources or self.default_resources
        allowed = {"cpu_num", "gpu_mem", "io_num", "cpu"}
        for key, value in resources.items():
            if key not in allowed:
                raise ValueError(f"Invalid resource type: {key}")
            if not isinstance(value, (int, float)):
                raise ValueError(
                    f"Resource values must be numbers, but got {type(value)}"
                )
        normalized_resources = self._normalize_resources(resources)
        normalized_task_kind = self._normalize_task_kind(
            task_kind,
            normalized_resources,
        )
        return lambda func: self._decorate(
            func,
            normalized_resources,
            normalized_task_kind,
        )

    def _decorate(
        self,
        func: Callable,
        resources: Dict[str, int],
        task_kind: str,
    ):
        task_id = str(uuid.uuid4())
        workflow_id = f"{self.workflow_id}-{task_id}"
        template = {
            "schema": "maze.workflow/v1",
            "workflow_id": workflow_id,
            "name": f"langgraph-{func.__name__}",
            "nodes": [{
                "id": task_id,
                "type": "code",
                "task_name": func.__name__,
                "inputs": {
                    "callable": {
                        "input_schema": "from_user",
                        "value": _dumps(func),
                        "data_type": "str",
                        "has_value": True,
                    },
                    "args": {
                        "input_schema": "from_run",
                        "value": {RUN_INPUT_REF_MARKER: True, "key": "args"},
                        "data_type": "str",
                    },
                    "kwargs": {
                        "input_schema": "from_run",
                        "value": {RUN_INPUT_REF_MARKER: True, "key": "kwargs"},
                        "data_type": "str",
                    },
                },
                "outputs": [{"name": "result", "data_type": "str"}],
                "resources": dict(resources),
                "task_kind": task_kind,
                "code_ser": _dumps(_execute_langgraph_callable),
            }],
            "edges": [],
            "input_contract": {
                "constants": ["callable"],
                "runtime": {
                    "args": {"required": True},
                    "kwargs": {"required": True},
                },
            },
            "final_output_refs": {
                "result": {
                    "__maze_output_ref__": True,
                    "task_id": task_id,
                    "output_key": "result",
                },
            },
        }

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            spec = {
                **template,
                "run": {
                    "artifact_mode": False,
                    "tags": ["langgraph"],
                    "metadata": {"adapter": "langgraph"},
                    "inputs": {
                        "args": _dumps(args),
                        "kwargs": _dumps(kwargs),
                    },
                },
            }
            try:
                submission = self._client.submit_workflow(
                    spec,
                    artifact_mode=False,
                )
                run = self._client.wait_run(submission["run_id"])
                if run.get("status") != "succeeded":
                    raise RuntimeError(
                        f"Run {submission['run_id']} ended with {run.get('status')}: "
                        f"{run.get('error_summary') or 'unknown error'}"
                    )
                encoded_result = (run.get("result_summary") or {}).get("result")
                if not isinstance(encoded_result, str):
                    raise RuntimeError("succeeded run is missing result_summary.result")
                return _loads(encoded_result)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to execute remote task {func.__name__}: {exc}"
                ) from exc

        wrapper._task_id = task_id
        wrapper._workflow_id = workflow_id
        wrapper._is_maze_task = True
        return wrapper
