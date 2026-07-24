"""Stable C14 validity contracts and reason codes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ascend_maze.benchmark.canonical import canonical_json_digest

RAW_FILES_SCHEMA = "ascend-maze.raw-files.v1"
TRIAL_VALIDITY_SCHEMA = "ascend-maze.trial-validity.v1"
STUDY_VALIDATION_SCHEMA = "ascend-maze.study-validation.v1"

# These codes are persisted analysis inputs. Renaming one requires a schema revision.
REASON_CODES = frozenset(
    {
        "committed_file_hash_changed",
        "committed_file_manifest_mismatch",
        "committed_file_missing",
        "committed_file_not_regular",
        "committed_file_path_invalid",
        "committed_file_temporary",
        "context_identity_mismatch",
        "context_missing",
        "context_row_invalid",
        "analysis_input_invalid",
        "analysis_input_missing",
        "arrival_lateness_exceeded",
        "dispatch_reference_dangling",
        "dropped_control_events",
        "dropped_telemetry_events",
        "event_id_duplicate",
        "event_row_invalid",
        "flush_result_conflict",
        "flush_result_missing",
        "metric_dependency_unknown",
        "metric_required_fact_missing",
        "missing_producer_reported",
        "model_instance_reference_dangling",
        "parquet_footer_invalid",
        "parquet_metadata_invalid",
        "parquet_schema_invalid",
        "placement_lease_interval_inverted",
        "placement_lease_interval_open",
        "placement_lease_reference_dangling",
        "privacy_violation",
        "producer_missing",
        "producer_unexpected",
        "producer_sequence_duplicate",
        "producer_sequence_gap",
        "producer_sequence_reversal",
        "raw_files_snapshot_invalid",
        "recording_incomplete",
        "route_lease_interval_inverted",
        "route_lease_interval_open",
        "route_lease_reference_dangling",
        "run_identity_mismatch",
        "run_reference_dangling",
        "sequence_gap_reported",
        "task_attempt_interval_inverted",
        "task_attempt_interval_open",
        "task_attempt_reference_dangling",
        "task_reference_dangling",
        "terminal_event_conflict",
        "terminal_event_missing",
        "trial_manifest_identity_mismatch",
        "trial_manifest_state_invalid",
        "unlisted_trial_file",
        "worker_lease_interval_inverted",
        "worker_lease_interval_open",
        "worker_lease_reference_dangling",
        "writer_error",
    }
)


@dataclass(frozen=True, slots=True)
class ValidityIssue:
    reason_code: str
    run_id: str | None = None
    subject: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if self.reason_code not in REASON_CODES:
            raise ValueError(f"unknown C14 reason code: {self.reason_code}")
        for name in ("run_id", "subject", "source"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"ValidityIssue {name} must be non-empty or None")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "run_id": self.run_id,
            "subject": self.subject,
            "source": self.source,
        }


def stable_issues(issues: Iterable[ValidityIssue]) -> tuple[ValidityIssue, ...]:
    return tuple(
        sorted(
            set(issues),
            key=lambda item: (
                item.reason_code,
                item.run_id or "",
                item.subject or "",
                item.source or "",
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class MetricValidity:
    metric_name: str
    valid: bool
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.metric_name:
            raise ValueError("metric_name is required")
        reasons = tuple(sorted(set(self.reason_codes)))
        if any(reason not in REASON_CODES for reason in reasons):
            raise ValueError("metric validity contains an unknown reason code")
        if self.valid == bool(reasons):
            raise ValueError("metric validity and reason codes disagree")
        object.__setattr__(self, "reason_codes", reasons)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "metric_name": self.metric_name,
            "valid": self.valid,
            "reason_codes": self.reason_codes,
        }


@dataclass(frozen=True, slots=True)
class RawFileRecord:
    logical_name: str
    run_id: str
    source_path: str
    size_bytes: int
    sha256: str
    parquet_kind: str
    row_count: int

    def canonical_payload(self) -> dict[str, object]:
        return {
            "logical_name": self.logical_name,
            "run_id": self.run_id,
            "source_path": self.source_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "parquet_kind": self.parquet_kind,
            "row_count": self.row_count,
        }


@dataclass(frozen=True, slots=True)
class IgnoredFileRecord:
    source_path: str
    reason_code: str

    def canonical_payload(self) -> dict[str, str]:
        return {
            "source_path": self.source_path,
            "reason_code": self.reason_code,
        }


def raw_files_payload(
    *,
    trial_attempt_id: str,
    committed_paths: Iterable[str],
    files: Iterable[RawFileRecord],
    ignored_files: Iterable[IgnoredFileRecord],
) -> dict[str, object]:
    file_payloads = [
        item.canonical_payload()
        for item in sorted(files, key=lambda item: item.logical_name)
    ]
    ignored_payloads = [
        item.canonical_payload()
        for item in sorted(
            set(ignored_files), key=lambda item: (item.source_path, item.reason_code)
        )
    ]
    identity = {
        "trial_attempt_id": trial_attempt_id,
        "committed_paths": tuple(sorted(set(committed_paths))),
        "files": file_payloads,
        "ignored_files": ignored_payloads,
    }
    return {
        "schema_version": 1,
        "schema": RAW_FILES_SCHEMA,
        **identity,
        "content_digest": canonical_json_digest(identity),
    }


def trial_validation_payload(
    *,
    trial_attempt_id: str,
    trial_id: str,
    trial_valid: bool,
    issues: Iterable[ValidityIssue],
    metrics: Iterable[MetricValidity],
    raw_files_digest: str,
    analysis_inputs: Iterable[dict[str, object]],
    analysis_inputs_digest: str,
    index_counts: dict[str, int],
) -> dict[str, object]:
    ordered_issues = stable_issues(issues)
    ordered_metrics = tuple(sorted(metrics, key=lambda item: item.metric_name))
    reason_codes = tuple(sorted({item.reason_code for item in ordered_issues}))
    ordered_inputs = sorted(analysis_inputs, key=lambda item: str(item["logical_name"]))
    body = {
        "trial_attempt_id": trial_attempt_id,
        "trial_id": trial_id,
        "trial_valid": trial_valid,
        "reason_codes": reason_codes,
        "reasons": [item.canonical_payload() for item in ordered_issues],
        "metric_valid": [item.canonical_payload() for item in ordered_metrics],
        "raw_files_digest": raw_files_digest,
        "analysis_inputs": ordered_inputs,
        "analysis_inputs_digest": analysis_inputs_digest,
        "index_counts": dict(sorted(index_counts.items())),
    }
    return {
        "schema_version": 1,
        "schema": TRIAL_VALIDITY_SCHEMA,
        **body,
        "validation_digest": canonical_json_digest(body),
    }
