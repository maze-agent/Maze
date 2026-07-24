"""Strict TOML loading for versioned C14 ExperimentSpec documents."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Mapping, Sequence, cast

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - covered in the supported Python 3.10 environment
    import tomli as tomllib

from ascend_maze.benchmark.contracts import (
    SCHEMA_VERSION,
    AnalysisSpec,
    ArrivalSpec,
    CellDefinition,
    ConfigOverride,
    ExperimentSpec,
    ExternalAdapterSpec,
    FactorSpec,
    FileArtifact,
    MatrixSpec,
    MeasurementWindows,
    StudyPlan,
    WorkloadSpec,
)
from ascend_maze.benchmark.planning import build_study_plan, file_sha256
from ascend_maze.config import load_config
from ascend_maze.core.canonical import CanonicalValue
from ascend_maze.core.errors import (
    ContractValidationError,
    ExperimentValidationError,
)

_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "study_name",
        "study_kind",
        "base_seed",
        "block_count",
        "repetition_count",
        "build_revision",
        "base_config",
        "base_config_sha256",
        "workload",
        "arrival",
        "windows",
        "analysis",
        "matrix",
        "baselines",
    }
)
_WORKLOAD_KEYS = frozenset(
    {
        "name",
        "workflow_factory",
        "workflow_fingerprint",
        "model_catalog_revision",
        "model_artifact_digest",
        "required_environment_fingerprint",
        "inputs",
    }
)
_INPUT_KEYS = frozenset({"logical_name", "path", "sha256", "size_bytes"})
_ARRIVAL_KEYS = frozenset({"mode", "concurrency", "rate_per_second", "trace_input"})
_WINDOW_KEYS = frozenset(
    {
        "warmup_runs",
        "warmup_duration_ms",
        "measurement_run_count",
        "measurement_duration_ms",
        "drain_deadline_ms",
    }
)
_ANALYSIS_KEYS = frozenset(
    {
        "metric_set",
        "validity_policy",
        "statistics_policy",
        "performance_budget_set",
        "quantile_method",
        "bootstrap_samples",
        "confidence_level",
        "familywise_confidence_level",
        "automatic_outlier_removal",
    }
)
_MATRIX_KEYS = frozenset({"kind", "baseline_cell", "factors", "cells"})
_FACTOR_KEYS = frozenset({"name", "allowed_paths"})
_CELL_KEYS = frozenset({"name", "factors", "confirmatory", "overrides"})
_OVERRIDE_KEYS = frozenset({"path", "value"})
_BASELINE_KEYS = frozenset({"id", "argv", "executable_sha256"})


def load_experiment_spec(path: str | Path) -> ExperimentSpec:
    source = _resolve_file(path, "ExperimentSpec")
    try:
        raw_bytes = source.read_bytes()
        document = tomllib.loads(raw_bytes.decode("utf-8"))
    except OSError as exc:
        raise ExperimentValidationError(
            f"ExperimentSpec is unavailable: {source}"
        ) from exc
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ExperimentValidationError(
            f"ExperimentSpec contains invalid TOML: {exc}"
        ) from exc
    _reject_unknown(document, _ROOT_KEYS, "")
    schema_version = _integer(
        _required(document, "schema_version", ""), "schema_version"
    )
    if schema_version != SCHEMA_VERSION:
        raise ExperimentValidationError("schema_version: only version 1 is supported")
    build_revision = _string(
        _required(document, "build_revision", ""), "build_revision"
    )
    base_config = _resolve_relative_file(
        source.parent,
        _string(_required(document, "base_config", ""), "base_config"),
        "base_config",
    )
    base_digest = _sha256(
        _required(document, "base_config_sha256", ""), "base_config_sha256"
    )
    if file_sha256(base_config) != base_digest:
        raise ExperimentValidationError(
            "base_config_sha256: digest does not match base_config"
        )
    try:
        loaded = load_config(
            base_config,
            build_revision=build_revision,
            created_at_ms=0,
        )
    except ContractValidationError as exc:
        raise ExperimentValidationError(f"base_config: {exc}") from exc

    workload_raw = _table(document, "workload", "")
    _reject_unknown(workload_raw, _WORKLOAD_KEYS, "workload")
    inputs = tuple(
        _load_input(source.parent, item, index)
        for index, item in enumerate(_array_tables(workload_raw, "inputs", "workload"))
    )
    workload = WorkloadSpec(
        name=_string(_required(workload_raw, "name", "workload"), "workload.name"),
        workflow_factory=_string(
            _required(workload_raw, "workflow_factory", "workload"),
            "workload.workflow_factory",
        ),
        workflow_fingerprint=_sha256(
            _required(workload_raw, "workflow_fingerprint", "workload"),
            "workload.workflow_fingerprint",
        ),
        model_catalog_revision=_string(
            _required(workload_raw, "model_catalog_revision", "workload"),
            "workload.model_catalog_revision",
        ),
        model_artifact_digest=_sha256(
            _required(workload_raw, "model_artifact_digest", "workload"),
            "workload.model_artifact_digest",
        ),
        required_environment_fingerprint=_sha256(
            _required(
                workload_raw,
                "required_environment_fingerprint",
                "workload",
            ),
            "workload.required_environment_fingerprint",
        ),
        inputs=inputs,
    )

    arrival_raw = _table(document, "arrival", "")
    _reject_unknown(arrival_raw, _ARRIVAL_KEYS, "arrival")
    arrival = ArrivalSpec(
        mode=_string(_required(arrival_raw, "mode", "arrival"), "arrival.mode"),
        concurrency=_optional_integer(
            arrival_raw.get("concurrency"), "arrival.concurrency"
        ),
        rate_per_second=_optional_number(
            arrival_raw.get("rate_per_second"), "arrival.rate_per_second"
        ),
        trace_input=_optional_string(
            arrival_raw.get("trace_input"), "arrival.trace_input"
        ),
    )

    windows_raw = _table(document, "windows", "")
    _reject_unknown(windows_raw, _WINDOW_KEYS, "windows")
    windows = MeasurementWindows(
        warmup_runs=_integer(
            _required(windows_raw, "warmup_runs", "windows"),
            "windows.warmup_runs",
        ),
        warmup_duration_ms=_integer(
            _required(windows_raw, "warmup_duration_ms", "windows"),
            "windows.warmup_duration_ms",
        ),
        measurement_run_count=_integer(
            _required(windows_raw, "measurement_run_count", "windows"),
            "windows.measurement_run_count",
        ),
        measurement_duration_ms=_integer(
            _required(windows_raw, "measurement_duration_ms", "windows"),
            "windows.measurement_duration_ms",
        ),
        drain_deadline_ms=_integer(
            _required(windows_raw, "drain_deadline_ms", "windows"),
            "windows.drain_deadline_ms",
        ),
    )

    analysis_raw = _table(document, "analysis", "")
    _reject_unknown(analysis_raw, _ANALYSIS_KEYS, "analysis")
    analysis = AnalysisSpec(
        metric_set=_string_array(
            _required(analysis_raw, "metric_set", "analysis"),
            "analysis.metric_set",
        ),
        validity_policy=_string(
            analysis_raw.get("validity_policy", "c14_v1"),
            "analysis.validity_policy",
        ),
        statistics_policy=_string(
            analysis_raw.get("statistics_policy", "c14_v1"),
            "analysis.statistics_policy",
        ),
        performance_budget_set=_string(
            analysis_raw.get("performance_budget_set", "c14_v1"),
            "analysis.performance_budget_set",
        ),
        quantile_method=_string(
            analysis_raw.get("quantile_method", "hyndman_fan_type_7"),
            "analysis.quantile_method",
        ),
        bootstrap_samples=_integer(
            analysis_raw.get("bootstrap_samples", 10_000),
            "analysis.bootstrap_samples",
        ),
        confidence_level=_number(
            analysis_raw.get("confidence_level", 0.95),
            "analysis.confidence_level",
        ),
        familywise_confidence_level=_number(
            analysis_raw.get("familywise_confidence_level", 0.9875),
            "analysis.familywise_confidence_level",
        ),
        automatic_outlier_removal=_boolean(
            analysis_raw.get("automatic_outlier_removal", False),
            "analysis.automatic_outlier_removal",
        ),
    )

    matrix_raw = _table(document, "matrix", "")
    _reject_unknown(matrix_raw, _MATRIX_KEYS, "matrix")
    factors = tuple(
        _load_factor(item, index)
        for index, item in enumerate(_array_tables(matrix_raw, "factors", "matrix"))
    )
    cells = tuple(
        _load_cell(item, index)
        for index, item in enumerate(_array_tables(matrix_raw, "cells", "matrix"))
    )
    matrix = MatrixSpec(
        kind=_string(_required(matrix_raw, "kind", "matrix"), "matrix.kind"),
        baseline_cell=_string(
            _required(matrix_raw, "baseline_cell", "matrix"),
            "matrix.baseline_cell",
        ),
        factors=factors,
        cells=cells,
    )
    baselines = tuple(
        _load_baseline(item, index)
        for index, item in enumerate(_optional_array_tables(document, "baselines", ""))
    )
    return ExperimentSpec(
        schema_version=schema_version,
        study_name=_string(_required(document, "study_name", ""), "study_name"),
        study_kind=_string(_required(document, "study_kind", ""), "study_kind"),
        base_seed=_integer(_required(document, "base_seed", ""), "base_seed"),
        block_count=_integer(_required(document, "block_count", ""), "block_count"),
        repetition_count=_integer(
            _required(document, "repetition_count", ""), "repetition_count"
        ),
        build_revision=build_revision,
        base_config_path=str(base_config),
        base_config_source_digest=base_digest,
        base_config_snapshot=loaded.snapshot,
        workload=workload,
        arrival=arrival,
        windows=windows,
        analysis=analysis,
        matrix=matrix,
        baselines=baselines,
    )


def load_study_plan(path: str | Path) -> StudyPlan:
    return build_study_plan(load_experiment_spec(path))


def _load_input(base: Path, raw: Mapping[str, object], index: int) -> FileArtifact:
    prefix = f"workload.inputs[{index}]"
    _reject_unknown(raw, _INPUT_KEYS, prefix)
    path = _resolve_relative_file(
        base,
        _string(_required(raw, "path", prefix), f"{prefix}.path"),
        f"{prefix}.path",
    )
    expected_digest = _sha256(_required(raw, "sha256", prefix), f"{prefix}.sha256")
    expected_size = _integer(
        _required(raw, "size_bytes", prefix), f"{prefix}.size_bytes"
    )
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise ExperimentValidationError(f"{prefix}.path: file is unavailable") from exc
    if actual_size != expected_size:
        raise ExperimentValidationError(
            f"{prefix}.size_bytes: expected {expected_size}, found {actual_size}"
        )
    if file_sha256(path) != expected_digest:
        raise ExperimentValidationError(f"{prefix}.sha256: digest mismatch")
    return FileArtifact(
        logical_name=_string(
            _required(raw, "logical_name", prefix), f"{prefix}.logical_name"
        ),
        source_path=str(path),
        content_sha256=expected_digest,
        size_bytes=expected_size,
    )


def _load_factor(raw: Mapping[str, object], index: int) -> FactorSpec:
    prefix = f"matrix.factors[{index}]"
    _reject_unknown(raw, _FACTOR_KEYS, prefix)
    return FactorSpec(
        name=_string(_required(raw, "name", prefix), f"{prefix}.name"),
        allowed_paths=_string_array(
            _required(raw, "allowed_paths", prefix), f"{prefix}.allowed_paths"
        ),
    )


def _load_cell(raw: Mapping[str, object], index: int) -> CellDefinition:
    prefix = f"matrix.cells[{index}]"
    _reject_unknown(raw, _CELL_KEYS, prefix)
    overrides: list[ConfigOverride] = []
    seen: set[str] = set()
    for override_index, item in enumerate(
        _optional_array_tables(raw, "overrides", prefix)
    ):
        override_prefix = f"{prefix}.overrides[{override_index}]"
        _reject_unknown(item, _OVERRIDE_KEYS, override_prefix)
        path = _string(
            _required(item, "path", override_prefix), f"{override_prefix}.path"
        )
        if path in seen:
            raise ExperimentValidationError(
                f"{prefix}: duplicate override path: {path}"
            )
        seen.add(path)
        overrides.append(
            ConfigOverride(
                path=path,
                value=cast(
                    CanonicalValue,
                    _required(item, "value", override_prefix),
                ),
            )
        )
    return CellDefinition(
        name=_string(_required(raw, "name", prefix), f"{prefix}.name"),
        factors=_string_array(raw.get("factors", []), f"{prefix}.factors"),
        overrides=tuple(overrides),
        confirmatory=_boolean(raw.get("confirmatory", True), f"{prefix}.confirmatory"),
    )


def _load_baseline(raw: Mapping[str, object], index: int) -> ExternalAdapterSpec:
    prefix = f"baselines[{index}]"
    _reject_unknown(raw, _BASELINE_KEYS, prefix)
    return ExternalAdapterSpec(
        adapter_id=_string(_required(raw, "id", prefix), f"{prefix}.id"),
        argv=_string_array(_required(raw, "argv", prefix), f"{prefix}.argv"),
        executable_sha256=_sha256(
            _required(raw, "executable_sha256", prefix),
            f"{prefix}.executable_sha256",
        ),
    )


def _resolve_file(path: str | Path, name: str) -> Path:
    try:
        candidate = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ExperimentValidationError(f"{name}: file does not exist: {path}") from exc
    if not candidate.is_file():
        raise ExperimentValidationError(f"{name}: not a file: {candidate}")
    return candidate


def _resolve_relative_file(base: Path, value: str, name: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return _resolve_file(candidate, name)


def _required(document: Mapping[str, object], key: str, prefix: str) -> object:
    if key not in document:
        field = f"{prefix}.{key}" if prefix else key
        raise ExperimentValidationError(f"{field}: required field is missing")
    return document[key]


def _table(
    document: Mapping[str, object], key: str, prefix: str
) -> Mapping[str, object]:
    value = _required(document, key, prefix)
    field = f"{prefix}.{key}" if prefix else key
    if not isinstance(value, dict):
        raise ExperimentValidationError(f"{field}: must be a TOML table")
    return value


def _array_tables(
    document: Mapping[str, object], key: str, prefix: str
) -> tuple[Mapping[str, object], ...]:
    return _coerce_array_tables(_required(document, key, prefix), key, prefix)


def _optional_array_tables(
    document: Mapping[str, object], key: str, prefix: str
) -> tuple[Mapping[str, object], ...]:
    return _coerce_array_tables(document.get(key, []), key, prefix)


def _coerce_array_tables(
    value: object, key: str, prefix: str
) -> tuple[Mapping[str, object], ...]:
    field = f"{prefix}.{key}" if prefix else key
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ExperimentValidationError(f"{field}: must be an array of tables")
    return tuple(cast(Sequence[Mapping[str, object]], value))


def _reject_unknown(
    document: Mapping[str, object], allowed: frozenset[str], prefix: str
) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        field = f"{prefix}.{unknown[0]}" if prefix else unknown[0]
        raise ExperimentValidationError(f"{field}: unknown ExperimentSpec field")


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExperimentValidationError(f"{field}: must be a non-empty string")
    return value


def _optional_string(value: object, field: str) -> str | None:
    return None if value is None else _string(value, field)


def _sha256(value: object, field: str) -> str:
    result = _string(value, field)
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ExperimentValidationError(f"{field}: must be a lowercase SHA-256 digest")
    return result


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExperimentValidationError(f"{field}: must be an integer")
    return value


def _optional_integer(value: object, field: str) -> int | None:
    return None if value is None else _integer(value, field)


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExperimentValidationError(f"{field}: must be a number")
    return float(value)


def _optional_number(value: object, field: str) -> float | None:
    return None if value is None else _number(value, field)


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ExperimentValidationError(f"{field}: must be a boolean")
    return value


def _string_array(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ExperimentValidationError(f"{field}: must be an array of strings")
    return tuple(value)
