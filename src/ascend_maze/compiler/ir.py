"""Immutable internal representation of a compiled static workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from ascend_maze.contracts.resources import ResourceSpec
from ascend_maze.core.canonical import CanonicalValue, FrozenMap


@dataclass(frozen=True, slots=True)
class LiteralBinding:
    input_name: str
    value: CanonicalValue


@dataclass(frozen=True, slots=True)
class OutputBinding:
    input_name: str
    source_task_id: str
    source_output: str


@dataclass(frozen=True, slots=True)
class DefaultBinding:
    input_name: str


@dataclass(frozen=True, slots=True)
class WorkflowInputBinding:
    input_name: str
    workflow_input_name: str


InputBinding: TypeAlias = (
    LiteralBinding | OutputBinding | DefaultBinding | WorkflowInputBinding
)


@dataclass(frozen=True, slots=True)
class ModelAnchorSpec:
    model: str
    mode: str


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    definition_id: str
    callable_id: str
    module: str
    qualname: str
    code_hash: str
    input_names: tuple[str, ...]
    default_inputs: tuple[str, ...]
    default_value_digests: tuple[tuple[str, str], ...]
    output_names: tuple[str, ...]
    task_kind: str
    resources: ResourceSpec
    static_inferred: ResourceSpec
    static_signals: tuple[str, ...]
    timeout_ms: int | None
    max_retries: int
    retry_backoff_ms: int
    retry_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskNode:
    task_id: str
    task_name: str
    definition_id: str
    inputs: tuple[InputBinding, ...]
    model_anchor: ModelAnchorSpec | None


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    schema_version: str
    workflow_id: str
    workflow_name: str
    workflow_fingerprint: str
    canonical_ir_bytes: bytes
    workflow_inputs: tuple[str, ...]
    definitions: FrozenMap[str, TaskDefinition]
    tasks: FrozenMap[str, TaskNode]
    predecessors: FrozenMap[str, tuple[str, ...]]
    successors: FrozenMap[str, tuple[str, ...]]
    topological_order: tuple[str, ...]
    entry_tasks: tuple[str, ...]
    exit_tasks: tuple[str, ...]
    depth_from_entry: FrozenMap[str, int]
    depth_to_exit: FrozenMap[str, int]
