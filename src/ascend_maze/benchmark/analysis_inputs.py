"""Validation and digest binding for formal non-C8 Trial analysis inputs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Mapping, Sequence

from ascend_maze.benchmark.canonical import canonical_json_digest
from ascend_maze.benchmark.persistence import load_json_object
from ascend_maze.benchmark.schedule_parquet import SCHEDULE_SCHEMA_NAME
from ascend_maze.benchmark.state import RunManifestData, parse_run_manifest
from ascend_maze.core.errors import ExperimentValidationError


@dataclass(frozen=True, slots=True)
class AnalysisInputRecord:
    logical_name: str
    size_bytes: int
    sha256: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "logical_name": self.logical_name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ValidatedAnalysisInputs:
    records: tuple[AnalysisInputRecord, ...]
    run_manifest: RunManifestData
    resource_before: Mapping[str, object]
    resource_after: Mapping[str, object]

    @property
    def digest(self) -> str:
        return canonical_json_digest(
            [record.canonical_payload() for record in self.records]
        )


def validate_analysis_inputs(
    directory: Path,
    *,
    trial_attempt_id: str,
    committed_run_ids: Sequence[str],
) -> ValidatedAnalysisInputs:
    paths = {
        "arrival_schedule.parquet": directory / "arrival_schedule.parquet",
        "flush_results.json": directory / "flush_results.json",
        "resource_after.json": directory / "resource_after.json",
        "resource_before.json": directory / "resource_before.json",
        "run_manifest.json": directory / "run_manifest.json",
    }
    for logical_name, path in paths.items():
        if not path.is_file() or path.is_symlink():
            raise ExperimentValidationError(
                f"formal analysis input is unavailable: {logical_name}"
            )
    run_payload = load_json_object(paths["run_manifest.json"], description="Run manifest")
    run_manifest = parse_run_manifest(run_payload)
    if run_manifest.trial_attempt_id != trial_attempt_id:
        raise ExperimentValidationError("Run manifest Trial identity is invalid")
    run_ids = tuple(run.run_id for run in run_manifest.runs if run.run_id is not None)
    if tuple(committed_run_ids) != run_ids:
        raise ExperimentValidationError("Run manifest committed Runs do not match Trial")
    _validate_schedule(
        paths["arrival_schedule.parquet"],
        trial_attempt_id=trial_attempt_id,
        runs=run_manifest,
    )
    before = load_json_object(paths["resource_before.json"], description="resource baseline")
    after = load_json_object(paths["resource_after.json"], description="resource result")
    _validate_resource_snapshots(before, after)
    records = tuple(
        _record(logical_name, path) for logical_name, path in sorted(paths.items())
    )
    return ValidatedAnalysisInputs(records, run_manifest, before, after)


def verify_analysis_input_records(
    directory: Path, records: Sequence[Mapping[str, object]]
) -> None:
    seen: set[str] = set()
    for item in records:
        logical_name = item.get("logical_name")
        size_bytes = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            not isinstance(logical_name, str)
            or logical_name in seen
            or logical_name not in {
                "arrival_schedule.parquet",
                "flush_results.json",
                "resource_after.json",
                "resource_before.json",
                "run_manifest.json",
            }
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ExperimentValidationError("validity analysis input record is invalid")
        seen.add(logical_name)
        observed = _record(logical_name, directory / logical_name)
        if observed.size_bytes != size_bytes or observed.sha256 != digest:
            raise ExperimentValidationError(
                f"formal analysis input changed after validation: {logical_name}"
            )
    if seen != {
        "arrival_schedule.parquet",
        "flush_results.json",
        "resource_after.json",
        "resource_before.json",
        "run_manifest.json",
    }:
        raise ExperimentValidationError("validity does not bind every analysis input")


def _validate_schedule(
    path: Path, *, trial_attempt_id: str, runs: RunManifestData
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(path)
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise ExperimentValidationError("arrival schedule Parquet is invalid") from exc
    metadata = table.schema.metadata or {}
    if (
        metadata.get(b"ascend_maze.schema")
        != SCHEDULE_SCHEMA_NAME.encode("ascii")
        or metadata.get(b"ascend_maze.schema_version") != b"1"
    ):
        raise ExperimentValidationError("arrival schedule metadata is invalid")
    expected_fields = (
        "schema_version",
        "trial_attempt_id",
        "mode",
        "phase",
        "arrival_index",
        "scheduled_offset_ms",
        "record_id",
        "input_digest",
        "submission_id",
    )
    if tuple(table.column_names) != expected_fields:
        raise ExperimentValidationError("arrival schedule columns are invalid")
    rows = table.to_pylist()
    if len(rows) != len(runs.runs):
        raise ExperimentValidationError("arrival schedule row count is invalid")
    for row, run in zip(rows, runs.runs, strict=True):
        expected = {
            "schema_version": 1,
            "trial_attempt_id": trial_attempt_id,
            "phase": run.phase,
            "arrival_index": run.arrival_index,
            "scheduled_offset_ms": run.scheduled_offset_ms,
            "record_id": run.record_id,
            "input_digest": run.input_digest,
            "submission_id": run.submission_id,
        }
        if any(row.get(key) != value for key, value in expected.items()):
            raise ExperimentValidationError("arrival schedule and Run manifest differ")
        if row.get("mode") not in {
            "closed_loop",
            "fixed_rate",
            "poisson",
            "trace_replay",
        }:
            raise ExperimentValidationError("arrival schedule mode is invalid")


def _validate_resource_snapshots(
    before: Mapping[str, object], after: Mapping[str, object]
) -> None:
    required = {
        "captured_at_wall_ms",
        "controller_generation",
        "config_fingerprint",
        "snapshot_digest",
        "payload",
    }
    if set(before) != required or set(after) != required | {"recovery"}:
        raise ExperimentValidationError("resource snapshot fields are invalid")
    for snapshot in (before, after):
        payload = snapshot.get("payload")
        if not isinstance(payload, Mapping):
            raise ExperimentValidationError("resource snapshot payload is invalid")
        if snapshot.get("snapshot_digest") != canonical_json_digest(payload):
            raise ExperimentValidationError("resource snapshot digest is invalid")
        captured = snapshot.get("captured_at_wall_ms")
        if isinstance(captured, bool) or not isinstance(captured, int) or captured < 0:
            raise ExperimentValidationError("resource snapshot time is invalid")
        for field in ("controller_generation", "config_fingerprint"):
            if not isinstance(snapshot.get(field), str) or not snapshot[field]:
                raise ExperimentValidationError("resource snapshot identity is invalid")
    if before["config_fingerprint"] != after["config_fingerprint"]:
        raise ExperimentValidationError("resource snapshot config identity changed")
    recovery = after.get("recovery")
    if not isinstance(recovery, Mapping) or not isinstance(recovery.get("recovered"), bool):
        raise ExperimentValidationError("resource recovery result is invalid")


def _record(logical_name: str, path: Path) -> AnalysisInputRecord:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ExperimentValidationError(
            f"cannot read formal analysis input: {logical_name}"
        ) from exc
    return AnalysisInputRecord(logical_name, len(data), hashlib.sha256(data).hexdigest())
