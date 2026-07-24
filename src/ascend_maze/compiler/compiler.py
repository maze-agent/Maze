"""Compile local Workflow drafts into deterministic immutable IR."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
from typing import TYPE_CHECKING

from ascend_maze.api.workflow import OutputRef, WorkflowInputRef
from ascend_maze.compiler.ir import (
    CompiledWorkflow,
    DefaultBinding,
    InputBinding,
    LiteralBinding,
    ModelAnchorSpec,
    OutputBinding,
    TaskDefinition,
    TaskNode,
    WorkflowInputBinding,
)
from ascend_maze.contracts.resources import ResourceSpec
from ascend_maze.core.canonical import (
    FrozenMap,
    canonical_bytes,
    canonical_digest,
    freeze_literal,
)
from ascend_maze.core.errors import (
    CanonicalizationError,
    LiteralSizeError,
    TaskDefinitionError,
    WorkflowValidationError,
)
from ascend_maze.core.identifiers import stable_id

if TYPE_CHECKING:
    from ascend_maze.api.workflow import Workflow, _DraftTask


@dataclass(frozen=True, slots=True)
class CompileOptions:
    schema_version: str = "1"
    max_literal_value_bytes: int = 64 * 1024
    max_compiled_literal_bytes: int = 1024 * 1024
    default_resources: ResourceSpec = ResourceSpec(
        cpu_num=1,
        mem_mb=0,
        npu_mem_mb=0,
        io_num=0,
    )

    def __post_init__(self) -> None:
        if not self.schema_version:
            raise ValueError("schema_version is required")
        for name in ("max_literal_value_bytes", "max_compiled_literal_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_literal_value_bytes > self.max_compiled_literal_bytes:
            raise ValueError(
                "max_literal_value_bytes cannot exceed max_compiled_literal_bytes"
            )


def _resolve_kind(
    declared: str | None,
    resources: ResourceSpec,
    static_kind: str | None,
) -> str:
    if declared is not None and static_kind == "npu" and declared != "npu":
        raise TaskDefinitionError(
            f"task_kind={declared} conflicts with high-confidence Ascend APIs"
        )
    if declared is not None:
        kind = declared
    elif resources.npu_mem_mb > 0:
        kind = "npu"
    elif static_kind is not None:
        kind = static_kind
    elif resources.io_num > 0:
        kind = "io"
    else:
        kind = "cpu"
    if kind in {"cpu", "io"} and resources.npu_mem_mb > 0:
        raise TaskDefinitionError(
            f"task_kind={kind} cannot declare positive npu_mem"
        )
    return kind


def _definition(draft: _DraftTask, options: CompileOptions) -> TaskDefinition:
    template = draft.template
    resources = template.resource_declaration.resolve(options.default_resources)
    task_kind = _resolve_kind(
        template.declared_task_kind,
        resources,
        template.analysis.static_task_kind,
    )
    static_inferred = ResourceSpec(
        cpu_num=template.analysis.static_cpu_num,
        mem_mb=0,
        npu_mem_mb=0,
        io_num=template.analysis.static_io_num,
    )
    signature = inspect.signature(template.func)
    default_names: list[str] = []
    default_digests: list[tuple[str, str]] = []
    for parameter in signature.parameters.values():
        if parameter.default is inspect.Signature.empty:
            continue
        try:
            digest = canonical_digest(parameter.default)
        except CanonicalizationError as exc:
            raise TaskDefinitionError(
                f"default value for {parameter.name!r} is not canonical"
            ) from exc
        default_names.append(parameter.name)
        default_digests.append((parameter.name, digest))

    identity = {
        "code_hash": template.analysis.code_hash,
        "input_names": template.analysis.input_names,
        "default_value_digests": default_digests,
        "output_names": template.analysis.output_names,
        "task_kind": task_kind,
        "resources": {
            "cpu_num": resources.cpu_num,
            "mem_mb": resources.mem_mb,
            "npu_mem_mb": resources.npu_mem_mb,
            "io_num": resources.io_num,
        },
        "static_inferred": {
            "cpu_num": static_inferred.cpu_num,
            "mem_mb": static_inferred.mem_mb,
            "npu_mem_mb": static_inferred.npu_mem_mb,
            "io_num": static_inferred.io_num,
        },
        "static_signals": template.analysis.static_signals,
        "timeout_ms": template.timeout_ms,
        "max_retries": template.max_retries,
        "retry_backoff_ms": template.retry_backoff_ms,
        "retry_on": template.retry_on,
    }
    definition_id = stable_id("definition", canonical_digest(identity))
    return TaskDefinition(
        definition_id=definition_id,
        callable_id=f"{template.analysis.module}:{template.analysis.qualname}",
        module=template.analysis.module,
        qualname=template.analysis.qualname,
        code_hash=template.analysis.code_hash,
        input_names=template.analysis.input_names,
        default_inputs=tuple(default_names),
        default_value_digests=tuple(default_digests),
        output_names=template.analysis.output_names,
        task_kind=task_kind,
        resources=resources,
        static_inferred=static_inferred,
        static_signals=template.analysis.static_signals,
        timeout_ms=template.timeout_ms,
        max_retries=template.max_retries,
        retry_backoff_ms=template.retry_backoff_ms,
        retry_on=template.retry_on,
    )


def _validate_model_resources(
    definition: TaskDefinition,
    model_anchor: ModelAnchorSpec | None,
) -> None:
    if model_anchor is not None and definition.task_kind != "npu":
        raise WorkflowValidationError("model_anchor requires task_kind='npu'")
    if model_anchor is not None and model_anchor.mode == "service":
        if definition.resources.npu_mem_mb > 0:
            raise WorkflowValidationError(
                "service tasks cannot explicitly reserve npu_mem"
            )
        return
    if definition.task_kind == "npu":
        if model_anchor is None and definition.resources.npu_mem_mb <= 0:
            raise WorkflowValidationError(
                "local NPU tasks without model_anchor require positive npu_mem"
            )
        if (
            model_anchor is not None
            and model_anchor.mode == "local_worker"
            and definition.resources.npu_mem_mb == 0
        ):
            return


def _binding(
    *,
    workflow: Workflow,
    input_name: str,
    value: object,
    options: CompileOptions,
) -> tuple[InputBinding, int, tuple[str, str] | None]:
    if isinstance(value, WorkflowInputRef):
        if (
            value.workflow_id != workflow.workflow_id
            or value.name not in workflow._workflow_inputs
        ):
            raise WorkflowValidationError(
                f"workflow input {value.name!r} does not belong to this workflow"
            )
        return WorkflowInputBinding(input_name, value.name), 0, None
    if isinstance(value, OutputRef):
        source = workflow._tasks_by_id.get(value.task_id)
        if value.workflow_id != workflow.workflow_id or source is None:
            raise WorkflowValidationError("output reference belongs to another workflow")
        if value.output_name not in source.template.analysis.output_names:
            raise WorkflowValidationError(
                f"task {source.task_name!r} has no output {value.output_name!r}"
            )
        return (
            OutputBinding(input_name, value.task_id, value.output_name),
            0,
            (value.task_id, "data"),
        )
    try:
        frozen = freeze_literal(value, max_bytes=options.max_literal_value_bytes)
    except LiteralSizeError:
        raise
    except CanonicalizationError as exc:
        raise WorkflowValidationError(
            f"literal input {input_name!r} is not deterministic; use workflow.input()"
        ) from exc
    size = len(canonical_bytes(frozen))
    return LiteralBinding(input_name, frozen), size, None


def _topological_order(
    task_ids: tuple[str, ...],
    predecessors: dict[str, set[str]],
    successors: dict[str, set[str]],
) -> tuple[str, ...]:
    remaining = {task_id: len(predecessors[task_id]) for task_id in task_ids}
    ready = sorted(task_id for task_id, count in remaining.items() if count == 0)
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for successor in sorted(successors[current]):
            remaining[successor] -= 1
            if remaining[successor] == 0:
                ready.append(successor)
                ready.sort()
    if len(result) != len(task_ids):
        raise WorkflowValidationError("workflow contains a cycle")
    return tuple(result)


def _depths(
    topological: tuple[str, ...],
    predecessors: dict[str, set[str]],
    successors: dict[str, set[str]],
) -> tuple[dict[str, int], dict[str, int]]:
    from_entry: dict[str, int] = {}
    for task_id in topological:
        from_entry[task_id] = (
            0
            if not predecessors[task_id]
            else max(from_entry[item] + 1 for item in predecessors[task_id])
        )
    to_exit: dict[str, int] = {}
    for task_id in reversed(topological):
        to_exit[task_id] = (
            0
            if not successors[task_id]
            else max(to_exit[item] + 1 for item in successors[task_id])
        )
    return from_entry, to_exit


def _resource_payload(resources: ResourceSpec) -> dict[str, int]:
    return {
        "cpu_num": resources.cpu_num,
        "mem_mb": resources.mem_mb,
        "npu_mem_mb": resources.npu_mem_mb,
        "io_num": resources.io_num,
    }


def _binding_payload(binding: InputBinding) -> dict[str, object]:
    if isinstance(binding, LiteralBinding):
        return {"kind": "literal", "name": binding.input_name, "value": binding.value}
    if isinstance(binding, OutputBinding):
        return {
            "kind": "output",
            "name": binding.input_name,
            "source_task_id": binding.source_task_id,
            "source_output": binding.source_output,
        }
    if isinstance(binding, WorkflowInputBinding):
        return {
            "kind": "workflow_input",
            "name": binding.input_name,
            "workflow_input_name": binding.workflow_input_name,
        }
    return {"kind": "default", "name": binding.input_name}


def _ir_payload(
    *,
    options: CompileOptions,
    workflow: Workflow,
    definitions: FrozenMap[str, TaskDefinition],
    tasks: FrozenMap[str, TaskNode],
    predecessors: FrozenMap[str, tuple[str, ...]],
    successors: FrozenMap[str, tuple[str, ...]],
    topological_order: tuple[str, ...],
    entry_tasks: tuple[str, ...],
    exit_tasks: tuple[str, ...],
    depth_from_entry: FrozenMap[str, int],
    depth_to_exit: FrozenMap[str, int],
) -> dict[str, object]:
    return {
        "schema_version": options.schema_version,
        "workflow_id": workflow.workflow_id,
        "workflow_name": workflow.name,
        "workflow_inputs": tuple(sorted(workflow._workflow_inputs)),
        "definitions": [
            {
                "definition_id": definition.definition_id,
                "callable_id": definition.callable_id,
                "module": definition.module,
                "qualname": definition.qualname,
                "code_hash": definition.code_hash,
                "input_names": definition.input_names,
                "default_inputs": definition.default_inputs,
                "default_value_digests": definition.default_value_digests,
                "output_names": definition.output_names,
                "task_kind": definition.task_kind,
                "resources": _resource_payload(definition.resources),
                "static_inferred": _resource_payload(definition.static_inferred),
                "static_signals": definition.static_signals,
                "timeout_ms": definition.timeout_ms,
                "max_retries": definition.max_retries,
                "retry_backoff_ms": definition.retry_backoff_ms,
                "retry_on": definition.retry_on,
            }
            for _, definition in definitions.items_tuple()
        ],
        "tasks": [
            {
                "task_id": task.task_id,
                "task_name": task.task_name,
                "definition_id": task.definition_id,
                "inputs": [_binding_payload(item) for item in task.inputs],
                "model_anchor": (
                    None
                    if task.model_anchor is None
                    else {
                        "model": task.model_anchor.model,
                        "mode": task.model_anchor.mode,
                    }
                ),
            }
            for _, task in tasks.items_tuple()
        ],
        "predecessors": predecessors.items_tuple(),
        "successors": successors.items_tuple(),
        "topological_order": topological_order,
        "entry_tasks": entry_tasks,
        "exit_tasks": exit_tasks,
        "depth_from_entry": depth_from_entry.items_tuple(),
        "depth_to_exit": depth_to_exit.items_tuple(),
    }


def compile_workflow(
    workflow: Workflow,
    options: CompileOptions | None = None,
) -> CompiledWorkflow:
    effective = options or CompileOptions()
    if not workflow._draft_tasks:
        raise WorkflowValidationError("workflow must contain at least one task")

    definitions_by_id: dict[str, TaskDefinition] = {}
    nodes_by_id: dict[str, TaskNode] = {}
    task_ids = tuple(sorted(workflow._tasks_by_id))
    predecessors: dict[str, set[str]] = {
        task_id: set() for task_id in task_ids
    }
    successors: dict[str, set[str]] = {task_id: set() for task_id in task_ids}
    literal_total = 0

    for draft in workflow._draft_tasks:
        definition = _definition(draft, effective)
        existing = definitions_by_id.get(definition.definition_id)
        if existing is not None and existing != definition:
            raise WorkflowValidationError(
                f"conflicting definition identity {definition.definition_id}"
            )
        definitions_by_id[definition.definition_id] = definition
        _validate_model_resources(definition, draft.model_anchor)

        provided = set(draft.inputs)
        expected = set(definition.input_names)
        unknown = sorted(provided - expected)
        if unknown:
            raise WorkflowValidationError(
                f"task {draft.task_name!r} has unknown inputs: {', '.join(unknown)}"
            )

        bindings: list[InputBinding] = []
        for input_name in definition.input_names:
            if input_name in draft.inputs:
                binding, size, edge = _binding(
                    workflow=workflow,
                    input_name=input_name,
                    value=draft.inputs[input_name],
                    options=effective,
                )
                literal_total += size
                if literal_total > effective.max_compiled_literal_bytes:
                    raise LiteralSizeError(
                        "compiled literal bytes exceed "
                        f"max_compiled_literal_bytes={effective.max_compiled_literal_bytes}"
                    )
                if edge is not None:
                    source_id, _ = edge
                    predecessors[draft.task_id].add(source_id)
                    successors[source_id].add(draft.task_id)
                bindings.append(binding)
            elif input_name in definition.default_inputs:
                bindings.append(DefaultBinding(input_name))
            else:
                raise WorkflowValidationError(
                    f"task {draft.task_name!r} is missing required input {input_name!r}"
                )

        nodes_by_id[draft.task_id] = TaskNode(
            task_id=draft.task_id,
            task_name=draft.task_name,
            definition_id=definition.definition_id,
            inputs=tuple(bindings),
            model_anchor=draft.model_anchor,
        )

    for source, target in workflow._control_edges:
        predecessors[target].add(source)
        successors[source].add(target)

    topological = _topological_order(task_ids, predecessors, successors)
    from_entry, to_exit = _depths(topological, predecessors, successors)

    frozen_definitions = FrozenMap(
        (key, definitions_by_id[key]) for key in sorted(definitions_by_id)
    )
    frozen_tasks = FrozenMap((key, nodes_by_id[key]) for key in sorted(nodes_by_id))
    frozen_predecessors = FrozenMap(
        (key, tuple(sorted(predecessors[key]))) for key in task_ids
    )
    frozen_successors = FrozenMap(
        (key, tuple(sorted(successors[key]))) for key in task_ids
    )
    frozen_from_entry = FrozenMap((key, from_entry[key]) for key in task_ids)
    frozen_to_exit = FrozenMap((key, to_exit[key]) for key in task_ids)
    entry_tasks = tuple(sorted(key for key in task_ids if not predecessors[key]))
    exit_tasks = tuple(sorted(key for key in task_ids if not successors[key]))

    payload = _ir_payload(
        options=effective,
        workflow=workflow,
        definitions=frozen_definitions,
        tasks=frozen_tasks,
        predecessors=frozen_predecessors,
        successors=frozen_successors,
        topological_order=topological,
        entry_tasks=entry_tasks,
        exit_tasks=exit_tasks,
        depth_from_entry=frozen_from_entry,
        depth_to_exit=frozen_to_exit,
    )
    ir_bytes = canonical_bytes(payload)
    fingerprint = hashlib.sha256(ir_bytes).hexdigest()
    return CompiledWorkflow(
        schema_version=effective.schema_version,
        workflow_id=workflow.workflow_id,
        workflow_name=workflow.name,
        workflow_fingerprint=fingerprint,
        canonical_ir_bytes=ir_bytes,
        workflow_inputs=tuple(sorted(workflow._workflow_inputs)),
        definitions=frozen_definitions,
        tasks=frozen_tasks,
        predecessors=frozen_predecessors,
        successors=frozen_successors,
        topological_order=topological,
        entry_tasks=entry_tasks,
        exit_tasks=exit_tasks,
        depth_from_entry=frozen_from_entry,
        depth_to_exit=frozen_to_exit,
    )
