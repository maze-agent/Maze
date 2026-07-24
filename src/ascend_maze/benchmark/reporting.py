"""Deterministic machine report and offline-derived presentation artifacts."""

from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
from typing import Mapping, Sequence, cast

from ascend_maze import __version__
from ascend_maze.benchmark.aggregate_artifacts import (
    read_run_metrics,
    read_trial_metrics,
    write_csv,
)
from ascend_maze.benchmark.aggregation import (
    AGGREGATOR_VERSION,
    _load_formal_inputs,
    _load_validity,
    load_study,
    materialize_aggregate_views,
)
from ascend_maze.benchmark.canonical import (
    canonical_json_digest,
    derive_seed,
    stable_payload_id,
)
from ascend_maze.benchmark.metrics import metric_definition
from ascend_maze.benchmark.persistence import (
    atomic_write_bytes,
    atomic_write_json,
    load_json_object,
)
from ascend_maze.benchmark.schema_registry import schema_digests
from ascend_maze.benchmark.statistics import (
    BOOTSTRAP_ALGORITHM,
    budget_decision,
    deterministic_bootstrap_interval,
    summarize_distribution,
)
from ascend_maze.core.errors import ExperimentValidationError


REPORTER_VERSION = "c14_report_v1"


def report_study(study_directory: str | Path) -> dict[str, object]:
    study = load_study(study_directory)
    aggregate_root = study.root / "aggregates"
    _verify_aggregate_manifest(study)
    run_rows = read_run_metrics(aggregate_root / "run_metrics.parquet")
    trial_rows = read_trial_metrics(aggregate_root / "trial_metrics.parquet")
    if any(row.get("study_id") != study.study_id for row in (*run_rows, *trial_rows)):
        raise ExperimentValidationError("aggregate Parquet Study identity is invalid")
    cell_rows, comparison_rows, _ = materialize_aggregate_views(study, trial_rows)
    counts, resources, formal_warnings = _counts_and_resources(study)
    metric_reports = _metric_reports(study, trial_rows, cell_rows)
    comparison_reports = _comparison_reports(comparison_rows)
    validity_reports = _validity_reports(trial_rows)
    spec = study.spec
    workload = _mapping(spec.get("workload"), "Study workload")
    analysis = _mapping(spec.get("analysis"), "Study analysis")
    baselines = [
        _mapping(item, "baseline")
        for item in _list(spec.get("baselines", []), "baselines")
    ]
    unavailable = sorted(
        _string(item, "adapter_id", "baseline") for item in baselines
    )
    invalid_trials = sum(not cast(bool, item["trial_valid"]) for item in validity_reports)
    insufficient = sorted(
        {
            f"{item['cell_id']}:{item['name']}:p99"
            for item in metric_reports
            if item["p99_status"] == "insufficient_sample"
        }
    )
    warnings = list(formal_warnings)
    if invalid_trials:
        warnings.append(f"invalid_trials:{invalid_trials}")
    if spec.get("study_kind") != "formal":
        warnings.append("pilot_study_non_confirmatory")
    warnings.extend(f"p99_insufficient:{item}" for item in insufficient)
    unsupported: list[str] = []
    if spec.get("study_kind") != "formal":
        unsupported.append("formal_performance_conclusion")
    if insufficient:
        unsupported.append("p99_conclusion")
    if unavailable:
        unsupported.append("external_baseline_comparison")
    body: dict[str, object] = {
        "schema_version": 1,
        "study": {
            "study_id": study.study_id,
            "study_name": spec.get("study_name"),
            "study_kind": spec.get("study_kind"),
            "cell_ids": [cell.cell_id for cell in sorted(study.cells, key=lambda item: item.cell_id)],
            "trial_attempt_ids": sorted(trial.trial_attempt_id for trial in study.trials),
        },
        "provenance": {
            "source_commits": [spec.get("build_revision")],
            "config_fingerprints": sorted(
                _string(_mapping(item, "Study Cell"), "config_fingerprint", "Study Cell")
                for item in _list(study.plan.get("cells"), "Study Cells")
            ),
            "environment_fingerprints": [
                workload.get("required_environment_fingerprint")
            ],
            "workflow_fingerprint": workload.get("workflow_fingerprint"),
            "model_artifact_digest": workload.get("model_artifact_digest"),
            "baseline_fingerprints": sorted(
                _string(item, "executable_sha256", "baseline") for item in baselines
            ),
        },
        "experiment": {
            "workload_digest": workload.get("workload_digest"),
            "arrival": spec.get("arrival"),
            "windows": spec.get("windows"),
            "base_seed": spec.get("base_seed"),
            "block_count": spec.get("block_count"),
            "repetition_count": spec.get("repetition_count"),
            "automatic_outlier_removal": analysis.get("automatic_outlier_removal"),
            "mad_sensitivity_separate": True,
        },
        "metrics": metric_reports,
        "comparisons": comparison_reports,
        "validity": validity_reports,
        "counts": counts,
        "resources": resources,
        "known_warnings": sorted(set(warnings)),
        "unavailable_baselines": unavailable,
        "unsupported_conclusions": sorted(set(unsupported)),
        "generator": {
            "name": "ascend-maze",
            "version": __version__,
            "aggregator_version": AGGREGATOR_VERSION,
            "reporter_version": REPORTER_VERSION,
            "schema_digests": dict(schema_digests()),
            "analysis_seed": derive_seed(study.study_id, "report_analysis_v1"),
            "bootstrap_algorithm": BOOTSTRAP_ALGORITHM,
            "bootstrap_samples": analysis.get("bootstrap_samples"),
            "quantile_method": analysis.get("quantile_method"),
            "mad_sensitivity_algorithm": "median_3mad_v1",
        },
    }
    report = {
        **body,
        "content_digest": canonical_json_digest(body),
    }
    report_root = study.root / "report"
    atomic_write_json(report_root / "report.v1.json", report)
    rebuild_report_views(study.root)
    return {
        "schema_version": 1,
        "schema": "ascend-maze.report-result.v1",
        "study_id": study.study_id,
        "content_digest": report["content_digest"],
        "metric_count": len(metric_reports),
        "comparison_count": len(comparison_reports),
        "invalid_trial_count": invalid_trials,
        "known_warnings": report["known_warnings"],
    }


def rebuild_report_views(study_directory: str | Path) -> dict[str, object]:
    study = load_study(study_directory)
    report_root = study.root / "report"
    report = load_json_object(report_root / "report.v1.json", description="machine report")
    body = {key: value for key, value in report.items() if key != "content_digest"}
    if report.get("content_digest") != canonical_json_digest(body):
        raise ExperimentValidationError("machine report digest is invalid")
    trial_rows = read_trial_metrics(study.root / "aggregates" / "trial_metrics.parquet")
    markdown = _render_markdown(report)
    atomic_write_bytes(report_root / "report.md", markdown.encode("utf-8"))
    plot_root = report_root / "plot_data"
    metric_rows = [
        {
            "cell_id": item["cell_id"],
            "cell_name": item["cell_name"],
            "metric_name": item["name"],
            "unit": item["unit"],
            "valid_trial_count": item["valid_trial_count"],
            "sample_count": item["sample_count"],
            "estimate": item["estimate"],
            "ci_lower": _nested(item.get("confidence_interval"), "lower"),
            "ci_upper": _nested(item.get("confidence_interval"), "upper"),
            "p99": _nested(item.get("distribution"), "p99"),
            "p99_status": item["p99_status"],
        }
        for item in cast(list[Mapping[str, object]], report["metrics"])
    ]
    write_csv(
        plot_root / "metrics.csv",
        metric_rows,
        (
            "cell_id",
            "cell_name",
            "metric_name",
            "unit",
            "valid_trial_count",
            "sample_count",
            "estimate",
            "ci_lower",
            "ci_upper",
            "p99",
            "p99_status",
        ),
    )
    comparison_rows = [dict(item) for item in cast(list[Mapping[str, object]], report["comparisons"])]
    flattened_comparisons = [
        {
            "comparison_id": item["comparison_id"],
            "baseline_cell_id": item["baseline_cell_id"],
            "candidate_cell_id": item["candidate_cell_id"],
            "metric_name": item["metric_name"],
            "paired_blocks": item["paired_blocks"],
            "effect_pct": item["effect_pct"],
            "ci95_lower": _nested(item.get("confidence_interval"), "lower"),
            "ci95_upper": _nested(item.get("confidence_interval"), "upper"),
            "decision": item["decision"],
        }
        for item in comparison_rows
    ]
    write_csv(
        plot_root / "comparisons.csv",
        flattened_comparisons,
        (
            "comparison_id",
            "baseline_cell_id",
            "candidate_cell_id",
            "metric_name",
            "paired_blocks",
            "effect_pct",
            "ci95_lower",
            "ci95_upper",
            "decision",
        ),
    )
    validity_rows = [
        {
            "trial_attempt_id": row["trial_attempt_id"],
            "cell_id": row["cell_id"],
            "metric_name": row["metric_name"],
            "trial_valid": row["trial_valid"],
            "metric_valid": row["metric_valid"],
            "reason_codes": row["reason_codes"],
        }
        for row in trial_rows
    ]
    write_csv(
        plot_root / "validity.csv",
        validity_rows,
        (
            "trial_attempt_id",
            "cell_id",
            "metric_name",
            "trial_valid",
            "metric_valid",
            "reason_codes",
        ),
    )
    atomic_write_json(
        plot_root / "manifest.json",
        {
            "schema_version": 1,
            "report_digest": report["content_digest"],
            "sources": {
                "trial_metrics": canonical_json_digest(trial_rows),
            },
            "files": ["comparisons.csv", "metrics.csv", "validity.csv"],
        },
    )
    return {
        "report_digest": report["content_digest"],
        "plot_file_count": 4,
    }


def _metric_reports(
    study: object,
    trial_rows: Sequence[Mapping[str, object]],
    cell_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    study_id = cast(str, getattr(study, "study_id"))
    bootstrap_samples = cast(int, getattr(study, "bootstrap_samples"))
    confidence_level = cast(float, getattr(study, "confidence_level"))
    result: list[dict[str, object]] = []
    for cell in cell_rows:
        cell_id = cast(str, cell["cell_id"])
        metric_name = cast(str, cell["metric_name"])
        valid_rows = [
            row
            for row in trial_rows
            if row.get("cell_id") == cell_id
            and row.get("metric_name") == metric_name
            and row.get("metric_valid") is True
            and isinstance(row.get("primary_value"), (int, float))
            and not isinstance(row.get("primary_value"), bool)
        ]
        values = [float(cast(float, row["primary_value"])) for row in valid_rows]
        ci = (
            None
            if not values
            else deterministic_bootstrap_interval(
                values,
                seed=derive_seed(study_id, "cell_metric", cell_id, metric_name),
                samples=bootstrap_samples,
                confidence_level=confidence_level,
            ).canonical_payload()
        )
        reasons = Counter(
            reason
            for row in trial_rows
            if row.get("cell_id") == cell_id and row.get("metric_name") == metric_name
            for reason in cast(list[str], row.get("reason_codes", []))
        )
        budget = _absolute_budget(study, metric_name, valid_rows, cell)
        definition = metric_definition(metric_name)
        result.append(
            {
                "measurement_id": stable_payload_id(
                    "measurement",
                    {
                        "study_id": study_id,
                        "cell_id": cell_id,
                        "metric_name": metric_name,
                        "metric_schema_version": 1,
                    },
                ),
                "cell_id": cell_id,
                "cell_name": cell["cell_name"],
                "name": metric_name,
                "definition": "Unknown metric" if definition is None else definition.description,
                "unit": cell["unit"],
                "valid_trial_count": cell["valid_trial_count"],
                "invalid_trial_count": cell["invalid_trial_count"],
                "sample_count": cell["sample_count"],
                "estimate": cell["median"],
                "confidence_interval": ci,
                "distribution": {
                    "median": cell["median"],
                    "mean": cell["mean"],
                    "standard_deviation": cell["standard_deviation"],
                    "mad": cell["mad"],
                    "minimum": cell["minimum"],
                    "maximum": cell["maximum"],
                    "p50": cell["p50"],
                    "p95": cell["p95"],
                    "p99": cell["p99"],
                },
                "p99_status": cell["p99_status"],
                "invalid_reason_counts": dict(sorted(reasons.items())),
                "mad_sensitivity": _mad_sensitivity(values),
                "budget": budget,
            }
        )
    return result


def _verify_aggregate_manifest(study: object) -> None:
    root = cast(Path, getattr(study, "root")) / "aggregates"
    manifest = load_json_object(root / "manifest.json", description="aggregate manifest")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("schema") != "ascend-maze.aggregate-manifest.v1"
        or manifest.get("study_id") != getattr(study, "study_id")
        or manifest.get("aggregator_version") != AGGREGATOR_VERSION
    ):
        raise ExperimentValidationError("aggregate manifest identity is invalid")
    digests = _mapping(manifest.get("output_digests"), "aggregate output digests")
    expected_names = {
        "cell_metrics.csv",
        "comparisons.csv",
        "run_metrics.parquet",
        "trial_metrics.parquet",
        "validity.csv",
    }
    if set(digests) != expected_names:
        raise ExperimentValidationError("aggregate manifest outputs are incomplete")
    source_digests = _mapping(
        manifest.get("source_validity_digests"), "aggregate source validity digests"
    )
    trials = cast(Sequence[object], getattr(study, "trials"))
    if set(source_digests) != {
        cast(str, getattr(trial, "trial_attempt_id")) for trial in trials
    }:
        raise ExperimentValidationError("aggregate manifest sources are incomplete")
    for trial in trials:
        trial_attempt_id = cast(str, getattr(trial, "trial_attempt_id"))
        validity = _load_validity(trial)  # type: ignore[arg-type]
        if source_digests[trial_attempt_id] != validity.get("validation_digest"):
            raise ExperimentValidationError("Trial validity changed after aggregation")
    for name in sorted(expected_names):
        try:
            observed = hashlib.sha256((root / name).read_bytes()).hexdigest()
        except OSError as exc:
            raise ExperimentValidationError(
                f"aggregate output is unavailable: {name}"
            ) from exc
        if digests[name] != observed:
            raise ExperimentValidationError(f"aggregate output digest changed: {name}")


def _absolute_budget(
    study: object,
    metric_name: str,
    valid_rows: Sequence[Mapping[str, object]],
    cell: Mapping[str, object],
) -> dict[str, object] | None:
    limit: float | None = None
    budget_name: str | None = None
    if metric_name == "scheduler_policy_select_ms":
        budget_name, limit = "c7_policy_select_p99", 5.0
    elif metric_name == "scheduler_total_ms":
        budget_name, limit = "c7_score_placement_p99", 10.0
    elif metric_name == "arrival_lateness_ms":
        spec = cast(Mapping[str, object], getattr(study, "spec"))
        arrival = _mapping(spec.get("arrival"), "arrival")
        rate = arrival.get("rate_per_second")
        mean_interval = 0.0 if not isinstance(rate, (int, float)) else 1_000.0 / float(rate)
        budget_name, limit = "c14_arrival_lateness_p99", max(10.0, mean_interval * 0.05)
    if budget_name is None or limit is None:
        return None
    p99_values = [
        float(cast(float, row["p99"]))
        for row in valid_rows
        if row.get("p99_status") == "sufficient"
        and isinstance(row.get("p99"), (int, float))
        and not isinstance(row.get("p99"), bool)
    ]
    upper = (
        None
        if not p99_values
        else deterministic_bootstrap_interval(
            p99_values,
            seed=derive_seed(
                cast(str, getattr(study, "study_id")),
                "absolute_budget",
                cell["cell_id"],
                metric_name,
            ),
            samples=cast(int, getattr(study, "bootstrap_samples")),
            confidence_level=cast(float, getattr(study, "confidence_level")),
            one_sided_upper=True,
        ).upper
    )
    point = cast(float | None, cell.get("p99"))
    return {
        "name": budget_name,
        "limit": limit,
        "point_estimate": point,
        "upper_95": upper,
        "decision": budget_decision(
            point_estimate=point,
            upper_bound=upper,
            limit=limit,
        ),
    }


def _mad_sensitivity(values: Sequence[float]) -> dict[str, object]:
    summary = summarize_distribution(values)
    if summary.sample_count == 0:
        return {
            "algorithm": "median_3mad_v1",
            "status": "insufficient_sample",
            "threshold_multiplier": 3.0,
            "flagged_count": 0,
            "sensitivity_estimate": None,
            "primary_analysis_unchanged": True,
        }
    if summary.mad is None or summary.mad == 0 or summary.median is None:
        return {
            "algorithm": "median_3mad_v1",
            "status": "zero_mad",
            "threshold_multiplier": 3.0,
            "flagged_count": 0,
            "sensitivity_estimate": summary.median,
            "primary_analysis_unchanged": True,
        }
    threshold = 3.0 * summary.mad
    retained = [
        value for value in values if abs(value - summary.median) <= threshold
    ]
    return {
        "algorithm": "median_3mad_v1",
        "status": "computed",
        "threshold_multiplier": 3.0,
        "flagged_count": len(values) - len(retained),
        "sensitivity_estimate": summarize_distribution(retained).median,
        "primary_analysis_unchanged": True,
    }


def _comparison_reports(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "comparison_id": row["comparison_id"],
            "baseline_cell_id": row["baseline_cell_id"],
            "baseline_cell_name": row["baseline_cell_name"],
            "candidate_cell_id": row["candidate_cell_id"],
            "candidate_cell_name": row["candidate_cell_name"],
            "metric_name": row["metric_name"],
            "unit": row["unit"],
            "direction": row["direction"],
            "paired_blocks": row["paired_blocks"],
            "effect": row["absolute_effect"],
            "effect_pct": row["relative_effect_pct"],
            "confidence_interval": _interval(row, "ci95_lower", "ci95_upper", 0.95),
            "familywise_confidence_interval": _interval(
                row, "familywise_ci_lower", "familywise_ci_upper", 0.9875
            ),
            "budget": None
            if row["budget_name"] is None
            else {
                "name": row["budget_name"],
                "limit_pct": row["budget_limit_pct"],
                "upper_95": row["budget_upper95"],
            },
            "guard_decision": row["guard_decision"],
            "guard_reasons": row["guard_reasons"],
            "decision": row["decision"],
        }
        for row in rows
    ]


def _validity_reports(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        grouped.setdefault(cast(str, row["trial_attempt_id"]), []).append(row)
    result: list[dict[str, object]] = []
    for trial_attempt_id, trial_rows in sorted(grouped.items()):
        first = trial_rows[0]
        reason_codes = sorted(
            {
                reason
                for row in trial_rows
                for reason in cast(list[str], row.get("reason_codes", []))
            }
        )
        result.append(
            {
                "trial_attempt_id": trial_attempt_id,
                "trial_id": first["trial_id"],
                "cell_id": first["cell_id"],
                "block_index": first["block_index"],
                "repetition_index": first["repetition_index"],
                "trial_valid": first["trial_valid"],
                "metric_valid": {
                    cast(str, row["metric_name"]): row["metric_valid"]
                    for row in sorted(trial_rows, key=lambda item: cast(str, item["metric_name"]))
                },
                "metric_reasons": {
                    cast(str, row["metric_name"]): row["reason_codes"]
                    for row in sorted(trial_rows, key=lambda item: cast(str, item["metric_name"]))
                },
                "reasons": reason_codes,
            }
        )
    return result


def _counts_and_resources(study: object) -> tuple[dict[str, int], dict[str, object], list[str]]:
    counts = {
        "offered": 0,
        "issued": 0,
        "committed": 0,
        "admitted": 0,
        "terminal": 0,
        "succeeded": 0,
        "failed": 0,
        "timed_out": 0,
        "backlog": 0,
    }
    before: dict[str, object] = {}
    after: dict[str, object] = {}
    recovered = True
    warnings: list[str] = []
    for trial in cast(Sequence[object], getattr(study, "trials")):
        try:
            validity = _load_validity(trial)  # type: ignore[arg-type]
            formal = _load_formal_inputs(trial, validity)  # type: ignore[arg-type]
        except ExperimentValidationError:
            warnings.append(
                f"formal_inputs_unavailable:{getattr(trial, 'trial_attempt_id')}"
            )
            recovered = False
            continue
        for run in formal.run_manifest.runs:
            if run.phase != "measurement":
                continue
            counts["offered"] += run.offered_at_monotonic_ms is not None
            counts["issued"] += run.issued_at_monotonic_ms is not None
            counts["committed"] += run.run_id is not None
            counts["admitted"] += run.admitted_at_monotonic_ms is not None
            counts["terminal"] += run.terminal_status is not None
            counts["succeeded"] += run.terminal_status == "succeeded"
            counts["timed_out"] += run.terminal_status == "timed_out"
            counts["failed"] += run.terminal_status in {
                "failed",
                "cancelled",
                "interrupted",
            }
        trial_id = cast(str, getattr(trial, "trial_attempt_id"))
        before[trial_id] = {
            "snapshot_digest": formal.resource_before.get("snapshot_digest"),
            "controller_generation": formal.resource_before.get("controller_generation"),
            "config_fingerprint": formal.resource_before.get("config_fingerprint"),
        }
        recovery = formal.resource_after.get("recovery")
        trial_recovered = isinstance(recovery, Mapping) and recovery.get("recovered") is True
        recovered = recovered and trial_recovered
        after[trial_id] = {
            "snapshot_digest": formal.resource_after.get("snapshot_digest"),
            "controller_generation": formal.resource_after.get("controller_generation"),
            "config_fingerprint": formal.resource_after.get("config_fingerprint"),
            "recovered": trial_recovered,
            "reason_code": None if not isinstance(recovery, Mapping) else recovery.get("reason_code"),
        }
    counts["backlog"] = counts["committed"] - counts["terminal"]
    return counts, {"before": before, "after": after, "recovered": recovered}, warnings


def _render_markdown(report: Mapping[str, object]) -> str:
    study = _mapping(report.get("study"), "report Study")
    counts = _mapping(report.get("counts"), "report counts")
    lines = [
        f"# Ascend-Maze Study {study.get('study_id')}",
        "",
        f"Report digest: `{report.get('content_digest')}`",
        "",
        "## Cohort",
        "",
        "| Offered | Issued | Committed | Admitted | Terminal | Succeeded | Failed | Timed out | Backlog |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {counts.get('offered')} | {counts.get('issued')} | {counts.get('committed')} | {counts.get('admitted')} | {counts.get('terminal')} | {counts.get('succeeded')} | {counts.get('failed')} | {counts.get('timed_out')} | {counts.get('backlog')} |",
        "",
        "## Metrics",
        "",
        "| Cell | Metric | Estimate | Unit | Valid Trials | P99 status |",
        "|---|---|---:|---|---:|---|",
    ]
    for item in cast(list[Mapping[str, object]], report.get("metrics", [])):
        lines.append(
            f"| {item.get('cell_name')} | {item.get('name')} | {_display(item.get('estimate'))} | {item.get('unit')} | {item.get('valid_trial_count')} | {item.get('p99_status')} |"
        )
    lines.extend(
        (
            "",
            "## Comparisons",
            "",
            "| Baseline | Candidate | Metric | Effect % | Paired blocks | Decision |",
            "|---|---|---|---:|---:|---|",
        )
    )
    for item in cast(list[Mapping[str, object]], report.get("comparisons", [])):
        lines.append(
            f"| {item.get('baseline_cell_name')} | {item.get('candidate_cell_name')} | {item.get('metric_name')} | {_display(item.get('effect_pct'))} | {item.get('paired_blocks')} | {item.get('decision')} |"
        )
    warnings = cast(list[str], report.get("known_warnings", []))
    lines.extend(("", "## Warnings", ""))
    lines.extend(["None."] if not warnings else [f"- `{warning}`" for warning in warnings])
    lines.extend(
        (
            "",
            "## Analysis Contract",
            "",
            "Hyndman-Fan type 7 quantiles; deterministic paired bootstrap; no automatic outlier removal or winsorization. MAD is reported only as sensitivity information.",
            "",
        )
    )
    return "\n".join(lines)


def _interval(
    row: Mapping[str, object], lower_name: str, upper_name: str, level: float
) -> dict[str, object] | None:
    lower = row.get(lower_name)
    upper = row.get(upper_name)
    if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)):
        return None
    return {
        "confidence_level": level,
        "lower": lower,
        "upper": upper,
        "sidedness": "two_sided",
    }


def _nested(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, Mapping) else None


def _display(value: object) -> str:
    return "missing" if value is None else format(value, ".6g") if isinstance(value, float) else str(value)


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ExperimentValidationError(f"{description} must be an object")
    return cast(Mapping[str, object], value)


def _list(value: object, description: str) -> list[object]:
    if not isinstance(value, (tuple, list)):
        raise ExperimentValidationError(f"{description} must be an array")
    return list(value)


def _string(value: Mapping[str, object], key: str, description: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ExperimentValidationError(f"{description} {key} is invalid")
    return result
