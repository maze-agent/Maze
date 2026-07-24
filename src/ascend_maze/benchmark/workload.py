"""Versioned workload records and trace inputs for benchmark execution."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
from pathlib import Path
from typing import Mapping, cast

from ascend_maze.benchmark.canonical import canonical_json_digest, thaw
from ascend_maze.benchmark.contracts import FileArtifact, WorkloadSpec
from ascend_maze.benchmark.planning import file_sha256
from ascend_maze.contracts.data import SharedFileRef
from ascend_maze.core.canonical import CanonicalValue, FrozenMap, freeze_canonical
from ascend_maze.core.errors import ExperimentValidationError

WORKLOAD_DATASET_SCHEMA = "ascend-maze.workload-dataset.v1"
TRACE_SCHEDULE_SCHEMA = "ascend-maze.trace-schedule.v1"


@dataclass(frozen=True, slots=True)
class WorkloadRecord:
    record_id: str
    inputs: FrozenMap[CanonicalValue, CanonicalValue]
    input_digest: str

    def materialize_inputs(self) -> dict[str, object]:
        plain = thaw(self.inputs)
        if not isinstance(plain, dict) or any(
            not isinstance(key, str) for key in plain
        ):
            raise ExperimentValidationError(
                f"workload record {self.record_id}: inputs are not a string map"
            )
        return {key: _decode_input(value) for key, value in plain.items()}


@dataclass(frozen=True, slots=True)
class WorkloadDataset:
    schema_version: int
    workflow_fingerprint: str
    records: tuple[WorkloadRecord, ...]
    source_digest: str


@dataclass(frozen=True, slots=True)
class TraceSchedule:
    schema_version: int
    offsets_ms: tuple[int, ...]
    source_digest: str


def load_workload_dataset(spec: WorkloadSpec) -> WorkloadDataset:
    artifact = _artifact(spec, "dataset")
    document = _load_json_artifact(artifact, "workload dataset")
    allowed = {"schema_version", "workflow_fingerprint", "records"}
    _reject_unknown(document, allowed, "workload dataset")
    if document.get("schema_version") != 1:
        raise ExperimentValidationError("workload dataset schema_version must be 1")
    if document.get("workflow_fingerprint") != spec.workflow_fingerprint:
        raise ExperimentValidationError(
            "workload dataset workflow_fingerprint does not match ExperimentSpec"
        )
    raw_records = document.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise ExperimentValidationError("workload dataset records must be non-empty")
    records: list[WorkloadRecord] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            raise ExperimentValidationError(
                f"workload dataset records[{index}] must be an object"
            )
        _reject_unknown(raw, {"record_id", "inputs"}, f"records[{index}]")
        record_id = raw.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise ExperimentValidationError(
                f"workload dataset records[{index}].record_id is required"
            )
        if record_id in seen:
            raise ExperimentValidationError(
                f"workload dataset duplicate record_id: {record_id}"
            )
        seen.add(record_id)
        raw_inputs = raw.get("inputs")
        if not isinstance(raw_inputs, dict) or any(
            not isinstance(key, str) or not key for key in raw_inputs
        ):
            raise ExperimentValidationError(
                f"workload dataset records[{index}].inputs must be a string map"
            )
        frozen = freeze_canonical(raw_inputs)
        if not isinstance(frozen, FrozenMap):
            raise ExperimentValidationError("workload record inputs must be a mapping")
        records.append(
            WorkloadRecord(
                record_id=record_id,
                inputs=frozen,
                input_digest=canonical_json_digest(raw_inputs),
            )
        )
    return WorkloadDataset(
        schema_version=1,
        workflow_fingerprint=spec.workflow_fingerprint,
        records=tuple(records),
        source_digest=artifact.content_sha256,
    )


def load_trace_schedule(spec: WorkloadSpec, logical_name: str) -> TraceSchedule:
    artifact = _artifact(spec, logical_name)
    document = _load_json_artifact(artifact, "trace schedule")
    _reject_unknown(document, {"schema_version", "offsets_ms"}, "trace schedule")
    if document.get("schema_version") != 1:
        raise ExperimentValidationError("trace schedule schema_version must be 1")
    raw_offsets = document.get("offsets_ms")
    if not isinstance(raw_offsets, list) or not raw_offsets:
        raise ExperimentValidationError("trace schedule offsets_ms must be non-empty")
    offsets: list[int] = []
    for index, value in enumerate(raw_offsets):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ExperimentValidationError(
                f"trace schedule offsets_ms[{index}] must be non-negative"
            )
        if offsets and value < offsets[-1]:
            raise ExperimentValidationError(
                "trace schedule offsets_ms must be non-decreasing"
            )
        offsets.append(value)
    return TraceSchedule(1, tuple(offsets), artifact.content_sha256)


def load_workflow(spec: WorkloadSpec, *, config: Mapping[str, object]) -> object:
    """Load and fingerprint-check the public Workflow factory for execution."""

    from ascend_maze.api.workflow import Workflow
    from ascend_maze.compiler.compiler import CompileOptions
    from ascend_maze.compiler.ir import CompiledWorkflow

    module_name, attribute = spec.workflow_factory.split(":", 1)
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
        workflow = factory()
    except (ImportError, AttributeError, TypeError) as exc:
        raise ExperimentValidationError(
            f"cannot load workload.workflow_factory {spec.workflow_factory}: {exc}"
        ) from exc
    if isinstance(workflow, Workflow):
        workflow_config = config.get("workflow")
        if not isinstance(workflow_config, Mapping):
            raise ExperimentValidationError("resolved config has no workflow table")
        literal_limit = workflow_config.get("max_literal_value_bytes")
        total_limit = workflow_config.get("max_compiled_literal_bytes")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (literal_limit, total_limit)
        ):
            raise ExperimentValidationError("resolved workflow limits are invalid")
        compiled = workflow.compile(
            CompileOptions(
                max_literal_value_bytes=cast(int, literal_limit),
                max_compiled_literal_bytes=cast(int, total_limit),
            )
        )
    elif isinstance(workflow, CompiledWorkflow):
        compiled = workflow
    else:
        raise ExperimentValidationError(
            "workload.workflow_factory must return Workflow or CompiledWorkflow"
        )
    if compiled.workflow_fingerprint != spec.workflow_fingerprint:
        raise ExperimentValidationError(
            "workload.workflow_fingerprint does not match the loaded Workflow"
        )
    return workflow


def _artifact(spec: WorkloadSpec, logical_name: str) -> FileArtifact:
    for artifact in spec.inputs:
        if artifact.logical_name == logical_name:
            return artifact
    raise ExperimentValidationError(
        f"workload input is required for execution: {logical_name}"
    )


def _load_json_artifact(
    artifact: FileArtifact, description: str
) -> Mapping[str, object]:
    path = Path(artifact.source_path)
    try:
        if path.stat().st_size != artifact.size_bytes:
            raise ExperimentValidationError(f"{description} size changed")
        if file_sha256(path) != artifact.content_sha256:
            raise ExperimentValidationError(f"{description} digest changed")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ExperimentValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentValidationError(f"cannot read {description}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExperimentValidationError(f"{description} must contain a JSON object")
    return cast(Mapping[str, object], payload)


def _reject_unknown(
    document: Mapping[str, object], allowed: set[str], prefix: str
) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise ExperimentValidationError(f"{prefix}.{unknown[0]} is unknown")


def _decode_input(value: object) -> object:
    if not isinstance(value, dict) or "$shared_file" not in value:
        return value
    if set(value) != {"$shared_file"} or not isinstance(value["$shared_file"], dict):
        raise ExperimentValidationError("invalid SharedFileRef workload input")
    payload = value["$shared_file"]
    if set(payload) != {"canonical_path", "content_sha256", "size_bytes"}:
        raise ExperimentValidationError("invalid SharedFileRef workload fields")
    return SharedFileRef(
        canonical_path=payload["canonical_path"],
        content_sha256=payload["content_sha256"],
        size_bytes=payload["size_bytes"],
    )
