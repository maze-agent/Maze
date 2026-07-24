"""Immutable C14 experiment and planning contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from pathlib import Path
import re

from ascend_maze.benchmark.canonical import (
    canonical_json_bytes,
    canonical_json_digest,
    derive_seed,
    stable_payload_id,
    thaw,
)
from ascend_maze.contracts.config import ConfigSnapshot
from ascend_maze.core.canonical import CanonicalValue, freeze_canonical
from ascend_maze.core.errors import ExperimentValidationError

SCHEMA_VERSION = 1
EXPERIMENT_SPEC_SCHEMA = "ascend-maze.experiment-spec.v1"
STUDY_PLAN_SCHEMA = "ascend-maze.study-plan.v1"
TRIAL_MANIFEST_SCHEMA = "ascend-maze.trial-manifest.v1"
ANALYSIS_POLICY_VERSION = "c14_v1"
INTERNAL_ABLATION_MATRIX = "internal_ablation_v1"
CUSTOM_MATRIX = "custom_v1"
BENCHMARK_OVERRIDE_PATHS = frozenset(
    {
        "benchmark.c12_bookkeeping",
        "benchmark.c13_read_clients",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_CONFIG_PATH_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_FACTORY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$")

OVERRIDABLE_CONFIG_PATHS = frozenset(
    {
        "fault.max_retries_default",
        "fault.retry_backoff_ms",
        "inference.reconcile_interval_ms",
        "placement.allow_colocation",
        "placement.anchor_strategy",
        "placement.host_mem_headroom_mb",
        "placement.io_slots_total",
        "placement.npu_hbm_headroom_mb",
        "placement.npu_system_reserved_hbm_mb",
        "placement.task_slots_total",
        "recording.backend",
        "recording.batch_size",
        "recording.compression",
        "recording.control_queue_capacity",
        "recording.flush_interval_ms",
        "recording.flush_timeout_ms",
        "recording.max_page_size",
        "recording.telemetry_queue_capacity",
        "scheduler.dispatch_timeout_ms",
        "scheduler.max_bypass_count",
        "scheduler.partitioner",
        "scheduler.placement_lookahead",
        "scheduler.policy",
        "worker.binding_deadline_ms",
        "worker.hbm_recovery_deadline_ms",
        "worker.hbm_recovery_tolerance_mb",
        "worker.max_tasks_per_worker",
        "worker.max_total",
        "worker.standby_max_idle",
        "worker.standby_min_idle",
    }
) | BENCHMARK_OVERRIDE_PATHS


def _required_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ExperimentValidationError(f"{name}: must be a non-empty string")
    return value


def _name(name: str, value: object) -> str:
    result = _required_string(name, value)
    if not _NAME_RE.fullmatch(result):
        raise ExperimentValidationError(f"{name}: must match [a-z][a-z0-9_.-]*")
    return result


def _digest(name: str, value: object) -> str:
    result = _required_string(name, value)
    if not _SHA256_RE.fullmatch(result):
        raise ExperimentValidationError(f"{name}: must be a lowercase SHA-256 digest")
    return result


def _non_negative(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExperimentValidationError(f"{name}: must be a non-negative integer")
    return value


def _positive(name: str, value: object) -> int:
    result = _non_negative(name, value)
    if result == 0:
        raise ExperimentValidationError(f"{name}: must be a positive integer")
    return result


def _identity(name: str, value: object, prefix: str) -> str:
    result = _required_string(name, value)
    if not re.fullmatch(rf"{re.escape(prefix)}_[0-9a-f]{{32}}", result):
        raise ExperimentValidationError(f"{name}: invalid {prefix} identity")
    return result


@dataclass(frozen=True, slots=True)
class FileArtifact:
    logical_name: str
    source_path: str
    content_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "logical_name", _name("input.logical_name", self.logical_name)
        )
        path = Path(_required_string("input.path", self.source_path))
        if not path.is_absolute():
            raise ExperimentValidationError(
                "input.path: must be resolved to an absolute path"
            )
        object.__setattr__(self, "source_path", str(path))
        _digest("input.sha256", self.content_sha256)
        _non_negative("input.size_bytes", self.size_bytes)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "logical_name": self.logical_name,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    name: str
    workflow_factory: str
    workflow_fingerprint: str
    model_catalog_revision: str
    model_artifact_digest: str
    required_environment_fingerprint: str
    inputs: tuple[FileArtifact, ...]
    workload_digest: str = field(init=False)
    input_manifest_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name("workload.name", self.name))
        if not isinstance(self.workflow_factory, str) or not _FACTORY_RE.fullmatch(
            self.workflow_factory
        ):
            raise ExperimentValidationError(
                "workload.workflow_factory: must be a stable MODULE:CALLABLE identity"
            )
        _digest("workload.workflow_fingerprint", self.workflow_fingerprint)
        _required_string("workload.model_catalog_revision", self.model_catalog_revision)
        _digest("workload.model_artifact_digest", self.model_artifact_digest)
        _digest(
            "workload.required_environment_fingerprint",
            self.required_environment_fingerprint,
        )
        ordered = tuple(sorted(self.inputs, key=lambda item: item.logical_name))
        if len({item.logical_name for item in ordered}) != len(ordered):
            raise ExperimentValidationError(
                "workload.inputs: logical names must be unique"
            )
        if not ordered:
            raise ExperimentValidationError(
                "workload.inputs: at least one input is required"
            )
        object.__setattr__(self, "inputs", ordered)
        input_payload = [item.canonical_payload() for item in ordered]
        object.__setattr__(
            self, "input_manifest_digest", canonical_json_digest(input_payload)
        )
        object.__setattr__(
            self,
            "workload_digest",
            canonical_json_digest(self._identity_payload(input_payload)),
        )

    def _identity_payload(self, inputs: object) -> dict[str, object]:
        return {
            "name": self.name,
            "workflow_factory": self.workflow_factory,
            "workflow_fingerprint": self.workflow_fingerprint,
            "model_catalog_revision": self.model_catalog_revision,
            "model_artifact_digest": self.model_artifact_digest,
            "required_environment_fingerprint": self.required_environment_fingerprint,
            "inputs": inputs,
        }

    def canonical_payload(self) -> dict[str, object]:
        payload = self._identity_payload(
            [item.canonical_payload() for item in self.inputs]
        )
        payload["workload_digest"] = self.workload_digest
        payload["input_manifest_digest"] = self.input_manifest_digest
        return payload


@dataclass(frozen=True, slots=True)
class ArrivalSpec:
    mode: str
    concurrency: int | None = None
    rate_per_second: float | None = None
    trace_input: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"closed_loop", "fixed_rate", "poisson", "trace_replay"}:
            raise ExperimentValidationError("arrival.mode: unsupported value")
        if self.mode == "closed_loop":
            _positive("arrival.concurrency", self.concurrency)
            if self.rate_per_second is not None or self.trace_input is not None:
                raise ExperimentValidationError(
                    "arrival: closed_loop only accepts concurrency"
                )
            return
        if self.mode in {"fixed_rate", "poisson"}:
            rate = self.rate_per_second
            if (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not math.isfinite(float(rate))
                or rate <= 0
            ):
                raise ExperimentValidationError(
                    "arrival.rate_per_second: must be finite and positive"
                )
            object.__setattr__(self, "rate_per_second", float(rate))
            if self.concurrency is not None or self.trace_input is not None:
                raise ExperimentValidationError(
                    f"arrival: {self.mode} only accepts rate_per_second"
                )
            return
        if self.concurrency is not None or self.rate_per_second is not None:
            raise ExperimentValidationError(
                "arrival: trace_replay only accepts trace_input"
            )
        object.__setattr__(
            self, "trace_input", _name("arrival.trace_input", self.trace_input)
        )

    def canonical_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"mode": self.mode}
        if self.concurrency is not None:
            payload["concurrency"] = self.concurrency
        if self.rate_per_second is not None:
            payload["rate_per_second"] = self.rate_per_second
        if self.trace_input is not None:
            payload["trace_input"] = self.trace_input
        return payload


@dataclass(frozen=True, slots=True)
class MeasurementWindows:
    warmup_runs: int
    warmup_duration_ms: int
    measurement_run_count: int
    measurement_duration_ms: int
    drain_deadline_ms: int

    def __post_init__(self) -> None:
        for name in (
            "warmup_runs",
            "warmup_duration_ms",
            "measurement_run_count",
            "measurement_duration_ms",
        ):
            _non_negative(f"windows.{name}", getattr(self, name))
        _positive("windows.drain_deadline_ms", self.drain_deadline_ms)
        if self.warmup_runs > 0 and self.warmup_duration_ms > 0:
            raise ExperimentValidationError(
                "windows: warmup_runs and warmup_duration_ms cannot both be positive"
            )
        if (self.measurement_run_count > 0) == (self.measurement_duration_ms > 0):
            raise ExperimentValidationError(
                "windows: exactly one measurement limit must be positive"
            )

    def validate_arrival(self, arrival: ArrivalSpec) -> None:
        if arrival.mode == "closed_loop" and self.measurement_run_count == 0:
            raise ExperimentValidationError(
                "windows.measurement_run_count: required for closed_loop"
            )
        if arrival.mode == "closed_loop" and self.warmup_duration_ms > 0:
            raise ExperimentValidationError(
                "windows.warmup_duration_ms: closed_loop requires count-based warmup"
            )
        if arrival.mode != "closed_loop" and self.measurement_duration_ms == 0:
            raise ExperimentValidationError(
                "windows.measurement_duration_ms: required for open arrivals"
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "warmup_runs": self.warmup_runs,
            "warmup_duration_ms": self.warmup_duration_ms,
            "measurement_run_count": self.measurement_run_count,
            "measurement_duration_ms": self.measurement_duration_ms,
            "drain_deadline_ms": self.drain_deadline_ms,
        }


@dataclass(frozen=True, slots=True)
class AnalysisSpec:
    metric_set: tuple[str, ...]
    validity_policy: str = ANALYSIS_POLICY_VERSION
    statistics_policy: str = ANALYSIS_POLICY_VERSION
    performance_budget_set: str = ANALYSIS_POLICY_VERSION
    quantile_method: str = "hyndman_fan_type_7"
    bootstrap_samples: int = 10_000
    confidence_level: float = 0.95
    familywise_confidence_level: float = 0.9875
    automatic_outlier_removal: bool = False

    def __post_init__(self) -> None:
        metrics = tuple(sorted(self.metric_set))
        if not metrics or len(metrics) != len(set(metrics)):
            raise ExperimentValidationError(
                "analysis.metric_set: values must be non-empty and unique"
            )
        for metric in metrics:
            _name("analysis.metric_set", metric)
        object.__setattr__(self, "metric_set", metrics)
        for name in (
            "validity_policy",
            "statistics_policy",
            "performance_budget_set",
        ):
            if getattr(self, name) != ANALYSIS_POLICY_VERSION:
                raise ExperimentValidationError(f"analysis.{name}: unsupported value")
        if self.quantile_method != "hyndman_fan_type_7":
            raise ExperimentValidationError(
                "analysis.quantile_method: unsupported value"
            )
        if self.bootstrap_samples != 10_000:
            raise ExperimentValidationError(
                "analysis.bootstrap_samples: version 1 requires 10000"
            )
        if self.confidence_level != 0.95:
            raise ExperimentValidationError(
                "analysis.confidence_level: version 1 requires 0.95"
            )
        if self.familywise_confidence_level != 0.9875:
            raise ExperimentValidationError(
                "analysis.familywise_confidence_level: version 1 requires 0.9875"
            )
        if self.automatic_outlier_removal is not False:
            raise ExperimentValidationError(
                "analysis.automatic_outlier_removal: must be false"
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "metric_set": self.metric_set,
            "validity_policy": self.validity_policy,
            "statistics_policy": self.statistics_policy,
            "performance_budget_set": self.performance_budget_set,
            "quantile_method": self.quantile_method,
            "bootstrap_samples": self.bootstrap_samples,
            "confidence_level": self.confidence_level,
            "familywise_confidence_level": self.familywise_confidence_level,
            "automatic_outlier_removal": self.automatic_outlier_removal,
        }


@dataclass(frozen=True, slots=True)
class ConfigOverride:
    path: str
    value: CanonicalValue

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not _CONFIG_PATH_RE.fullmatch(self.path):
            raise ExperimentValidationError("matrix override path is invalid")
        if self.path not in OVERRIDABLE_CONFIG_PATHS:
            raise ExperimentValidationError(
                f"matrix override path is not allowed: {self.path}"
            )
        object.__setattr__(self, "value", freeze_canonical(self.value))

    def canonical_payload(self) -> dict[str, object]:
        return {"path": self.path, "value": thaw(self.value)}


@dataclass(frozen=True, slots=True)
class ConfigDifference:
    path: str
    before: CanonicalValue
    after: CanonicalValue

    def __post_init__(self) -> None:
        if not _CONFIG_PATH_RE.fullmatch(self.path):
            raise ExperimentValidationError("config difference path is invalid")
        object.__setattr__(self, "before", freeze_canonical(self.before))
        object.__setattr__(self, "after", freeze_canonical(self.after))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "before": thaw(self.before),
            "after": thaw(self.after),
        }


@dataclass(frozen=True, slots=True)
class FactorSpec:
    name: str
    allowed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name("matrix.factor.name", self.name))
        paths = tuple(sorted(self.allowed_paths))
        if not paths or len(paths) != len(set(paths)):
            raise ExperimentValidationError(
                f"matrix factor {self.name}: allowed paths must be non-empty and unique"
            )
        for path in paths:
            if path not in OVERRIDABLE_CONFIG_PATHS:
                raise ExperimentValidationError(
                    f"matrix factor {self.name}: path is not allowed: {path}"
                )
        object.__setattr__(self, "allowed_paths", paths)

    def canonical_payload(self) -> dict[str, object]:
        return {"name": self.name, "allowed_paths": self.allowed_paths}


@dataclass(frozen=True, slots=True)
class CellDefinition:
    name: str
    factors: tuple[str, ...]
    overrides: tuple[ConfigOverride, ...]
    confirmatory: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name("matrix.cell.name", self.name))
        factors = tuple(sorted(self.factors))
        overrides = tuple(sorted(self.overrides, key=lambda item: item.path))
        if len(factors) != len(set(factors)):
            raise ExperimentValidationError(
                f"matrix cell {self.name}: factors must be unique"
            )
        if len({item.path for item in overrides}) != len(overrides):
            raise ExperimentValidationError(
                f"matrix cell {self.name}: duplicate override path"
            )
        if not isinstance(self.confirmatory, bool):
            raise ExperimentValidationError(
                f"matrix cell {self.name}: confirmatory must be a boolean"
            )
        object.__setattr__(self, "factors", factors)
        object.__setattr__(self, "overrides", overrides)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "factors": self.factors,
            "overrides": [item.canonical_payload() for item in self.overrides],
            "confirmatory": self.confirmatory,
        }


@dataclass(frozen=True, slots=True)
class MatrixSpec:
    kind: str
    baseline_cell: str
    factors: tuple[FactorSpec, ...]
    cells: tuple[CellDefinition, ...]

    def __post_init__(self) -> None:
        if self.kind not in {INTERNAL_ABLATION_MATRIX, CUSTOM_MATRIX}:
            raise ExperimentValidationError("matrix.kind: unsupported value")
        _name("matrix.baseline_cell", self.baseline_cell)
        factors = tuple(sorted(self.factors, key=lambda item: item.name))
        cells = tuple(sorted(self.cells, key=lambda item: item.name))
        factor_by_name = {item.name: item for item in factors}
        if len(factor_by_name) != len(factors):
            raise ExperimentValidationError("matrix.factors: names must be unique")
        path_owners: dict[str, str] = {}
        for factor in factors:
            for path in factor.allowed_paths:
                owner = path_owners.get(path)
                if owner is not None:
                    raise ExperimentValidationError(
                        f"matrix factors {owner} and {factor.name} overlap at {path}"
                    )
                path_owners[path] = factor.name
        if len({item.name for item in cells}) != len(cells):
            raise ExperimentValidationError("matrix.cells: names must be unique")
        if self.baseline_cell not in {item.name for item in cells}:
            raise ExperimentValidationError("matrix.baseline_cell: cell does not exist")
        for cell in cells:
            unknown = sorted(set(cell.factors) - set(factor_by_name))
            if unknown:
                raise ExperimentValidationError(
                    f"matrix cell {cell.name}: unknown factor {unknown[0]}"
                )
            if cell.name == self.baseline_cell:
                if cell.factors or cell.overrides:
                    raise ExperimentValidationError(
                        "matrix baseline cell cannot contain factors or overrides"
                    )
                continue
            if cell.confirmatory and len(cell.factors) != 1:
                raise ExperimentValidationError(
                    f"matrix cell {cell.name}: confirmatory cell must change one factor"
                )
            if not cell.factors:
                raise ExperimentValidationError(
                    f"matrix cell {cell.name}: at least one factor is required"
                )
            allowed = {
                path
                for factor in cell.factors
                for path in factor_by_name[factor].allowed_paths
            }
            changed = {item.path for item in cell.overrides}
            if not changed:
                raise ExperimentValidationError(
                    f"matrix cell {cell.name}: overrides are required"
                )
            unexpected = sorted(changed - allowed)
            if unexpected:
                raise ExperimentValidationError(
                    f"matrix cell {cell.name}: override is outside factor boundary: "
                    f"{unexpected[0]}"
                )
            unused = sorted(
                factor.name
                for factor in (factor_by_name[name] for name in cell.factors)
                if not changed.intersection(factor.allowed_paths)
            )
            if unused:
                raise ExperimentValidationError(
                    f"matrix cell {cell.name}: factor has no override: {unused[0]}"
                )
        object.__setattr__(self, "factors", factors)
        object.__setattr__(self, "cells", cells)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "baseline_cell": self.baseline_cell,
            "factors": [item.canonical_payload() for item in self.factors],
            "cells": [item.canonical_payload() for item in self.cells],
        }


@dataclass(frozen=True, slots=True)
class ExternalAdapterSpec:
    adapter_id: str
    argv: tuple[str, ...]
    executable_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_id", _name("baseline.id", self.adapter_id))
        argv = tuple(self.argv)
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ExperimentValidationError(
                "baseline.argv: must be a non-empty string array"
            )
        if any("\x00" in item for item in argv):
            raise ExperimentValidationError("baseline.argv: NUL bytes are forbidden")
        object.__setattr__(self, "argv", argv)
        _digest("baseline.executable_sha256", self.executable_sha256)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "argv": self.argv,
            "executable_sha256": self.executable_sha256,
        }


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    schema_version: int
    study_name: str
    study_kind: str
    base_seed: int
    block_count: int
    repetition_count: int
    build_revision: str
    base_config_path: str
    base_config_source_digest: str
    base_config_snapshot: ConfigSnapshot
    workload: WorkloadSpec
    arrival: ArrivalSpec
    windows: MeasurementWindows
    analysis: AnalysisSpec
    matrix: MatrixSpec
    baselines: tuple[ExternalAdapterSpec, ...] = ()
    study_id: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != SCHEMA_VERSION
        ):
            raise ExperimentValidationError(
                "schema_version: only version 1 is supported"
            )
        object.__setattr__(self, "study_name", _name("study_name", self.study_name))
        if self.study_kind not in {"pilot", "formal"}:
            raise ExperimentValidationError("study_kind: must be pilot or formal")
        if (
            isinstance(self.base_seed, bool)
            or not isinstance(self.base_seed, int)
            or not 0 <= self.base_seed < (1 << 63)
        ):
            raise ExperimentValidationError("base_seed: must be in [0, 2^63-1]")
        _positive("block_count", self.block_count)
        if self.study_kind == "pilot" and self.block_count < 3:
            raise ExperimentValidationError("block_count: pilot requires at least 3")
        if self.study_kind == "formal" and self.block_count < 10:
            raise ExperimentValidationError("block_count: formal requires at least 10")
        _positive("repetition_count", self.repetition_count)
        if self.repetition_count != 1:
            raise ExperimentValidationError(
                "repetition_count: schema version 1 requires exactly 1"
            )
        if not isinstance(self.build_revision, str) or not _GIT_REVISION_RE.fullmatch(
            self.build_revision
        ):
            raise ExperimentValidationError(
                "build_revision: must be a full lowercase Git commit"
            )
        base_path = Path(_required_string("base_config", self.base_config_path))
        if not base_path.is_absolute():
            raise ExperimentValidationError(
                "base_config: must resolve to an absolute path"
            )
        object.__setattr__(self, "base_config_path", str(base_path))
        _digest("base_config_sha256", self.base_config_source_digest)
        if self.base_config_snapshot.resolved["profile"] != "performance":
            raise ExperimentValidationError(
                "base_config: correctness profile cannot produce performance studies"
            )
        if (
            self.base_config_snapshot.model_catalog_revision
            != self.workload.model_catalog_revision
        ):
            raise ExperimentValidationError(
                "workload.model_catalog_revision: does not match base ConfigSnapshot"
            )
        cluster = self.base_config_snapshot.resolved.get("cluster")
        if not isinstance(cluster, Mapping):
            raise ExperimentValidationError("base ConfigSnapshot has no cluster table")
        if (
            cluster.get("environment_fingerprint")
            != self.workload.required_environment_fingerprint
        ):
            raise ExperimentValidationError(
                "workload.required_environment_fingerprint: does not match "
                "base ConfigSnapshot"
            )
        self.windows.validate_arrival(self.arrival)
        if self.arrival.trace_input is not None and self.arrival.trace_input not in {
            item.logical_name for item in self.workload.inputs
        }:
            raise ExperimentValidationError(
                "arrival.trace_input: no matching workload input"
            )
        baselines = tuple(sorted(self.baselines, key=lambda item: item.adapter_id))
        if len({item.adapter_id for item in baselines}) != len(baselines):
            raise ExperimentValidationError("baselines: adapter IDs must be unique")
        object.__setattr__(self, "baselines", baselines)
        object.__setattr__(
            self,
            "study_id",
            stable_payload_id("study", self.canonical_payload(), length=32),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "schema": EXPERIMENT_SPEC_SCHEMA,
            "study_name": self.study_name,
            "study_kind": self.study_kind,
            "base_seed": self.base_seed,
            "block_count": self.block_count,
            "repetition_count": self.repetition_count,
            "build_revision": self.build_revision,
            "base_config_fingerprint": self.base_config_snapshot.config_fingerprint,
            "workload": self.workload.canonical_payload(),
            "arrival": self.arrival.canonical_payload(),
            "windows": self.windows.canonical_payload(),
            "analysis": self.analysis.canonical_payload(),
            "matrix": self.matrix.canonical_payload(),
            "baselines": [item.canonical_payload() for item in self.baselines],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class CellSpec:
    name: str
    cell_id: str
    factors: tuple[str, ...]
    overrides: tuple[ConfigOverride, ...]
    differences: tuple[ConfigDifference, ...]
    config_snapshot: ConfigSnapshot
    confirmatory: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name("CellSpec.name", self.name))
        _identity("CellSpec.cell_id", self.cell_id, "cell")
        if not isinstance(self.confirmatory, bool):
            raise ExperimentValidationError("CellSpec.confirmatory must be a boolean")
        if tuple(sorted(self.factors)) != self.factors:
            raise ExperimentValidationError("CellSpec.factors must be sorted")
        if tuple(sorted(self.overrides, key=lambda item: item.path)) != self.overrides:
            raise ExperimentValidationError("CellSpec.overrides must be sorted")
        if (
            tuple(sorted(self.differences, key=lambda item: item.path))
            != self.differences
        ):
            raise ExperimentValidationError("CellSpec.differences must be sorted")
        if {item.path for item in self.overrides} != {
            item.path for item in self.differences
        }:
            raise ExperimentValidationError(
                "CellSpec differences must exactly match its overrides"
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "cell_id": self.cell_id,
            "factors": self.factors,
            "confirmatory": self.confirmatory,
            "overrides": [item.canonical_payload() for item in self.overrides],
            "config_differences": [
                item.canonical_payload() for item in self.differences
            ],
            "config_fingerprint": self.config_snapshot.config_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class TrialSpec:
    trial_id: str
    study_id: str
    cell_id: str
    cell_name: str
    block_index: int
    repetition_index: int
    position_in_block: int
    trial_seed: int
    pairing_seed: int

    def __post_init__(self) -> None:
        _identity("TrialSpec.trial_id", self.trial_id, "trial")
        _identity("TrialSpec.study_id", self.study_id, "study")
        _identity("TrialSpec.cell_id", self.cell_id, "cell")
        object.__setattr__(
            self, "cell_name", _name("TrialSpec.cell_name", self.cell_name)
        )
        for name in ("block_index", "repetition_index", "position_in_block"):
            _non_negative(name, getattr(self, name))
        for name in ("trial_seed", "pairing_seed"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value < (1 << 63)
            ):
                raise ExperimentValidationError(f"{name}: invalid derived seed")

    def seed(self, namespace: str) -> int:
        return derive_seed(self.trial_seed, _name("seed namespace", namespace))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "study_id": self.study_id,
            "cell_id": self.cell_id,
            "cell_name": self.cell_name,
            "block_index": self.block_index,
            "repetition_index": self.repetition_index,
            "position_in_block": self.position_in_block,
            "trial_seed": self.trial_seed,
            "pairing_seed": self.pairing_seed,
        }


@dataclass(frozen=True, slots=True)
class TrialManifest:
    schema_version: int
    trial_attempt_id: str
    trial_id: str
    attempt_index: int
    state: str
    run_ids: tuple[str, ...] = ()
    experiment_ids: tuple[str, ...] = ()
    committed_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != SCHEMA_VERSION
        ):
            raise ExperimentValidationError(
                "TrialManifest schema version is unsupported"
            )
        _identity(
            "TrialManifest.trial_attempt_id",
            self.trial_attempt_id,
            "trial_attempt",
        )
        _identity("TrialManifest.trial_id", self.trial_id, "trial")
        _non_negative("attempt_index", self.attempt_index)
        if self.state not in {
            "planned",
            "preparing",
            "warming",
            "measuring",
            "draining",
            "flushing",
            "valid",
            "invalid",
            "aborted",
        }:
            raise ExperimentValidationError("TrialManifest state is invalid")
        for name in ("run_ids", "experiment_ids", "committed_files"):
            values = tuple(getattr(self, name))
            if any(not isinstance(item, str) or not item for item in values):
                raise ExperimentValidationError(f"TrialManifest {name} is invalid")
            if len(values) != len(set(values)):
                raise ExperimentValidationError(
                    f"TrialManifest {name} contains duplicate values"
                )
            object.__setattr__(self, name, values)

    @classmethod
    def planned(cls, trial: TrialSpec, *, attempt_index: int = 0) -> "TrialManifest":
        _non_negative("attempt_index", attempt_index)
        return cls(
            schema_version=SCHEMA_VERSION,
            trial_attempt_id=stable_payload_id(
                "trial_attempt",
                {"trial_id": trial.trial_id, "attempt_index": attempt_index},
                length=32,
            ),
            trial_id=trial.trial_id,
            attempt_index=attempt_index,
            state="planned",
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "schema": TRIAL_MANIFEST_SCHEMA,
            "trial_attempt_id": self.trial_attempt_id,
            "trial_id": self.trial_id,
            "attempt_index": self.attempt_index,
            "state": self.state,
            "run_ids": self.run_ids,
            "experiment_ids": self.experiment_ids,
            "committed_files": self.committed_files,
        }


def measurement_id(
    trial_attempt_id: str, metric_name: str, metric_schema_version: int
) -> str:
    _required_string("trial_attempt_id", trial_attempt_id)
    _name("metric_name", metric_name)
    _positive("metric_schema_version", metric_schema_version)
    return stable_payload_id(
        "measurement",
        {
            "trial_attempt_id": trial_attempt_id,
            "metric_name": metric_name,
            "metric_schema_version": metric_schema_version,
        },
        length=32,
    )


@dataclass(frozen=True, slots=True)
class StudyPlan:
    schema_version: int
    spec: ExperimentSpec
    cells: tuple[CellSpec, ...]
    trials: tuple[TrialSpec, ...]
    schema_digests: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or self.schema_version != SCHEMA_VERSION
        ):
            raise ExperimentValidationError("StudyPlan schema version is unsupported")
        if not self.cells or not self.trials:
            raise ExperimentValidationError("StudyPlan cells and trials are required")
        if tuple(sorted(self.cells, key=lambda item: item.name)) != self.cells:
            raise ExperimentValidationError("StudyPlan cells must be sorted by name")
        if len({cell.cell_id for cell in self.cells}) != len(self.cells):
            raise ExperimentValidationError(
                "matrix contains duplicate logical Cell identities"
            )
        if len({trial.trial_id for trial in self.trials}) != len(self.trials):
            raise ExperimentValidationError("StudyPlan trial identities are not unique")
        cell_ids = {cell.cell_id for cell in self.cells}
        if any(
            trial.study_id != self.spec.study_id or trial.cell_id not in cell_ids
            for trial in self.trials
        ):
            raise ExperimentValidationError(
                "StudyPlan contains a Trial outside its Study or matrix"
            )
        expected_trial_count = (
            self.spec.block_count * self.spec.repetition_count * len(self.cells)
        )
        if len(self.trials) != expected_trial_count:
            raise ExperimentValidationError(
                "StudyPlan does not contain one Trial per Cell and block"
            )
        ordered_trials = tuple(
            sorted(
                self.trials,
                key=lambda item: (
                    item.block_index,
                    item.repetition_index,
                    item.position_in_block,
                ),
            )
        )
        if ordered_trials != self.trials:
            raise ExperimentValidationError(
                "StudyPlan trials are not in execution order"
            )
        for block_index in range(self.spec.block_count):
            for repetition_index in range(self.spec.repetition_count):
                block = tuple(
                    trial
                    for trial in self.trials
                    if trial.block_index == block_index
                    and trial.repetition_index == repetition_index
                )
                if (
                    {trial.cell_id for trial in block} != cell_ids
                    or {trial.position_in_block for trial in block}
                    != set(range(len(self.cells)))
                    or len({trial.pairing_seed for trial in block}) != 1
                ):
                    raise ExperimentValidationError(
                        "StudyPlan block is incomplete or is not paired"
                    )
        if len(self.schema_digests) != len({name for name, _ in self.schema_digests}):
            raise ExperimentValidationError("StudyPlan schema names must be unique")
        for name, digest in self.schema_digests:
            _required_string("StudyPlan schema name", name)
            _digest("StudyPlan schema digest", digest)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "schema": STUDY_PLAN_SCHEMA,
            "study_id": self.spec.study_id,
            "canonical_spec_sha256": canonical_json_digest(
                self.spec.canonical_payload()
            ),
            "spec": self.spec.canonical_payload(),
            "cells": [cell.canonical_payload() for cell in self.cells],
            "trials": [trial.canonical_payload() for trial in self.trials],
            "schema_digests": dict(self.schema_digests),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.canonical_payload())
