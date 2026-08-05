from __future__ import annotations

from typing import Optional


class TaskOutput:
    """A reference to one output of a task in the same local workflow draft."""

    def __init__(self, task_id: str, output_key: str):
        self.task_id = task_id
        self.output_key = output_key

    def to_reference_string(self) -> str:
        return f"{self.task_id}.output.{self.output_key}"

    def __repr__(self) -> str:
        return f"TaskOutput({self.task_id[:8]}...:{self.output_key})"


class TaskOutputs:
    def __init__(self, task_id: str, output_keys: list[str]):
        self.task_id = task_id
        self._outputs = {key: TaskOutput(task_id, key) for key in output_keys}

    def __getitem__(self, key: str) -> TaskOutput:
        if key not in self._outputs:
            raise KeyError(f"Task does not have output parameter named {key!r}")
        return self._outputs[key]

    def keys(self):
        return self._outputs.keys()

    def __repr__(self) -> str:
        return f"TaskOutputs({list(self._outputs)})"


class MaTask:
    """A task node in a local ``MaWorkflow`` draft."""

    def __init__(
        self,
        task_id: str,
        workflow_id: str,
        task_name: Optional[str] = None,
        output_keys: Optional[list[str]] = None,
    ):
        self.task_id = task_id
        self.workflow_id = workflow_id
        self.task_name = task_name
        self.outputs = TaskOutputs(task_id, output_keys) if output_keys else None

    def __repr__(self) -> str:
        name = f", name='{self.task_name}'" if self.task_name else ""
        return f"MaTask(id='{self.task_id[:8]}...'{name})"
