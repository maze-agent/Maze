"""Offline C14 metric aggregation and paired comparison pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Mapping, Sequence, cast

from ascend_maze.benchmark.aggregate_artifacts import (
    read_trial_metrics,
    write_csv,
    write_run_metrics,
    write_trial_metrics,
)
from ascend_maze.benchmark.analysis_inputs import (
    ValidatedAnalysisInputs,
    validate_analysis_inputs,
    verify_analysis_input_records,
)
from ascend_maze.benchmark.canonical import (
    canonical_json_digest,
    derive_seed,
    stable_payload_id,
)
from ascend_maze.benchmark.metrics import (
    CORRECTNESS_GUARD_METRICS,
    RunFact,
    extract_metric,
    metric_definition,
)
from ascend_maze.benchmark.parquet_import import import_committed_parquet
from ascend_maze.benchmark.persistence import atomic_write_json, load_json_object
from ascend_maze.benchmark.statistics import (
    BOOTSTRAP_ALGORITHM,
    budget_decision,
    degradation_percent,
    deterministic_bootstrap_interval,
    relative_effect_percent,
    summarize_distribution,
)
from ascend_maze.contracts.recording import ExecutionEvent
from ascend_maze.core.errors import ExperimentValidationError


AGGREGATOR_VERSION = "c14_14d_v1"

CELL_FIELDS = (
    "study_id",
    "cell_id",
    "cell_name",
    "metric_name",
    "unit",
    "valid_trial_count",
    "invalid_trial_count",
    "valid_block_count",
    "sample_count",
    "median",
    "mean",
    "standard_deviation",
    "mad",
    "minimum",
    "maximum",
    "p50",
    "p95",
    "p99",
    "p99_status",
)
COMPARISON_FIELDS = (
    "study_id",
    "comparison_id",
    "baseline_cell_id",
    "baseline_cell_name",
    "candidate_cell_id",
    "candidate_cell_name",
    "metric_name",
    "unit",
    "direction",
    "paired_blocks",
    "absolute_effect",
    "relative_effect_pct",
    "ci95_lower",
    "ci95_upper",
    "familywise_ci_lower",
    "familywise_ci_upper",
    "budget_name",
    "budget_limit_pct",
    "budget_upper95",
    "guard_decision",
    "guard_reasons",
    "decision",
)
VALIDITY_FIELDS = (
    "study_id",
    "cell_id",
    "cell_name",
    "block_index",
    "repetition_index",
    "trial_id",
    "trial_attempt_id",
    "metric_name",
    "trial_valid",
    "metric_valid",
    "reason_codes",
)


@dataclass(frozen=True, slots=True)
class _Cell:
    cell_id: str
    name: str
    confirmatory: bool


@dataclass(frozen=True, slots=True)
class _Trial:
    trial_id: str
    trial_attempt_id: str
    cell_id: str
    cell_name: str
    block_index: int
    repetition_index: int
    pairing_seed: int
    directory: Path


@dataclass(frozen=True, slots=True)
class _Study:
    root: Path
    study_id: str
    plan: Mapping[str, object]
    spec: Mapping[str, object]
    cells: tuple[_Cell, ...]
    trials: tuple[_Trial, ...]
    metric_names: tuple[str, ...]
    measurement_duration_ms: int
    bootstrap_samples: int
    confidence_level: float
    familywise_confidence_level: float
    baseline_cell_name: str
    matrix_kind: str


def aggregate_study(study_directory: str | Path) -> dict[str, object]:
    study = load_study(study_directory)
    run_rows: list[dict[str, object]] = []
    trial_rows: list[dict[str, object]] = []
    valid_run_ids: set[str] = set()
    trial_rows_by_attempt: dict[str, list[dict[str, object]]] = {}

    for trial in study.trials:
        validity = _load_validity(trial)
        raw = _load_raw_files(trial, validity)
        formal: ValidatedAnalysisInputs | None
        try:
            formal = _load_formal_inputs(trial, validity)
        except ExperimentValidationError:
            if validity["trial_valid"] is True:
                raise
            formal = None
        events = _load_committed_events(raw) if validity["trial_valid"] is True else ()
        runs = tuple(
            RunFact(
                run_id=run.run_id,
                phase=run.phase,
                offered_at_ms=run.offered_at_monotonic_ms,
                issued_at_ms=run.issued_at_monotonic_ms,
                admitted_at_ms=run.admitted_at_monotonic_ms,
                terminal_at_ms=run.terminal_at_monotonic_ms,
                terminal_status=run.terminal_status,
                scheduled_at_ms=run.scheduled_at_monotonic_ms,
                scheduled_offset_ms=run.scheduled_offset_ms,
                arrival_lateness_ms=run.arrival_lateness_ms,
            )
            for run in (() if formal is None else formal.run_manifest.runs)
        )
        if validity["trial_valid"] is True and formal is not None:
            valid_run_ids.update(
                run.run_id
                for run in formal.run_manifest.runs
                if run.phase == "measurement" and run.run_id is not None
            )
        configured_validity = _metric_validity_map(validity)
        try:
            flush_results = _flush_results(trial.directory)
        except ExperimentValidationError:
            if validity["trial_valid"] is True:
                raise
            flush_results = ()
        per_trial_rows: list[dict[str, object]] = []
        for metric_name in study.metric_names:
            declared_valid, declared_reasons = configured_validity.get(
                metric_name, (False, ("metric_dependency_unknown",))
            )
            extraction = extract_metric(
                metric_name,
                events=events,
                runs=runs,
                measurement_duration_ms=study.measurement_duration_ms,
                recording_complete=_recording_complete(flush_results),
                flush_results=flush_results,
                resource_after=None if formal is None else formal.resource_after,
            )
            reasons = set(declared_reasons)
            if validity["trial_valid"] is not True:
                reasons.update(_string_list(validity.get("reason_codes"), "reason codes"))
            if not declared_valid:
                reasons.update(declared_reasons)
            reasons.update(extraction.reason_codes)
            metric_valid = validity["trial_valid"] is True and declared_valid and not reasons
            samples = extraction.samples if metric_valid else ()
            ordered_samples = tuple(
                sorted(
                    samples,
                    key=lambda item: (
                        item.run_id or "",
                        item.node_id or "",
                        item.device_id or "",
                        item.producer_id or "",
                        item.value,
                    ),
                )
            )
            stable_reasons = tuple(sorted(reasons))
            if ordered_samples:
                for sample_index, sample in enumerate(ordered_samples):
                    run_rows.append(
                        _run_metric_row(
                            study,
                            trial,
                            metric_name,
                            extraction.definition.unit,
                            sample_index,
                            sample.run_id,
                            sample.node_id,
                            sample.device_id,
                            sample.producer_id,
                            sample.value,
                            True,
                            (),
                            validity["trial_valid"] is True,
                        )
                    )
            else:
                run_rows.append(
                    _run_metric_row(
                        study,
                        trial,
                        metric_name,
                        extraction.definition.unit,
                        -1,
                        None,
                        None,
                        None,
                        None,
                        None,
                        False,
                        stable_reasons or ("metric_required_fact_missing",),
                        validity["trial_valid"] is True,
                    )
                )
            summary = summarize_distribution(sample.value for sample in ordered_samples)
            row = {
                "schema_version": 1,
                "study_id": study.study_id,
                "cell_id": trial.cell_id,
                "cell_name": trial.cell_name,
                "block_index": trial.block_index,
                "repetition_index": trial.repetition_index,
                "pairing_seed": trial.pairing_seed,
                "trial_id": trial.trial_id,
                "trial_attempt_id": trial.trial_attempt_id,
                "trial_valid": validity["trial_valid"] is True,
                "measurement_id": stable_payload_id(
                    "measurement",
                    {
                        "trial_attempt_id": trial.trial_attempt_id,
                        "metric_name": metric_name,
                        "metric_schema_version": 1,
                    },
                ),
                "metric_name": metric_name,
                "unit": extraction.definition.unit,
                "higher_is_better": extraction.definition.higher_is_better,
                "metric_valid": metric_valid,
                "reason_codes": list(stable_reasons),
                "sample_count": summary.sample_count,
                "primary_value": summary.median,
                "mean": summary.mean,
                "standard_deviation": summary.standard_deviation,
                "mad": summary.mad,
                "minimum": summary.minimum,
                "maximum": summary.maximum,
                "p50": summary.p50,
                "p95": summary.p95,
                "p99": summary.p99,
                "p99_status": "pending_study_sample_check" if metric_valid else "invalid",
            }
            per_trial_rows.append(row)
            trial_rows.append(row)
        trial_rows_by_attempt[trial.trial_attempt_id] = per_trial_rows

    _finalize_p99_status(trial_rows, len(valid_run_ids))
    for trial in study.trials:
        write_trial_metrics(
            trial.directory / "trial_metrics.parquet",
            trial_rows_by_attempt[trial.trial_attempt_id],
        )
    aggregate_root = study.root / "aggregates"
    write_run_metrics(aggregate_root / "run_metrics.parquet", run_rows)
    write_trial_metrics(aggregate_root / "trial_metrics.parquet", trial_rows)
    persisted_trial_rows = read_trial_metrics(
        aggregate_root / "trial_metrics.parquet"
    )
    cell_rows, comparison_rows, validity_rows = materialize_aggregate_views(
        study, persisted_trial_rows
    )
    write_csv(aggregate_root / "cell_metrics.csv", cell_rows, CELL_FIELDS)
    write_csv(
        aggregate_root / "comparisons.csv", comparison_rows, COMPARISON_FIELDS
    )
    write_csv(aggregate_root / "validity.csv", validity_rows, VALIDITY_FIELDS)
    digests = {
        name: _sha256(aggregate_root / name)
        for name in (
            "run_metrics.parquet",
            "trial_metrics.parquet",
            "cell_metrics.csv",
            "comparisons.csv",
            "validity.csv",
        )
    }
    source_validity_digests = {
        trial.trial_attempt_id: _string(
            _load_validity(trial), "validation_digest", "Trial validity"
        )
        for trial in study.trials
    }
    atomic_write_json(
        aggregate_root / "manifest.json",
        {
            "schema_version": 1,
            "schema": "ascend-maze.aggregate-manifest.v1",
            "study_id": study.study_id,
            "aggregator_version": AGGREGATOR_VERSION,
            "source_validity_digests": dict(sorted(source_validity_digests.items())),
            "output_digests": dict(sorted(digests.items())),
        },
    )
    return {
        "schema_version": 1,
        "schema": "ascend-maze.aggregate-result.v1",
        "study_id": study.study_id,
        "trial_count": len(study.trials),
        "valid_trial_count": len(
            {row["trial_attempt_id"] for row in trial_rows if row["trial_valid"] is True}
        ),
        "metric_row_count": len(trial_rows),
        "paired_comparison_count": len(comparison_rows),
        "output_digests": dict(sorted(digests.items())),
        "aggregator_version": AGGREGATOR_VERSION,
        "bootstrap_algorithm": BOOTSTRAP_ALGORITHM,
    }


def materialize_aggregate_views(
    study: _Study,
    trial_rows: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    cell_rows = _cell_rows(study, trial_rows)
    comparison_rows = _comparison_rows(study, trial_rows)
    validity_rows = _validity_rows(trial_rows)
    return cell_rows, comparison_rows, validity_rows


def rebuild_aggregate_csv(study_directory: str | Path) -> dict[str, object]:
    study = load_study(study_directory)
    aggregate_root = study.root / "aggregates"
    trial_rows = read_trial_metrics(aggregate_root / "trial_metrics.parquet")
    cell_rows, comparison_rows, validity_rows = materialize_aggregate_views(study, trial_rows)
    write_csv(aggregate_root / "cell_metrics.csv", cell_rows, CELL_FIELDS)
    write_csv(aggregate_root / "comparisons.csv", comparison_rows, COMPARISON_FIELDS)
    write_csv(aggregate_root / "validity.csv", validity_rows, VALIDITY_FIELDS)
    return {
        "cell_rows": len(cell_rows),
        "comparison_rows": len(comparison_rows),
        "validity_rows": len(validity_rows),
    }


def load_study(study_directory: str | Path) -> _Study:
    try:
        root = Path(study_directory).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ExperimentValidationError("Study directory is unavailable") from exc
    if not root.is_dir():
        raise ExperimentValidationError("Study path is not a directory")
    plan = load_json_object(root / "study_plan.json", description="Study plan")
    manifest = load_json_object(root / "study_manifest.json", description="Study manifest")
    if plan.get("schema") != "ascend-maze.study-plan.v1" or plan.get("schema_version") != 1:
        raise ExperimentValidationError("Study plan schema is invalid")
    study_id = _string(plan, "study_id", "Study plan")
    if manifest.get("study_id") != study_id:
        raise ExperimentValidationError("Study manifest identity is invalid")
    if manifest.get("plan_sha256") != canonical_json_digest(plan):
        raise ExperimentValidationError("Study plan digest does not match manifest")
    spec = _mapping(plan.get("spec"), "Study spec")
    if plan.get("canonical_spec_sha256") != canonical_json_digest(spec):
        raise ExperimentValidationError("Study spec digest is invalid")
    analysis = _mapping(spec.get("analysis"), "Study analysis")
    requested_metrics = tuple(
        sorted(_string_list(analysis.get("metric_set"), "metric set"))
    )
    if not requested_metrics or len(requested_metrics) != len(set(requested_metrics)):
        raise ExperimentValidationError("Study metric set is invalid")
    metric_names = tuple(
        sorted(
            {
                *requested_metrics,
                *CORRECTNESS_GUARD_METRICS,
            }
        )
    )
    cells = tuple(
        _Cell(
            _string(cell, "cell_id", "Study Cell"),
            _string(cell, "name", "Study Cell"),
            _boolean(cell, "confirmatory", "Study Cell"),
        )
        for cell in (
            _mapping(item, "Study Cell")
            for item in _list(plan.get("cells"), "Study Cells")
        )
    )
    cell_by_id = {cell.cell_id: cell for cell in cells}
    trial_plan = {
        _string(item, "trial_id", "Study Trial"): item
        for item in (
            _mapping(raw, "Study Trial")
            for raw in _list(plan.get("trials"), "Study Trials")
        )
    }
    manifest_entries = _list(manifest.get("trials"), "Study manifest Trials")
    trials: list[_Trial] = []
    for raw in manifest_entries:
        entry = _mapping(raw, "Study manifest Trial")
        trial_id = _string(entry, "trial_id", "Study manifest Trial")
        planned = trial_plan.get(trial_id)
        if planned is None:
            raise ExperimentValidationError("Study manifest has an unknown Trial")
        cell_id = _string(planned, "cell_id", "Study Trial")
        cell = cell_by_id.get(cell_id)
        if cell is None:
            raise ExperimentValidationError("Study Trial has an unknown Cell")
        relative = Path(_string(entry, "relative_directory", "Study manifest Trial"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ExperimentValidationError("Study Trial directory is invalid")
        directory = (root / relative).resolve(strict=False)
        try:
            directory.relative_to(root)
        except ValueError as exc:
            raise ExperimentValidationError("Study Trial directory escapes Study") from exc
        trials.append(
            _Trial(
                trial_id=trial_id,
                trial_attempt_id=_string(
                    entry, "trial_attempt_id", "Study manifest Trial"
                ),
                cell_id=cell_id,
                cell_name=cell.name,
                block_index=_integer(planned, "block_index", "Study Trial"),
                repetition_index=_integer(
                    planned, "repetition_index", "Study Trial"
                ),
                pairing_seed=_integer(planned, "pairing_seed", "Study Trial"),
                directory=directory,
            )
        )
    windows = _mapping(spec.get("windows"), "Study windows")
    matrix = _mapping(spec.get("matrix"), "Study matrix")
    return _Study(
        root=root,
        study_id=study_id,
        plan=plan,
        spec=spec,
        cells=cells,
        trials=tuple(
            sorted(
                trials,
                key=lambda item: (
                    item.block_index,
                    item.repetition_index,
                    item.cell_id,
                    item.trial_attempt_id,
                ),
            )
        ),
        metric_names=metric_names,
        measurement_duration_ms=_integer(
            windows, "measurement_duration_ms", "Study windows"
        ),
        bootstrap_samples=_integer(
            analysis, "bootstrap_samples", "Study analysis"
        ),
        confidence_level=_float(analysis, "confidence_level", "Study analysis"),
        familywise_confidence_level=_float(
            analysis, "familywise_confidence_level", "Study analysis"
        ),
        baseline_cell_name=_string(matrix, "baseline_cell", "Study matrix"),
        matrix_kind=_string(matrix, "kind", "Study matrix"),
    )


def _load_validity(trial: _Trial) -> Mapping[str, object]:
    payload = load_json_object(trial.directory / "validity.json", description="Trial validity")
    if (
        payload.get("schema_version") != 1
        or payload.get("schema") != "ascend-maze.trial-validity.v1"
        or payload.get("trial_id") != trial.trial_id
        or payload.get("trial_attempt_id") != trial.trial_attempt_id
    ):
        raise ExperimentValidationError("Trial validity identity is invalid")
    body = {
        key: value
        for key, value in payload.items()
        if key not in {"schema_version", "schema", "validation_digest"}
    }
    if payload.get("validation_digest") != canonical_json_digest(body):
        raise ExperimentValidationError("Trial validity digest is invalid")
    if not isinstance(payload.get("trial_valid"), bool):
        raise ExperimentValidationError("Trial validity flag is invalid")
    return payload


def _load_raw_files(
    trial: _Trial, validity: Mapping[str, object]
) -> Mapping[str, object]:
    raw = load_json_object(trial.directory / "raw_files.json", description="raw files")
    if raw.get("schema_version") != 1 or raw.get("schema") != "ascend-maze.raw-files.v1":
        raise ExperimentValidationError("raw files schema is invalid")
    identity = {
        key: value
        for key, value in raw.items()
        if key not in {"schema_version", "schema", "content_digest"}
    }
    digest = canonical_json_digest(identity)
    if raw.get("content_digest") != digest or validity.get("raw_files_digest") != digest:
        raise ExperimentValidationError("raw files digest is invalid")
    return raw


def _load_formal_inputs(
    trial: _Trial, validity: Mapping[str, object]
) -> ValidatedAnalysisInputs:
    records = _list(validity.get("analysis_inputs"), "analysis inputs")
    mappings = tuple(_mapping(item, "analysis input") for item in records)
    verify_analysis_input_records(trial.directory, mappings)
    if validity.get("analysis_inputs_digest") != canonical_json_digest(mappings):
        raise ExperimentValidationError("analysis input digest is invalid")
    return validate_analysis_inputs(
        trial.directory,
        trial_attempt_id=trial.trial_attempt_id,
        committed_run_ids=tuple(
            _string(item, "run_id", "flush result")
            for item in _flush_results(trial.directory)
        ),
    )


def _load_committed_events(raw: Mapping[str, object]) -> tuple[ExecutionEvent, ...]:
    events: list[ExecutionEvent] = []
    files = sorted(
        (_mapping(item, "raw file") for item in _list(raw.get("files"), "raw files")),
        key=lambda item: _string(item, "logical_name", "raw file"),
    )
    for item in files:
        source = Path(_string(item, "source_path", "raw file"))
        content = import_committed_parquet(source)
        observation = content.observation
        if (
            item.get("size_bytes") != observation.size_bytes
            or item.get("sha256") != observation.sha256
            or item.get("parquet_kind") != content.kind
            or item.get("row_count") != content.row_count
        ):
            raise ExperimentValidationError("committed raw file changed after validation")
        events.extend(content.events)
    return tuple(events)


def _metric_validity_map(
    validity: Mapping[str, object],
) -> dict[str, tuple[bool, tuple[str, ...]]]:
    result: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for raw in _list(validity.get("metric_valid"), "metric validity"):
        item = _mapping(raw, "metric validity")
        name = _string(item, "metric_name", "metric validity")
        if name in result:
            raise ExperimentValidationError("metric validity contains duplicates")
        result[name] = (
            _boolean(item, "valid", "metric validity"),
            tuple(_string_list(item.get("reason_codes"), "metric reason codes")),
        )
    return result


def _flush_results(directory: Path) -> tuple[Mapping[str, object], ...]:
    payload = load_json_object(directory / "flush_results.json", description="flush results")
    return tuple(
        _mapping(item, "flush result")
        for item in _list(payload.get("results"), "flush results")
    )


def _recording_complete(results: Sequence[Mapping[str, object]]) -> bool:
    return bool(results) and all(item.get("recording_complete") is True for item in results)


def _run_metric_row(
    study: _Study,
    trial: _Trial,
    metric_name: str,
    unit: str,
    sample_index: int,
    run_id: str | None,
    node_id: str | None,
    device_id: str | None,
    producer_id: str | None,
    value: float | None,
    valid: bool,
    reason_codes: tuple[str, ...],
    trial_valid: bool,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "study_id": study.study_id,
        "cell_id": trial.cell_id,
        "cell_name": trial.cell_name,
        "block_index": trial.block_index,
        "repetition_index": trial.repetition_index,
        "pairing_seed": trial.pairing_seed,
        "trial_id": trial.trial_id,
        "trial_attempt_id": trial.trial_attempt_id,
        "trial_valid": trial_valid,
        "metric_name": metric_name,
        "unit": unit,
        "sample_index": sample_index,
        "run_id": run_id,
        "node_id": node_id,
        "device_id": device_id,
        "producer_id": producer_id,
        "value": value,
        "valid": valid,
        "reason_codes": list(reason_codes),
    }


def _finalize_p99_status(rows: Sequence[dict[str, object]], valid_run_count: int) -> None:
    by_metric: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_metric.setdefault(cast(str, row["metric_name"]), []).append(row)
    for metric_rows in by_metric.values():
        definition = metric_definition(cast(str, metric_rows[0]["metric_name"]))
        valid = [row for row in metric_rows if row["metric_valid"] is True]
        if definition is not None and definition.scope == "trial":
            for row in valid:
                row["p99_status"] = "not_applicable"
            continue
        study_sufficient = valid_run_count >= 1_000
        every_trial_sufficient = bool(valid) and all(
            cast(int, row["sample_count"]) >= 100 for row in valid
        )
        status = (
            "sufficient"
            if study_sufficient and every_trial_sufficient
            else "insufficient_sample"
        )
        for row in valid:
            row["p99_status"] = status


def _cell_rows(
    study: _Study, rows: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for cell in sorted(study.cells, key=lambda item: item.cell_id):
        for metric_name in study.metric_names:
            selected = [
                row
                for row in rows
                if row.get("cell_id") == cell.cell_id
                and row.get("metric_name") == metric_name
            ]
            valid = [
                row
                for row in selected
                if row.get("metric_valid") is True
                and isinstance(row.get("primary_value"), (int, float))
            ]
            block_values: dict[tuple[int, int], list[float]] = {}
            for row in valid:
                block_values.setdefault(
                    (
                        cast(int, row["block_index"]),
                        cast(int, row["repetition_index"]),
                    ),
                    [],
                ).append(float(cast(float, row["primary_value"])))
            block_results = [
                cast(float, summarize_distribution(values).median)
                for _, values in sorted(block_values.items())
            ]
            summary = summarize_distribution(block_results)
            sample_count = sum(cast(int, row["sample_count"]) for row in valid)
            statuses = {row.get("p99_status") for row in valid}
            p99_status = (
                "not_applicable"
                if statuses == {"not_applicable"}
                else "sufficient"
                if statuses == {"sufficient"}
                else "insufficient_sample"
            )
            unit = cast(str, selected[0]["unit"]) if selected else "unknown"
            quantiles = {
                name: summarize_distribution(
                    float(cast(float, row[name]))
                    for row in valid
                    if isinstance(row.get(name), (int, float))
                    and not isinstance(row.get(name), bool)
                ).median
                for name in ("p50", "p95", "p99")
            }
            result.append(
                {
                    "study_id": study.study_id,
                    "cell_id": cell.cell_id,
                    "cell_name": cell.name,
                    "metric_name": metric_name,
                    "unit": unit,
                    "valid_trial_count": len(valid),
                    "invalid_trial_count": len(selected) - len(valid),
                    "valid_block_count": summary.sample_count,
                    "sample_count": sample_count,
                    "median": summary.median,
                    "mean": summary.mean,
                    "standard_deviation": summary.standard_deviation,
                    "mad": summary.mad,
                    "minimum": summary.minimum,
                    "maximum": summary.maximum,
                    "p50": quantiles["p50"],
                    "p95": quantiles["p95"],
                    "p99": quantiles["p99"],
                    "p99_status": p99_status,
                }
            )
    return result


def _comparison_rows(
    study: _Study, rows: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    cell_by_name = {cell.name: cell for cell in study.cells}
    full = cell_by_name.get(study.baseline_cell_name)
    if full is None:
        return []
    result: list[dict[str, object]] = []
    others = [
        cell
        for cell in study.cells
        if cell.cell_id != full.cell_id and cell.confirmatory
    ]
    for other in sorted(others, key=lambda item: item.cell_id):
        if study.matrix_kind == "internal_ablation_v1":
            baseline_cell, candidate_cell = other, full
        else:
            baseline_cell, candidate_cell = full, other
        for metric_name in study.metric_names:
            budget = _budget_for(
                baseline_cell.name, candidate_cell.name, metric_name
            )
            value_field = (
                "p95"
                if budget is not None and budget[0].endswith("dct_p95")
                else "primary_value"
            )
            baseline_rows = _valid_metric_by_pair(
                rows, baseline_cell.cell_id, metric_name, value_field=value_field
            )
            candidate_rows = _valid_metric_by_pair(
                rows, candidate_cell.cell_id, metric_name, value_field=value_field
            )
            paired_keys = sorted(set(baseline_rows).intersection(candidate_rows))
            absolute: list[float] = []
            relative: list[float] = []
            degradation: list[float] = []
            for key in paired_keys:
                baseline = baseline_rows[key]
                candidate = candidate_rows[key]
                absolute.append(candidate - baseline)
                effect = relative_effect_percent(
                    baseline,
                    candidate,
                    higher_is_better=_higher_is_better(rows, metric_name),
                )
                degraded = degradation_percent(
                    baseline,
                    candidate,
                    higher_is_better=_higher_is_better(rows, metric_name),
                )
                if effect is not None and degraded is not None:
                    relative.append(effect)
                    degradation.append(degraded)
            seed = derive_seed(
                study.study_id,
                "paired_bootstrap",
                baseline_cell.cell_id,
                candidate_cell.cell_id,
                metric_name,
                BOOTSTRAP_ALGORITHM,
            )
            ci95 = (
                None
                if not relative
                else deterministic_bootstrap_interval(
                    relative,
                    seed=seed,
                    samples=study.bootstrap_samples,
                    confidence_level=study.confidence_level,
                )
            )
            family = (
                None
                if not relative
                else deterministic_bootstrap_interval(
                    relative,
                    seed=seed,
                    samples=study.bootstrap_samples,
                    confidence_level=study.familywise_confidence_level,
                )
            )
            budget_upper = None
            if budget is not None and degradation:
                budget_upper = deterministic_bootstrap_interval(
                    degradation,
                    seed=derive_seed(seed, "budget_upper"),
                    samples=study.bootstrap_samples,
                    confidence_level=study.confidence_level,
                    one_sided_upper=True,
                ).upper
                decision = budget_decision(
                    point_estimate=summarize_distribution(degradation).median,
                    upper_bound=budget_upper,
                    limit=budget[1],
                )
            elif not relative:
                decision = "insufficient_sample"
            elif family is not None and family.lower > 0:
                decision = "pass"
            elif family is not None and family.upper < 0:
                decision = "fail"
            else:
                decision = "borderline"
            if budget is not None and budget[0].startswith("c8_recording_"):
                recording_failures = {
                    reason
                    for row in rows
                    if row.get("cell_id") == candidate_cell.cell_id
                    for reason in cast(list[str], row.get("reason_codes", []))
                    if reason
                    in {
                        "dropped_control_events",
                        "dropped_telemetry_events",
                        "missing_producer_reported",
                        "producer_sequence_gap",
                        "recording_incomplete",
                        "sequence_gap_reported",
                        "writer_error",
                    }
                }
                if recording_failures:
                    decision = "fail"
            unit = _metric_unit(rows, metric_name)
            identity = {
                "baseline_cell_id": baseline_cell.cell_id,
                "candidate_cell_id": candidate_cell.cell_id,
                "metric_name": metric_name,
            }
            result.append(
                {
                    "study_id": study.study_id,
                    "comparison_id": stable_payload_id("comparison", identity),
                    "baseline_cell_id": baseline_cell.cell_id,
                    "baseline_cell_name": baseline_cell.name,
                    "candidate_cell_id": candidate_cell.cell_id,
                    "candidate_cell_name": candidate_cell.name,
                    "metric_name": metric_name,
                    "unit": unit,
                    "direction": "higher_is_better"
                    if _higher_is_better(rows, metric_name)
                    else "lower_is_better",
                    "paired_blocks": len(absolute),
                    "absolute_effect": summarize_distribution(absolute).median,
                    "relative_effect_pct": summarize_distribution(relative).median,
                    "ci95_lower": None if ci95 is None else ci95.lower,
                    "ci95_upper": None if ci95 is None else ci95.upper,
                    "familywise_ci_lower": None if family is None else family.lower,
                    "familywise_ci_upper": None if family is None else family.upper,
                    "budget_name": None if budget is None else budget[0],
                    "budget_limit_pct": None if budget is None else budget[1],
                    "budget_upper95": budget_upper,
                    "guard_decision": "pending",
                    "guard_reasons": [],
                    "decision": decision,
                }
            )
    _apply_guard_decisions(result)
    return result


def _apply_guard_decisions(rows: list[dict[str, object]]) -> None:
    grouped: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for row in rows:
        key = (
            cast(str, row["baseline_cell_id"]),
            cast(str, row["candidate_cell_id"]),
        )
        grouped.setdefault(key, {})[cast(str, row["metric_name"])] = row
    for metrics in grouped.values():
        reasons: list[str] = []
        guard_names = set(CORRECTNESS_GUARD_METRICS)
        if "scheduling_order_match" in metrics:
            guard_names.add("scheduling_order_match")
        for guard_name in sorted(guard_names):
            guard = metrics.get(guard_name)
            if guard is None or cast(int, guard["paired_blocks"]) == 0:
                reasons.append(f"guard_missing:{guard_name}")
                continue
            effect = guard["relative_effect_pct"]
            absolute = guard["absolute_effect"]
            lower = guard["ci95_lower"]
            if guard_name in {"scheduling_order_match", "success_rate"}:
                if (
                    not isinstance(effect, (int, float))
                    or not isinstance(lower, (int, float))
                    or float(effect) < -5.0
                    or float(lower) < -5.0
                ):
                    reasons.append(f"guard_degraded:{guard_name}")
            elif isinstance(effect, (int, float)):
                if float(effect) < -5.0 or (
                    isinstance(lower, (int, float)) and float(lower) < -5.0
                ):
                    reasons.append(f"guard_degraded:{guard_name}")
            elif not isinstance(absolute, (int, float)) or float(absolute) > 0.0:
                reasons.append(f"guard_degraded:{guard_name}")
        guard_decision = (
            "insufficient_sample"
            if any(reason.startswith("guard_missing:") for reason in reasons)
            else "fail"
            if reasons
            else "pass"
        )
        for metric_name, row in metrics.items():
            if metric_name in CORRECTNESS_GUARD_METRICS:
                row["guard_decision"] = "not_applicable"
                row["guard_reasons"] = []
                continue
            row["guard_decision"] = guard_decision
            row["guard_reasons"] = reasons
            if guard_decision == "fail":
                row["decision"] = "fail"
            elif row["decision"] == "pass" and guard_decision != "pass":
                row["decision"] = guard_decision


def _valid_metric_by_pair(
    rows: Sequence[Mapping[str, object]],
    cell_id: str,
    metric_name: str,
    *,
    value_field: str = "primary_value",
) -> dict[tuple[int, int, int], float]:
    result: dict[tuple[int, int, int], float] = {}
    for row in rows:
        value = row.get(value_field)
        if (
            row.get("cell_id") != cell_id
            or row.get("metric_name") != metric_name
            or row.get("metric_valid") is not True
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            continue
        key = (
            cast(int, row["block_index"]),
            cast(int, row["repetition_index"]),
            cast(int, row["pairing_seed"]),
        )
        if key in result:
            raise ExperimentValidationError("paired Trial key is not unique")
        result[key] = float(value)
    return result


def _validity_rows(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "study_id": row["study_id"],
            "cell_id": row["cell_id"],
            "cell_name": row["cell_name"],
            "block_index": row["block_index"],
            "repetition_index": row["repetition_index"],
            "trial_id": row["trial_id"],
            "trial_attempt_id": row["trial_attempt_id"],
            "metric_name": row["metric_name"],
            "trial_valid": row["trial_valid"],
            "metric_valid": row["metric_valid"],
            "reason_codes": row["reason_codes"],
        }
        for row in sorted(
            rows,
            key=lambda item: (
                cast(str, item["cell_id"]),
                cast(int, item["block_index"]),
                cast(int, item["repetition_index"]),
                cast(str, item["trial_attempt_id"]),
                cast(str, item["metric_name"]),
            ),
        )
    ]


def _budget_for(
    baseline_name: str, candidate_name: str, metric_name: str
) -> tuple[str, float] | None:
    rules = {
        ("noop", "parquet", "throughput_success_per_s"): ("c8_recording_throughput", 5.0),
        ("noop", "parquet", "dct_ms"): ("c8_recording_dct_p95", 5.0),
        ("no_fault_reference", "fault_bookkeeping", "throughput_success_per_s"): ("c12_fast_path_throughput", 2.0),
        ("no_fault_reference", "fault_bookkeeping", "dct_ms"): ("c12_fast_path_dct_p95", 2.0),
        ("no_client", "single_watch", "throughput_success_per_s"): ("c13_single_watch_throughput", 2.0),
        ("no_client", "single_watch", "dct_ms"): ("c13_single_watch_dct_p95", 2.0),
        ("no_client", "eight_read_clients", "throughput_success_per_s"): ("c13_multi_reader_throughput", 5.0),
        ("no_client", "eight_read_clients", "dct_ms"): ("c13_multi_reader_dct_p95", 5.0),
    }
    return rules.get((baseline_name, candidate_name, metric_name))


def _higher_is_better(rows: Sequence[Mapping[str, object]], metric_name: str) -> bool:
    for row in rows:
        if row.get("metric_name") == metric_name:
            return row.get("higher_is_better") is True
    return False


def _metric_unit(rows: Sequence[Mapping[str, object]], metric_name: str) -> str:
    for row in rows:
        if row.get("metric_name") == metric_name and isinstance(row.get("unit"), str):
            return cast(str, row["unit"])
    return "unknown"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ExperimentValidationError(f"{description} must be an object")
    return cast(Mapping[str, object], value)


def _list(value: object, description: str) -> list[object]:
    if not isinstance(value, (tuple, list)):
        raise ExperimentValidationError(f"{description} must be an array")
    return list(value)


def _string_list(value: object, description: str) -> list[str]:
    values = _list(value, description)
    if any(not isinstance(item, str) or not item for item in values):
        raise ExperimentValidationError(f"{description} must contain strings")
    return cast(list[str], values)


def _string(value: Mapping[str, object], key: str, description: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ExperimentValidationError(f"{description} {key} is invalid")
    return result


def _integer(value: Mapping[str, object], key: str, description: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ExperimentValidationError(f"{description} {key} is invalid")
    return result


def _float(value: Mapping[str, object], key: str, description: str) -> float:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ExperimentValidationError(f"{description} {key} is invalid")
    return float(result)


def _boolean(value: Mapping[str, object], key: str, description: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ExperimentValidationError(f"{description} {key} is invalid")
    return result
