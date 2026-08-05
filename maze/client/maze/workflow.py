from __future__ import annotations

import copy
import uuid
from typing import Any, Callable, Dict, List, Optional

from maze.client.maze.decorator import get_task_metadata
from maze.client.maze.models import MaTask, TaskOutput
from maze.client.maze.workflow_authoring import encode_run_inputs


def _encode_output_refs(value: Any) -> Any:
    if isinstance(value, TaskOutput):
        return {
            "__maze_output_ref__": True,
            "task_id": value.task_id,
            "output_key": value.output_key,
        }
    if isinstance(value, dict):
        return {key: _encode_output_refs(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_output_refs(item) for item in value]
    return value


class MaWorkflow:
    """A local static DAG draft submitted atomically when ``run`` is called."""

    def __init__(self, workflow_id: str, client: Any):
        self.workflow_id = workflow_id
        self._client = client
        self.server_url = client.server_url
        self.request_timeout = client.request_timeout
        self._tasks: Dict[str, MaTask] = {}
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._edges: List[Dict[str, str]] = []

    def add_task(
        self,
        task_func: Callable,
        inputs: Optional[Dict[str, Any]] = None,
        task_name: Optional[str] = None,
    ) -> MaTask:
        if task_func is None:
            raise TypeError("add_task expects a function decorated with @task")
        metadata = get_task_metadata(task_func)
        task_id = f"task_{uuid.uuid4().hex}"
        task_name = task_name or metadata.func_name
        task_inputs: Dict[str, Dict[str, Any]] = {}

        provided_inputs = dict(inputs or {})
        unknown_inputs = sorted(set(provided_inputs) - set(metadata.inputs))
        if unknown_inputs:
            raise ValueError("Unknown task inputs: " + ", ".join(unknown_inputs))

        for input_name in metadata.inputs:
            has_value = input_name in provided_inputs
            input_value = provided_inputs.get(input_name)
            data_type = metadata.data_types.get(input_name, "any")
            if isinstance(input_value, TaskOutput):
                if input_value.task_id not in self._tasks:
                    raise ValueError(
                        f"Task input {input_name!r} references a task outside this workflow"
                    )
                task_inputs[input_name] = {
                    "input_schema": "from_task",
                    "value": input_value.to_reference_string(),
                    "data_type": data_type,
                    "has_value": True,
                }
                self._edges.append({
                    "source_task_id": input_value.task_id,
                    "source_output": input_value.output_key,
                    "target_task_id": task_id,
                    "target_input": input_name,
                })
                continue

            encoded_value, has_run_input = encode_run_inputs(input_value)
            task_inputs[input_name] = {
                "input_schema": "from_run" if has_run_input else "from_user",
                "value": encoded_value,
                "data_type": data_type,
                "has_value": False if has_run_input else has_value,
            }

        node = {
            "id": task_id,
            "type": "code",
            "task_name": task_name,
            "inputs": task_inputs,
            "outputs": [
                {
                    "name": output_name,
                    "data_type": metadata.data_types.get(output_name, "any"),
                }
                for output_name in metadata.outputs
            ],
            "task_kind": metadata.task_kind,
            "resources": copy.deepcopy(metadata.resources),
            "code_str": metadata.code_str,
            "code_ser": metadata.code_ser,
            "max_retries": metadata.max_retries,
            "retry_backoff_seconds": metadata.retry_backoff_seconds,
            "retry_on": copy.deepcopy(metadata.retry_on),
            "timeout_seconds": metadata.timeout_seconds,
        }
        self._nodes[task_id] = node
        task = MaTask(task_id, self.workflow_id, task_name, metadata.outputs)
        self._tasks[task_id] = task
        return task

    def get_tasks(self) -> List[Dict[str, str]]:
        return [
            {"id": task.task_id, "name": task.task_name or task.task_id}
            for task in self._tasks.values()
        ]

    def _build_spec(
        self,
        *,
        file_context: Optional[Dict[str, Any]],
        workspace_dir: Optional[str],
        artifact_mode: bool,
        timeout_seconds: Optional[float],
        tags: Optional[List[str]],
        metadata: Optional[Dict[str, Any]],
        inputs: Optional[Dict[str, Any]],
        run_id: Optional[str],
    ) -> Dict[str, Any]:
        if not self._nodes:
            raise ValueError("Workflow has no tasks")

        run: Dict[str, Any] = {"artifact_mode": artifact_mode}
        prepared_file_context = self._client._build_file_context(
            file_context=file_context,
            workspace_dir=workspace_dir,
            artifact_mode=artifact_mode,
        )
        if prepared_file_context is not None:
            run["file_context"] = prepared_file_context
        if timeout_seconds is not None:
            run["timeout_seconds"] = timeout_seconds
        if tags is not None:
            run["tags"] = list(tags)
        if metadata is not None:
            run["metadata"] = dict(metadata)
        if inputs is not None:
            run["inputs"] = copy.deepcopy(inputs)
        if run_id is not None:
            run["run_id"] = run_id

        spec: Dict[str, Any] = {
            "schema": "maze.workflow/v1",
            "workflow_id": self.workflow_id,
            "name": getattr(getattr(self, "workflow_definition", None), "name", "python-workflow"),
            "nodes": [copy.deepcopy(node) for node in self._nodes.values()],
            "edges": copy.deepcopy(self._edges),
            "run": run,
        }
        input_contract = getattr(self, "_workflow_input_contract", None)
        if input_contract is not None:
            spec["input_contract"] = {
                "constants": sorted(input_contract["constants"]),
                "runtime": copy.deepcopy(input_contract["runtime"]),
            }
        if hasattr(self, "final_output_refs"):
            spec["final_output_refs"] = _encode_output_refs(self.final_output_refs)
        return spec

    def run(
        self,
        file_context: Optional[Dict[str, Any]] = None,
        workspace_dir: Optional[str] = None,
        artifact_mode: bool = False,
        timeout_seconds: Optional[float] = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        inputs: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> str:
        spec = self._build_spec(
            file_context=file_context,
            workspace_dir=workspace_dir,
            artifact_mode=artifact_mode,
            timeout_seconds=timeout_seconds,
            tags=tags,
            metadata=metadata,
            inputs=inputs,
            run_id=run_id,
        )
        result = self._client.submit_workflow(spec, artifact_mode=artifact_mode)
        self.workflow_id = result["workflow_id"]
        return result["run_id"]

    def __repr__(self) -> str:
        return f"MaWorkflow(id='{self.workflow_id[:8]}...', tasks={len(self._tasks)})"
