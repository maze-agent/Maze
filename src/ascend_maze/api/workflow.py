"""Local static Workflow authoring API."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ascend_maze.api.task import TaskTemplate, get_task_template
from ascend_maze.compiler.ir import CompiledWorkflow, ModelAnchorSpec
from ascend_maze.core.errors import (
    WorkflowFrozenError,
    WorkflowValidationError,
)
from ascend_maze.core.identifiers import stable_id

if TYPE_CHECKING:
    from ascend_maze.compiler.compiler import CompileOptions


@dataclass(frozen=True, slots=True)
class WorkflowInputRef:
    workflow_id: str
    name: str


@dataclass(frozen=True, slots=True)
class OutputRef:
    workflow_id: str
    task_id: str
    output_name: str


class TaskOutputs(Mapping[str, OutputRef]):
    __slots__ = ("_items",)

    def __init__(self, refs: tuple[tuple[str, OutputRef], ...]) -> None:
        self._items = refs

    def __getitem__(self, key: str) -> OutputRef:
        for name, ref in self._items:
            if name == key:
                return ref
        raise KeyError(f"task has no output named {key!r}")

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("TaskOutputs is immutable")
        object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class TaskHandle:
    workflow_id: str
    task_id: str
    task_name: str
    outputs: TaskOutputs


@dataclass(slots=True)
class _DraftTask:
    template: TaskTemplate
    task_id: str
    task_name: str
    inputs: dict[str, object]
    model_anchor: ModelAnchorSpec | None
    handle: TaskHandle


def _contains_reference(value: object, active: set[int] | None = None) -> bool:
    if isinstance(value, (WorkflowInputRef, OutputRef)):
        return True
    seen = active if active is not None else set()
    if isinstance(value, (tuple, list, set, frozenset)):
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        try:
            return any(_contains_reference(item, seen) for item in value)
        finally:
            seen.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        try:
            return any(
                _contains_reference(key, seen) or _contains_reference(item, seen)
                for key, item in value.items()
            )
        finally:
            seen.remove(identity)
    return False


def _model_anchor(value: Mapping[str, object] | None) -> ModelAnchorSpec | None:
    if value is None:
        return None
    unknown = sorted(set(value) - {"model", "mode"})
    if unknown:
        raise WorkflowValidationError(
            f"unknown model_anchor fields: {', '.join(unknown)}"
        )
    model = value.get("model")
    mode = value.get("mode")
    if not isinstance(model, str) or not model.strip():
        raise WorkflowValidationError("model_anchor.model must be a non-empty string")
    if mode not in {"service", "local_worker"}:
        raise WorkflowValidationError(
            "model_anchor.mode must be service or local_worker"
        )
    return ModelAnchorSpec(model=model.strip(), mode=mode)


class Workflow:
    """A mutable local DAG draft that freezes after successful compilation."""

    def __init__(self, name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise WorkflowValidationError("workflow name must be a non-empty string")
        self.name = name.strip()
        self.workflow_id = stable_id("workflow", self.name)
        self._workflow_inputs: dict[str, WorkflowInputRef] = {}
        self._draft_tasks: list[_DraftTask] = []
        self._tasks_by_id: dict[str, _DraftTask] = {}
        self._task_names: set[str] = set()
        self._control_edges: set[tuple[str, str]] = set()
        self._default_name_counts: dict[str, int] = {}
        self._compiled: CompiledWorkflow | None = None
        self._compile_options: CompileOptions | None = None
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise WorkflowFrozenError("workflow is frozen after compilation")

    def input(self, name: str) -> WorkflowInputRef:
        self._ensure_mutable()
        if not isinstance(name, str) or not name.strip():
            raise WorkflowValidationError("workflow input name must be non-empty")
        normalized = name.strip()
        if normalized in self._workflow_inputs:
            raise WorkflowValidationError(
                f"workflow input {normalized!r} is already declared"
            )
        ref = WorkflowInputRef(self.workflow_id, normalized)
        self._workflow_inputs[normalized] = ref
        return ref

    def _default_task_name(self, base: str) -> str:
        count = self._default_name_counts.get(base, 0) + 1
        while True:
            candidate = base if count == 1 else f"{base}_{count}"
            if candidate not in self._task_names:
                self._default_name_counts[base] = count
                return candidate
            count += 1

    def add_task(
        self,
        task_func: object,
        *,
        inputs: Mapping[str, Any] | None = None,
        task_name: str | None = None,
        model_anchor: Mapping[str, object] | None = None,
    ) -> TaskHandle:
        self._ensure_mutable()
        template = get_task_template(task_func)
        if task_name is None:
            resolved_name = self._default_task_name(template.func.__name__)
        else:
            if not isinstance(task_name, str) or not task_name.strip():
                raise WorkflowValidationError("task_name must be a non-empty string")
            resolved_name = task_name.strip()
            if resolved_name in self._task_names:
                raise WorkflowValidationError(
                    f"task_name {resolved_name!r} is already used"
                )
        task_id = stable_id("task", self.workflow_id, resolved_name)
        if task_id in self._tasks_by_id:
            raise WorkflowValidationError(f"duplicate task_id {task_id}")

        copied_inputs = dict(inputs or {})
        for input_name, value in copied_inputs.items():
            if not isinstance(input_name, str) or not input_name:
                raise WorkflowValidationError("task input names must be non-empty strings")
            if not isinstance(value, (WorkflowInputRef, OutputRef)) and _contains_reference(
                value
            ):
                raise WorkflowValidationError(
                    "phase one supports task/workflow references only as top-level inputs"
                )

        outputs = TaskOutputs(
            tuple(
                (
                    output_name,
                    OutputRef(self.workflow_id, task_id, output_name),
                )
                for output_name in template.analysis.output_names
            )
        )
        handle = TaskHandle(
            workflow_id=self.workflow_id,
            task_id=task_id,
            task_name=resolved_name,
            outputs=outputs,
        )
        draft = _DraftTask(
            template=template,
            task_id=task_id,
            task_name=resolved_name,
            inputs=copied_inputs,
            model_anchor=_model_anchor(model_anchor),
            handle=handle,
        )
        self._draft_tasks.append(draft)
        self._tasks_by_id[task_id] = draft
        self._task_names.add(resolved_name)
        return handle

    def add_edge(self, source: TaskHandle, target: TaskHandle) -> None:
        self._ensure_mutable()
        for handle in (source, target):
            if handle.workflow_id != self.workflow_id or handle.task_id not in self._tasks_by_id:
                raise WorkflowValidationError("edge task does not belong to this workflow")
        if source.task_id == target.task_id:
            raise WorkflowValidationError("self edges are not allowed")
        self._control_edges.add((source.task_id, target.task_id))

    def compile(self, options: CompileOptions | None = None) -> CompiledWorkflow:
        from ascend_maze.compiler.compiler import CompileOptions, compile_workflow

        effective = options or CompileOptions()
        if self._compiled is not None:
            if self._compile_options != effective:
                raise WorkflowFrozenError(
                    "workflow was already compiled with different options"
                )
            return self._compiled
        compiled = compile_workflow(self, effective)
        self._compile_options = effective
        self._compiled = compiled
        self._frozen = True
        return compiled

    async def run_async(
        self,
        *,
        inputs: Mapping[str, object],
        submission_id: str | None = None,
        config_path: str | None = None,
        socket_path: str | None = None,
    ) -> str:
        """Compile locally and submit through the Head-local C13 RuntimeClient."""

        import os
        from pathlib import Path

        from ascend_maze.compiler.compiler import CompileOptions
        from ascend_maze.config import load_config
        from ascend_maze.control.local_rpc import UdsRuntimeClient

        loaded = load_config(config_path)
        self.compile(
            CompileOptions(
                max_literal_value_bytes=loaded.config.workflow.max_literal_value_bytes,
                max_compiled_literal_bytes=loaded.config.workflow.max_compiled_literal_bytes,
            )
        )
        selected_socket = (
            socket_path
            or os.environ.get("ASCEND_MAZE_CONTROL_SOCKET")
            or loaded.config.control.socket_path
        )
        client = UdsRuntimeClient(
            Path(selected_socket).expanduser().resolve(strict=False),
            max_inline_control_bytes=loaded.config.control.max_inline_control_bytes,
            shared_filesystem_roots=loaded.config.data.shared_filesystem_roots,
        )
        client.config_fingerprint = loaded.snapshot.config_fingerprint
        await client.get_controller_status()
        await client.verify_compatibility()
        return await client.run(
            self,
            inputs=dict(inputs),
            submission_id=submission_id,
        )

    def run(
        self,
        *,
        inputs: Mapping[str, object],
        submission_id: str | None = None,
        config_path: str | None = None,
        socket_path: str | None = None,
    ) -> str:
        """Synchronous convenience wrapper for :meth:`run_async`."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "Workflow.run() cannot block an active event loop; use run_async()"
            )
        return asyncio.run(
            self.run_async(
                inputs=inputs,
                submission_id=submission_id,
                config_path=config_path,
                socket_path=socket_path,
            )
        )
