from __future__ import annotations

import contextvars
import inspect
from functools import wraps
from typing import Any, Callable, Dict

from maze.client.maze.decorator import get_task_metadata
from maze.client.maze.models import MaTask, TaskOutput


OutputRef = TaskOutput
RUN_INPUT_REF_MARKER = "__maze_run_input__"

_GRAPH_CONTEXT: contextvars.ContextVar["GraphBuildContext | None"] = contextvars.ContextVar(
    "maze_graph_build_context",
    default=None,
)


def active_graph_context() -> "GraphBuildContext | None":
    return _GRAPH_CONTEXT.get()


class RunInput:
    __slots__ = ("key",)

    def __init__(self, key: str):
        self.key = key

    def _unsupported(self, *_args, **_kwargs):
        raise TypeError(
            f"Workflow runtime input {self.key!r} must be passed unchanged to a task; "
            "bind it in create_workflow_from() if it controls DAG construction"
        )

    __bool__ = _unsupported
    __str__ = _unsupported
    __repr__ = _unsupported
    __format__ = _unsupported
    __eq__ = _unsupported


def encode_run_inputs(value: Any) -> tuple[Any, bool]:
    if isinstance(value, RunInput):
        return {
            RUN_INPUT_REF_MARKER: True,
            "key": value.key,
        }, True
    if isinstance(value, tuple):
        encoded = [encode_run_inputs(item) for item in value]
        return [item for item, _ in encoded], any(found for _, found in encoded)
    if isinstance(value, list):
        encoded = [encode_run_inputs(item) for item in value]
        return [item for item, _ in encoded], any(found for _, found in encoded)
    if isinstance(value, dict):
        encoded = {key: encode_run_inputs(item) for key, item in value.items()}
        return (
            {key: item for key, (item, _) in encoded.items()},
            any(found for _, found in encoded.values()),
        )
    return value, False


class TaskInvocation:
    """Concrete task call created while building a static workflow graph."""

    def __init__(self, task: MaTask, output_keys: list[str]):
        self.task = task
        self.task_id = task.task_id
        self.outputs = task.outputs
        self.output_keys = list(output_keys)

    def __getitem__(self, output_key: str) -> OutputRef:
        return self._output(output_key)

    def __getattr__(self, output_key: str) -> OutputRef:
        if output_key.startswith("_"):
            raise AttributeError(output_key)
        try:
            return self._output(output_key)
        except KeyError as exc:
            raise AttributeError(str(exc)) from exc

    def _output(self, output_key: str) -> OutputRef:
        if self.outputs is None:
            raise KeyError(f"Task {self.task_id} has no declared outputs")
        return self.outputs[output_key]

    def as_single_output(self) -> OutputRef:
        if len(self.output_keys) != 1:
            raise ValueError(
                "Task invocation has multiple outputs. Select one with "
                "task.output_name or task['output_name']."
            )
        return self._output(self.output_keys[0])

    def __repr__(self) -> str:
        return f"TaskInvocation(id='{self.task_id[:8]}...', outputs={self.output_keys})"


class WorkflowDefinition:
    def __init__(self, func: Callable):
        self.func = func
        self.name = func.__name__
        wraps(func)(self)

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    def build(self, ma_workflow, inputs: Dict[str, Any] | None = None):
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise TypeError("workflow inputs must be a dictionary")

        signature = inspect.signature(self.func)
        bound = signature.bind_partial(**inputs)
        runtime_inputs = {}
        for name, parameter in signature.parameters.items():
            if parameter.kind in {
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }:
                continue
            if name in bound.arguments:
                continue
            runtime_inputs[name] = {
                "required": parameter.default is inspect.Parameter.empty,
            }
            if parameter.default is not inspect.Parameter.empty:
                runtime_inputs[name]["default"] = parameter.default
            bound.arguments[name] = RunInput(name)

        ma_workflow._workflow_input_contract = {
            "constants": frozenset(inputs),
            "runtime": runtime_inputs,
        }

        context = GraphBuildContext(ma_workflow)
        token = _GRAPH_CONTEXT.set(context)
        try:
            result = self.func(*bound.args, **bound.kwargs)
            ma_workflow.final_output_refs = context.normalize_value(result)
            ma_workflow.workflow_definition = self
            return ma_workflow.final_output_refs
        finally:
            _GRAPH_CONTEXT.reset(token)


class GraphBuildContext:
    def __init__(self, ma_workflow):
        self.ma_workflow = ma_workflow
        self.invocations: list[TaskInvocation] = []

    def call_task(self, task_func: Callable, args: tuple[Any, ...], kwargs: Dict[str, Any]) -> TaskInvocation:
        metadata = get_task_metadata(task_func)
        signature = inspect.signature(metadata.func)
        bound = signature.bind_partial(*args, **kwargs)

        task_inputs = {
            key: self.normalize_value(value)
            for key, value in bound.arguments.items()
        }
        task = self.ma_workflow.add_task(task_func, inputs=task_inputs)
        invocation = TaskInvocation(task, metadata.outputs)
        self.invocations.append(invocation)
        return invocation

    def normalize_value(self, value: Any) -> Any:
        if isinstance(value, TaskInvocation):
            return value.as_single_output()
        if isinstance(value, tuple):
            return tuple(self.normalize_value(item) for item in value)
        if isinstance(value, list):
            return [self.normalize_value(item) for item in value]
        if isinstance(value, dict):
            return {
                key: self.normalize_value(item)
                for key, item in value.items()
            }
        return value


def workflow(func: Callable | None = None):
    """Decorate a Python function as a static Maze workflow template."""

    def decorator(inner: Callable) -> WorkflowDefinition:
        return WorkflowDefinition(inner)

    if func is not None:
        if not callable(func):
            raise TypeError("@workflow expects a callable")
        return decorator(func)

    return decorator
