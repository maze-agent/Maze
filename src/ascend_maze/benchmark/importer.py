"""Offline C14 Trial import and validity orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import quote

from ascend_maze.benchmark.canonical import canonical_json_digest, thaw
from ascend_maze.benchmark.analysis_inputs import (
    AnalysisInputRecord,
    ValidatedAnalysisInputs,
    validate_analysis_inputs,
)
from ascend_maze.benchmark.contracts import (
    STUDY_PLAN_SCHEMA,
    TRIAL_MANIFEST_SCHEMA,
    TrialManifest,
)
from ascend_maze.benchmark.indexes import (
    AssociationIndexes,
    build_indexes,
    metric_validity,
)
from ascend_maze.benchmark.metrics import CORRECTNESS_GUARD_METRICS
from ascend_maze.benchmark.parquet_import import (
    ParquetImportFailure,
    import_committed_parquet,
    observe_regular_file,
)
from ascend_maze.benchmark.persistence import (
    atomic_write_json,
    load_json_object,
)
from ascend_maze.benchmark.privacy import privacy_violations
from ascend_maze.benchmark.statistics import type7_quantile
from ascend_maze.benchmark.runtime import RunFlushResult
from ascend_maze.benchmark.validity import (
    RAW_FILES_SCHEMA,
    STUDY_VALIDATION_SCHEMA,
    IgnoredFileRecord,
    MetricValidity,
    RawFileRecord,
    ValidityIssue,
    raw_files_payload,
    stable_issues,
    trial_validation_payload,
)
from ascend_maze.contracts.recording import ExecutionEvent, RunRecordingContext
from ascend_maze.core.errors import ExperimentValidationError


@dataclass(frozen=True, slots=True)
class _StudyIdentity:
    study_id: str
    workflow_fingerprint: str
    environment_fingerprint: str
    build_revision: str
    metrics: tuple[str, ...]
    config_by_cell: Mapping[str, str]
    trial_cell: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _TrialEntry:
    trial_id: str
    trial_attempt_id: str
    state: str
    directory: Path


@dataclass(slots=True)
class _TrialImport:
    entry: _TrialEntry
    manifest: TrialManifest
    issues: list[ValidityIssue] = field(default_factory=list)
    events: list[ExecutionEvent] = field(default_factory=list)
    contexts: list[RunRecordingContext] = field(default_factory=list)
    files: list[RawFileRecord] = field(default_factory=list)
    ignored: list[IgnoredFileRecord] = field(default_factory=list)
    file_owners: dict[str, str] = field(default_factory=dict)
    indexes: AssociationIndexes | None = None
    raw_payload: dict[str, object] | None = None
    analysis_inputs: tuple[AnalysisInputRecord, ...] = ()
    analysis_inputs_digest: str = ""
    formal_inputs_valid: bool = False
    formal_inputs: ValidatedAnalysisInputs | None = None


def validate_study(study_directory: str | Path) -> dict[str, object]:
    root = _study_root(study_directory)
    plan = load_json_object(root / "study_plan.json", description="Study plan")
    manifest = load_json_object(
        root / "study_manifest.json", description="Study manifest"
    )
    identity = _parse_study_identity(plan, manifest)
    entries = _parse_trial_entries(root, plan, manifest)
    imports = [_import_trial(entry, identity) for entry in entries]
    _add_cross_trial_integrity(imports)

    summaries: list[dict[str, object]] = []
    for imported in imports:
        summaries.append(_commit_trial_validation(imported, identity))
    valid_count = sum(bool(item["trial_valid"]) for item in summaries)
    body = {
        "study_id": identity.study_id,
        "study_valid": bool(summaries)
        and len(summaries) == len(entries)
        and valid_count == len(summaries),
        "trial_count": len(summaries),
        "valid_trial_count": valid_count,
        "invalid_trial_count": len(summaries) - valid_count,
        "trials": summaries,
    }
    payload = {
        "schema_version": 1,
        "schema": STUDY_VALIDATION_SCHEMA,
        **body,
        "validation_digest": canonical_json_digest(body),
    }
    atomic_write_json(root / "validation_summary.json", payload)
    return payload


def _study_root(value: str | Path) -> Path:
    try:
        root = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ExperimentValidationError(
            f"Study directory is unavailable: {value}"
        ) from exc
    if not root.is_dir():
        raise ExperimentValidationError("Study path is not a directory")
    return root


def _parse_study_identity(
    plan: Mapping[str, object], manifest: Mapping[str, object]
) -> _StudyIdentity:
    if plan.get("schema_version") != 1 or plan.get("schema") != STUDY_PLAN_SCHEMA:
        raise ExperimentValidationError("Study plan schema is invalid")
    study_id = _string(plan, "study_id", "Study plan")
    if manifest.get("schema_version") != 1 or manifest.get("study_id") != study_id:
        raise ExperimentValidationError("Study manifest identity is invalid")
    plan_digest = canonical_json_digest(plan)
    if manifest.get("plan_sha256") != plan_digest:
        raise ExperimentValidationError("Study plan digest does not match manifest")
    spec = _mapping(plan.get("spec"), "Study plan spec")
    if plan.get("canonical_spec_sha256") != canonical_json_digest(spec):
        raise ExperimentValidationError("Study canonical spec digest is invalid")
    workload = _mapping(spec.get("workload"), "Study workload")
    analysis = _mapping(spec.get("analysis"), "Study analysis")
    raw_metrics = _list(analysis.get("metric_set"), "Study metric set")
    requested_metrics = tuple(
        sorted(_string_value(item, "metric name") for item in raw_metrics)
    )
    if not requested_metrics or len(requested_metrics) != len(set(requested_metrics)):
        raise ExperimentValidationError("Study metric set is invalid")
    metrics = tuple(
        sorted(
            {
                *requested_metrics,
                *CORRECTNESS_GUARD_METRICS,
            }
        )
    )
    raw_cells = _list(plan.get("cells"), "Study cells")
    config_by_cell: dict[str, str] = {}
    for item in raw_cells:
        cell = _mapping(item, "Study cell")
        cell_id = _string(cell, "cell_id", "Study cell")
        config = _string(cell, "config_fingerprint", "Study cell")
        if cell_id in config_by_cell:
            raise ExperimentValidationError("Study has duplicate Cell identities")
        config_by_cell[cell_id] = config
    raw_trials = _list(plan.get("trials"), "Study trials")
    trial_cell: dict[str, str] = {}
    for item in raw_trials:
        trial = _mapping(item, "Study Trial")
        trial_id = _string(trial, "trial_id", "Study Trial")
        cell_id = _string(trial, "cell_id", "Study Trial")
        if trial_id in trial_cell or cell_id not in config_by_cell:
            raise ExperimentValidationError("Study Trial identity is invalid")
        trial_cell[trial_id] = cell_id
    return _StudyIdentity(
        study_id=study_id,
        workflow_fingerprint=_string(
            workload, "workflow_fingerprint", "Study workload"
        ),
        environment_fingerprint=_string(
            workload, "required_environment_fingerprint", "Study workload"
        ),
        build_revision=_string(spec, "build_revision", "Study spec"),
        metrics=metrics,
        config_by_cell=config_by_cell,
        trial_cell=trial_cell,
    )


def _parse_trial_entries(
    root: Path,
    plan: Mapping[str, object],
    manifest: Mapping[str, object],
) -> tuple[_TrialEntry, ...]:
    raw = _list(manifest.get("trials"), "Study manifest Trials")
    planned = {
        _string(_mapping(item, "Study Trial"), "trial_id", "Study Trial")
        for item in _list(plan.get("trials"), "Study trials")
    }
    entries: list[_TrialEntry] = []
    seen: set[str] = set()
    for item in raw:
        entry = _mapping(item, "Study manifest Trial")
        trial_id = _string(entry, "trial_id", "Study manifest Trial")
        if trial_id not in planned or trial_id in seen:
            raise ExperimentValidationError("Study manifest Trial identity is invalid")
        seen.add(trial_id)
        relative = _string(entry, "relative_directory", "Study manifest Trial")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ExperimentValidationError("Study manifest Trial path is invalid")
        directory = (root / relative_path).resolve(strict=False)
        try:
            directory.relative_to(root)
        except ValueError as exc:
            raise ExperimentValidationError(
                "Study manifest Trial path escapes the Study"
            ) from exc
        entries.append(
            _TrialEntry(
                trial_id=trial_id,
                trial_attempt_id=_string(
                    entry, "trial_attempt_id", "Study manifest Trial"
                ),
                state=_string(entry, "state", "Study manifest Trial"),
                directory=directory,
            )
        )
    if manifest.get("state") == "completed" and seen != planned:
        raise ExperimentValidationError("completed Study does not index every Trial")
    return tuple(entries)


def _import_trial(entry: _TrialEntry, identity: _StudyIdentity) -> _TrialImport:
    manifest = _load_trial_manifest(entry.directory / "trial_manifest.json")
    imported = _TrialImport(entry=entry, manifest=manifest)
    try:
        formal = validate_analysis_inputs(
            entry.directory,
            trial_attempt_id=manifest.trial_attempt_id,
            committed_run_ids=manifest.run_ids,
        )
    except ExperimentValidationError as exc:
        reason = (
            "analysis_input_missing"
            if "unavailable" in str(exc)
            else "analysis_input_invalid"
        )
        imported.issues.append(ValidityIssue(reason, source=str(exc)))
    else:
        imported.analysis_inputs = formal.records
        imported.analysis_inputs_digest = formal.digest
        imported.formal_inputs_valid = True
        imported.formal_inputs = formal
    if (
        manifest.trial_id != entry.trial_id
        or manifest.trial_attempt_id != entry.trial_attempt_id
        or manifest.state != entry.state
    ):
        imported.issues.append(ValidityIssue("trial_manifest_identity_mismatch"))
    if manifest.state != "valid":
        imported.issues.append(ValidityIssue("trial_manifest_state_invalid"))
    if len(manifest.run_ids) != len(manifest.experiment_ids) or any(
        run_id != experiment_id
        for run_id, experiment_id in zip(
            manifest.run_ids, manifest.experiment_ids, strict=True
        )
    ):
        imported.issues.append(ValidityIssue("trial_manifest_identity_mismatch"))

    flush_results, flush_issues = _load_flush_results(
        entry.directory / "flush_results.json", manifest
    )
    imported.issues.extend(flush_issues)
    _validate_flush_health(flush_results, imported.issues)
    listed = set(manifest.committed_files)
    flushed = {path for result in flush_results for path in result.committed_files}
    if listed != flushed:
        imported.issues.append(ValidityIssue("committed_file_manifest_mismatch"))
    owners: dict[str, str] = {}
    for result in flush_results:
        for source in result.committed_files:
            previous_owner = owners.get(source)
            if previous_owner is not None and previous_owner != result.run_id:
                imported.issues.append(
                    ValidityIssue(
                        "flush_result_conflict", run_id=result.run_id, source=source
                    )
                )
            owners[source] = result.run_id
    imported.file_owners = owners

    authorized = tuple(sorted(listed.intersection(flushed)))
    baseline = _load_raw_baseline(entry.directory / "raw_files.json")
    if (
        baseline is not None
        and baseline.get("trial_attempt_id") != manifest.trial_attempt_id
    ):
        raise ExperimentValidationError("raw files manifest Trial identity changed")
    baseline_files = _raw_file_map(baseline)
    if baseline is not None and set(
        _string_tuple(baseline.get("committed_paths"), "raw committed paths")
    ) != set(authorized):
        imported.issues.append(ValidityIssue("committed_file_hash_changed"))
    for index, source in enumerate(authorized):
        run_id = owners[source]
        path = Path(source).expanduser()
        logical_name = f"c8_{index:06d}"
        try:
            observation = observe_regular_file(path)
        except ParquetImportFailure as exc:
            imported.issues.append(
                ValidityIssue(exc.reason_code, run_id=run_id, source=source)
            )
            continue
        baseline_record = baseline_files.get(source)
        if baseline_record is not None and (
            baseline_record.get("size_bytes") != observation.size_bytes
            or baseline_record.get("sha256") != observation.sha256
        ):
            imported.issues.append(
                ValidityIssue(
                    "committed_file_hash_changed", run_id=run_id, source=source
                )
            )
            continue
        try:
            content = import_committed_parquet(path)
        except ParquetImportFailure as exc:
            imported.files.append(
                RawFileRecord(
                    logical_name,
                    run_id,
                    observation.source_path,
                    observation.size_bytes,
                    observation.sha256,
                    "invalid",
                    0,
                )
            )
            imported.issues.append(
                ValidityIssue(exc.reason_code, run_id=run_id, source=source)
            )
            continue
        if observation != content.observation or (
            baseline_record is not None
            and (
                baseline_record.get("size_bytes") != content.observation.size_bytes
                or baseline_record.get("sha256") != content.observation.sha256
            )
        ):
            imported.issues.append(
                ValidityIssue(
                    "committed_file_hash_changed", run_id=run_id, source=source
                )
            )
            continue
        imported.files.append(
            RawFileRecord(
                logical_name,
                run_id,
                content.observation.source_path,
                content.observation.size_bytes,
                content.observation.sha256,
                content.kind,
                content.row_count,
            )
        )
        for context in content.contexts:
            if context.run_id != run_id:
                imported.issues.append(
                    ValidityIssue(
                        "run_identity_mismatch",
                        run_id=context.run_id,
                        source=source,
                    )
                )
            else:
                imported.contexts.append(context)
        for event in content.events:
            if event.run_id != run_id:
                imported.issues.append(
                    ValidityIssue(
                        "run_identity_mismatch",
                        run_id=event.run_id,
                        subject=event.event_id,
                        source=source,
                    )
                )
            else:
                imported.events.append(event)
                violations = privacy_violations(event.payload)
                if violations:
                    imported.issues.append(
                        ValidityIssue(
                            "privacy_violation",
                            run_id=run_id,
                            subject=event.event_id,
                            source=logical_name,
                        )
                    )

    _find_unlisted_files(imported, authorized)
    expected = _validate_contexts(imported, identity)
    index_result = build_indexes(
        run_ids=manifest.run_ids,
        events=imported.events,
        expected_producers=expected,
    )
    imported.indexes = index_result.indexes
    imported.issues.extend(index_result.issues)
    current_raw = raw_files_payload(
        trial_attempt_id=manifest.trial_attempt_id,
        committed_paths=authorized,
        files=imported.files,
        ignored_files=imported.ignored,
    )
    imported.raw_payload = baseline if baseline is not None else current_raw
    return imported


def _validate_flush_health(
    results: tuple[RunFlushResult, ...], issues: list[ValidityIssue]
) -> None:
    for result in results:
        payload = _mapping(thaw(result.payload), "FlushResult payload")
        expected_fields = {
            "run_id",
            "committed_files",
            "dropped_control_event_count",
            "dropped_telemetry_count",
            "sequence_gap_count",
            "missing_producer_count",
            "writer_errors",
            "recording_complete",
            "flush_duration_ms",
        }
        if set(payload) != expected_fields:
            issues.append(ValidityIssue("flush_result_conflict", run_id=result.run_id))
        if not result.recording_complete:
            issues.append(ValidityIssue("recording_incomplete", run_id=result.run_id))
        checks = (
            ("dropped_control_event_count", "dropped_control_events"),
            ("dropped_telemetry_count", "dropped_telemetry_events"),
            ("sequence_gap_count", "sequence_gap_reported"),
            ("missing_producer_count", "missing_producer_reported"),
        )
        for field_name, reason_code in checks:
            value = payload.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                issues.append(
                    ValidityIssue("flush_result_conflict", run_id=result.run_id)
                )
            elif value:
                issues.append(ValidityIssue(reason_code, run_id=result.run_id))
        errors = payload.get("writer_errors")
        if not isinstance(errors, (tuple, list)) or any(
            not isinstance(item, str) or not item for item in errors
        ):
            issues.append(ValidityIssue("flush_result_conflict", run_id=result.run_id))
        elif errors:
            issues.append(ValidityIssue("writer_error", run_id=result.run_id))
        duration = payload.get("flush_duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            issues.append(ValidityIssue("flush_result_conflict", run_id=result.run_id))
        expected = {
            "run_id": result.run_id,
            "committed_files": tuple(result.committed_files),
            "recording_complete": result.recording_complete,
        }
        payload_files = payload.get("committed_files")
        files_match = (
            isinstance(payload_files, (tuple, list))
            and tuple(payload_files) == expected["committed_files"]
        )
        if (
            payload.get("run_id") != expected["run_id"]
            or payload.get("recording_complete") != expected["recording_complete"]
            or not files_match
        ):
            issues.append(ValidityIssue("flush_result_conflict", run_id=result.run_id))


def _validate_contexts(
    imported: _TrialImport, identity: _StudyIdentity
) -> dict[str, frozenset[str]]:
    cell_id = identity.trial_cell.get(imported.manifest.trial_id)
    expected_config = None if cell_id is None else identity.config_by_cell.get(cell_id)
    expected: dict[str, set[str]] = {
        run_id: set() for run_id in imported.manifest.run_ids
    }
    seen_contexts: set[str] = set()
    context_identities: dict[str, tuple[object, ...]] = {}
    for context in imported.contexts:
        if context.run_id not in expected:
            imported.issues.append(
                ValidityIssue("run_reference_dangling", run_id=context.run_id)
            )
            continue
        seen_contexts.add(context.run_id)
        expected[context.run_id].update(context.initial_expected_producer_ids)
        context_identity = (
            context.experiment_id,
            context.workflow_fingerprint,
            context.config_fingerprint,
            context.environment_fingerprint,
            context.build_revision,
            context.started_wall_time_ms,
        )
        previous_identity = context_identities.setdefault(
            context.run_id, context_identity
        )
        if (
            context.experiment_id != context.run_id
            or context.workflow_fingerprint != identity.workflow_fingerprint
            or context.config_fingerprint != expected_config
            or context.environment_fingerprint != identity.environment_fingerprint
            or context.build_revision != identity.build_revision
            or not context.initial_expected_producer_ids
            or previous_identity != context_identity
        ):
            imported.issues.append(
                ValidityIssue("context_identity_mismatch", run_id=context.run_id)
            )
    for run_id in imported.manifest.run_ids:
        if run_id not in seen_contexts:
            imported.issues.append(ValidityIssue("context_missing", run_id=run_id))
    return {run_id: frozenset(values) for run_id, values in expected.items()}


def _find_unlisted_files(imported: _TrialImport, authorized: tuple[str, ...]) -> None:
    authorized_paths = {str(Path(item)) for item in authorized}
    roots: set[tuple[Path, str]] = set()
    for source in authorized:
        run_id = imported.file_owners.get(source)
        if run_id is None:
            continue
        path = Path(source)
        encoded = quote(run_id, safe="")
        experiment_root = next(
            (parent for parent in path.parents if parent.name == encoded), None
        )
        if experiment_root is not None:
            roots.add((experiment_root, encoded))
    # The owner lookup above intentionally has no access to non-manifest directories.
    for root, encoded_run in sorted(roots, key=lambda item: str(item[0])):
        try:
            candidates = tuple(root.rglob(f"*{encoded_run}*"))
        except OSError:
            continue
        for candidate in candidates:
            source = str(candidate)
            if source in authorized_paths or candidate.name.endswith(".flush.json"):
                continue
            name = candidate.name.lower()
            if not (
                name.endswith(".parquet")
                or name.endswith(".tmp")
                or (name.startswith(".") and ".tmp" in name)
            ):
                continue
            imported.ignored.append(IgnoredFileRecord(source, "unlisted_trial_file"))
            imported.issues.append(ValidityIssue("unlisted_trial_file", source=source))


def _add_cross_trial_integrity(imports: list[_TrialImport]) -> None:
    event_owners: dict[str, _TrialImport] = {}
    producer_sequences: dict[str, list[tuple[int, int, _TrialImport]]] = {}
    for imported in imports:
        for event in imported.events:
            previous = event_owners.get(event.event_id)
            if previous is not None and previous is not imported:
                previous.issues.append(
                    ValidityIssue(
                        "event_id_duplicate",
                        run_id=event.run_id,
                        subject=event.event_id,
                    )
                )
                imported.issues.append(
                    ValidityIssue(
                        "event_id_duplicate",
                        run_id=event.run_id,
                        subject=event.event_id,
                    )
                )
            else:
                event_owners[event.event_id] = imported
            producer_sequences.setdefault(event.producer_id, []).append(
                (event.producer_sequence, event.monotonic_time_ms, imported)
            )
    for producer_id, entries in producer_sequences.items():
        sequences = [sequence for sequence, _, _ in entries]
        owners = list({id(owner): owner for _, _, owner in entries}.values())
        if len(sequences) != len(set(sequences)):
            for owner in owners:
                owner.issues.append(
                    ValidityIssue("producer_sequence_duplicate", subject=producer_id)
                )
        unique = sorted(set(sequences))
        if unique and unique != list(range(unique[0], unique[-1] + 1)):
            for owner in owners:
                owner.issues.append(
                    ValidityIssue("producer_sequence_gap", subject=producer_id)
                )
        ordered = sorted(entries, key=lambda item: item[0])
        if any(later[1] < earlier[1] for earlier, later in zip(ordered, ordered[1:])):
            for owner in owners:
                owner.issues.append(
                    ValidityIssue("producer_sequence_reversal", subject=producer_id)
                )


def _commit_trial_validation(
    imported: _TrialImport, identity: _StudyIdentity
) -> dict[str, object]:
    assert imported.raw_payload is not None and imported.indexes is not None
    issues = stable_issues(imported.issues)
    trial_valid = not issues
    metrics = metric_validity(
        identity.metrics,
        run_ids=imported.manifest.run_ids,
        events=imported.events,
        trial_integrity_valid=trial_valid,
        formal_inputs_valid=imported.formal_inputs_valid,
    )
    if imported.formal_inputs is not None and _arrival_lateness_exceeded(
        imported.formal_inputs
    ):
        metrics = tuple(
            MetricValidity(
                metric.metric_name,
                False,
                tuple(sorted({*metric.reason_codes, "arrival_lateness_exceeded"})),
            )
            if metric.metric_name == "dct_ms"
            or metric.metric_name.startswith("throughput_")
            else metric
            for metric in metrics
        )
    raw_digest = _string(imported.raw_payload, "content_digest", "raw files manifest")
    validation = trial_validation_payload(
        trial_attempt_id=imported.manifest.trial_attempt_id,
        trial_id=imported.manifest.trial_id,
        trial_valid=trial_valid,
        issues=issues,
        metrics=metrics,
        raw_files_digest=raw_digest,
        analysis_inputs=(
            item.canonical_payload() for item in imported.analysis_inputs
        ),
        analysis_inputs_digest=imported.analysis_inputs_digest,
        index_counts=imported.indexes.counts(),
    )
    atomic_write_json(imported.entry.directory / "raw_files.json", imported.raw_payload)
    atomic_write_json(imported.entry.directory / "validity.json", validation)
    return {
        "trial_id": imported.manifest.trial_id,
        "trial_attempt_id": imported.manifest.trial_attempt_id,
        "trial_valid": trial_valid,
        "reason_codes": validation["reason_codes"],
        "raw_files_digest": raw_digest,
        "validation_digest": validation["validation_digest"],
    }


def _arrival_lateness_exceeded(formal: ValidatedAnalysisInputs) -> bool:
    measurement = tuple(
        run for run in formal.run_manifest.runs if run.phase == "measurement"
    )
    lateness = [
        float(run.arrival_lateness_ms)
        for run in measurement
        if run.arrival_lateness_ms is not None
    ]
    if not lateness:
        return False
    offsets = sorted(
        run.scheduled_offset_ms
        for run in measurement
        if run.scheduled_offset_ms is not None
    )
    mean_interval = (
        0.0
        if len(offsets) < 2
        else (offsets[-1] - offsets[0]) / (len(offsets) - 1)
    )
    limit_ms = max(10.0, mean_interval * 0.05)
    return type7_quantile(lateness, 0.99) > limit_ms


def _load_trial_manifest(path: Path) -> TrialManifest:
    payload = load_json_object(path, description="Trial manifest")
    required = {
        "schema_version",
        "schema",
        "trial_attempt_id",
        "trial_id",
        "attempt_index",
        "state",
        "run_ids",
        "experiment_ids",
        "committed_files",
    }
    if set(payload) != required or payload.get("schema") != TRIAL_MANIFEST_SCHEMA:
        raise ExperimentValidationError("Trial manifest schema is invalid")
    return TrialManifest(
        schema_version=_integer(payload, "schema_version", "Trial manifest"),
        trial_attempt_id=_string(payload, "trial_attempt_id", "Trial manifest"),
        trial_id=_string(payload, "trial_id", "Trial manifest"),
        attempt_index=_integer(payload, "attempt_index", "Trial manifest"),
        state=_string(payload, "state", "Trial manifest"),
        run_ids=_string_tuple(payload.get("run_ids"), "Trial run IDs"),
        experiment_ids=_string_tuple(
            payload.get("experiment_ids"), "Trial experiment IDs"
        ),
        committed_files=_string_tuple(
            payload.get("committed_files"), "Trial committed files"
        ),
    )


def _load_flush_results(
    path: Path, manifest: TrialManifest
) -> tuple[tuple[RunFlushResult, ...], tuple[ValidityIssue, ...]]:
    try:
        payload = load_json_object(path, description="Trial FlushResults")
    except ExperimentValidationError:
        return (), (ValidityIssue("flush_result_missing"),)
    if set(payload) != {"schema_version", "trial_attempt_id", "results"}:
        return (), (ValidityIssue("flush_result_conflict"),)
    if (
        payload.get("schema_version") != 1
        or payload.get("trial_attempt_id") != manifest.trial_attempt_id
    ):
        return (), (ValidityIssue("flush_result_conflict"),)
    try:
        raw_results = _list(payload.get("results"), "FlushResult list")
        results: list[RunFlushResult] = []
        for item in raw_results:
            raw = _mapping(item, "FlushResult")
            if set(raw) != {
                "run_id",
                "recording_complete",
                "committed_files",
                "payload",
            }:
                raise ExperimentValidationError("FlushResult fields are invalid")
            results.append(
                RunFlushResult.create(
                    _string(raw, "run_id", "FlushResult"),
                    _boolean(raw, "recording_complete", "FlushResult"),
                    _string_tuple(
                        raw.get("committed_files"), "FlushResult committed files"
                    ),
                    _mapping(raw.get("payload"), "FlushResult payload"),
                )
            )
    except (ExperimentValidationError, TypeError, ValueError):
        return (), (ValidityIssue("flush_result_conflict"),)
    issues: list[ValidityIssue] = []
    by_run: dict[str, RunFlushResult] = {}
    for result in results:
        if result.run_id not in manifest.run_ids:
            issues.append(ValidityIssue("run_reference_dangling", run_id=result.run_id))
        previous = by_run.get(result.run_id)
        if previous is not None:
            issues.append(ValidityIssue("flush_result_conflict", run_id=result.run_id))
        by_run[result.run_id] = result
    for run_id in manifest.run_ids:
        if run_id not in by_run:
            issues.append(ValidityIssue("flush_result_missing", run_id=run_id))
    return tuple(results), stable_issues(issues)


def _load_raw_baseline(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    payload = dict(load_json_object(path, description="raw files manifest"))
    if set(payload) != {
        "schema_version",
        "schema",
        "trial_attempt_id",
        "committed_paths",
        "files",
        "ignored_files",
        "content_digest",
    } or (
        payload.get("schema_version") != 1 or payload.get("schema") != RAW_FILES_SCHEMA
    ):
        raise ExperimentValidationError("raw files manifest schema is invalid")
    _string(payload, "trial_attempt_id", "raw files manifest")
    committed = _string_tuple(payload.get("committed_paths"), "raw committed paths")
    if len(committed) != len(set(committed)):
        raise ExperimentValidationError("raw committed paths are duplicated")
    _validate_raw_file_records(payload.get("files"))
    _validate_ignored_file_records(payload.get("ignored_files"))
    body = {
        "trial_attempt_id": payload["trial_attempt_id"],
        "committed_paths": payload["committed_paths"],
        "files": payload["files"],
        "ignored_files": payload["ignored_files"],
    }
    if payload.get("content_digest") != canonical_json_digest(body):
        raise ExperimentValidationError("raw files manifest digest is invalid")
    return payload


def _raw_file_map(
    payload: Mapping[str, object] | None,
) -> dict[str, Mapping[str, object]]:
    if payload is None:
        return {}
    result: dict[str, Mapping[str, object]] = {}
    for item in _list(payload.get("files"), "raw files"):
        record = _mapping(item, "raw file")
        source = _string(record, "source_path", "raw file")
        result[source] = record
    return result


def _validate_raw_file_records(value: object) -> None:
    logical_names: set[str] = set()
    sources: set[str] = set()
    for item in _list(value, "raw files"):
        record = _mapping(item, "raw file")
        if set(record) != {
            "logical_name",
            "run_id",
            "source_path",
            "size_bytes",
            "sha256",
            "parquet_kind",
            "row_count",
        }:
            raise ExperimentValidationError("raw file fields are invalid")
        logical = _string(record, "logical_name", "raw file")
        source = _string(record, "source_path", "raw file")
        _string(record, "run_id", "raw file")
        size = _integer(record, "size_bytes", "raw file")
        rows = _integer(record, "row_count", "raw file")
        digest = _string(record, "sha256", "raw file")
        if (
            logical in logical_names
            or source in sources
            or size < 0
            or rows < 0
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or record.get("parquet_kind") not in {"context", "event", "invalid"}
        ):
            raise ExperimentValidationError("raw file record is invalid")
        logical_names.add(logical)
        sources.add(source)


def _validate_ignored_file_records(value: object) -> None:
    seen: set[str] = set()
    for item in _list(value, "ignored files"):
        record = _mapping(item, "ignored file")
        if set(record) != {"source_path", "reason_code"}:
            raise ExperimentValidationError("ignored file fields are invalid")
        source = _string(record, "source_path", "ignored file")
        if record.get("reason_code") != "unlisted_trial_file" or source in seen:
            raise ExperimentValidationError("ignored file record is invalid")
        seen.add(source)


def _mapping(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ExperimentValidationError(f"{description} must be an object")
    return cast(Mapping[str, object], value)


def _list(value: object, description: str) -> list[object]:
    if not isinstance(value, list):
        raise ExperimentValidationError(f"{description} must be an array")
    return value


def _string(payload: Mapping[str, object], name: str, description: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ExperimentValidationError(f"{description} {name} is invalid")
    return value


def _string_value(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExperimentValidationError(f"{description} is invalid")
    return value


def _integer(payload: Mapping[str, object], name: str, description: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExperimentValidationError(f"{description} {name} is invalid")
    return value


def _boolean(payload: Mapping[str, object], name: str, description: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise ExperimentValidationError(f"{description} {name} is invalid")
    return value


def _string_tuple(value: object, description: str) -> tuple[str, ...]:
    return tuple(_string_value(item, description) for item in _list(value, description))
