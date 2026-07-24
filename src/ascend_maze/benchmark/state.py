"""Durable Trial execution state and transition validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, cast

from ascend_maze.benchmark.runtime import (
    ResourceRecoveryResult,
    ResourceSnapshot,
    RunFlushResult,
    TERMINAL_RUN_STATES,
)
from ascend_maze.core.canonical import FrozenMap, freeze_canonical
from ascend_maze.core.errors import ExperimentValidationError

TRIAL_STATE_SCHEMA = "ascend-maze.trial-state.v1"
RUN_MANIFEST_SCHEMA = "ascend-maze.run-manifest.v1"
TRIAL_STATES = frozenset(
    {
        "planned",
        "preparing",
        "warming",
        "measuring",
        "draining",
        "flushing",
        "valid",
        "invalid",
        "aborted",
    }
)
TERMINAL_TRIAL_STATES = frozenset({"valid", "invalid", "aborted"})
_NEXT_STATE = {
    "planned": frozenset({"preparing", "aborted"}),
    "preparing": frozenset({"warming", "aborted"}),
    "warming": frozenset({"measuring", "aborted"}),
    "measuring": frozenset({"draining", "aborted"}),
    "draining": frozenset({"flushing", "aborted"}),
    "flushing": frozenset({"valid", "invalid", "aborted"}),
    "valid": frozenset(),
    "invalid": frozenset(),
    "aborted": frozenset(),
}


def _optional_non_negative(name: str, value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ExperimentValidationError(f"{name} must be non-negative or null")


@dataclass(frozen=True, slots=True)
class TrialRunRecord:
    phase: str
    arrival_index: int
    record_id: str
    input_digest: str
    submission_id: str
    scheduled_offset_ms: int | None
    scheduled_at_monotonic_ms: int | None = None
    offered_at_monotonic_ms: int | None = None
    issued_at_monotonic_ms: int | None = None
    admitted_at_monotonic_ms: int | None = None
    arrival_lateness_ms: int | None = None
    run_id: str | None = None
    submission_replayed: bool = False
    submission_error: str | None = None
    terminal_status: str | None = None
    terminal_at_monotonic_ms: int | None = None
    flushed: bool = False
    recording_complete: bool | None = None
    destroyed: bool = False

    def __post_init__(self) -> None:
        if self.phase not in {"warmup", "measurement"}:
            raise ExperimentValidationError("Trial Run phase is invalid")
        if self.arrival_index < 0 or not self.record_id or not self.submission_id:
            raise ExperimentValidationError("Trial Run identity is invalid")
        for name in (
            "scheduled_offset_ms",
            "scheduled_at_monotonic_ms",
            "offered_at_monotonic_ms",
            "issued_at_monotonic_ms",
            "admitted_at_monotonic_ms",
            "arrival_lateness_ms",
            "terminal_at_monotonic_ms",
        ):
            _optional_non_negative(name, getattr(self, name))
        if (
            self.issued_at_monotonic_ms is not None
            and self.offered_at_monotonic_ms is None
        ):
            raise ExperimentValidationError("issued Run must first be offered")
        if self.admitted_at_monotonic_ms is not None and not self.run_id:
            raise ExperimentValidationError("admitted Run requires a Run ID")
        if self.run_id and self.admitted_at_monotonic_ms is None:
            raise ExperimentValidationError("Run ID requires an admission timestamp")
        if (
            self.terminal_status is not None
            and self.terminal_status not in TERMINAL_RUN_STATES
        ):
            raise ExperimentValidationError("Trial Run terminal status is invalid")
        if (self.terminal_status is None) != (self.terminal_at_monotonic_ms is None):
            raise ExperimentValidationError(
                "terminal status and time must appear together"
            )
        if self.flushed and self.terminal_status is None:
            raise ExperimentValidationError("only terminal Runs can be flushed")
        if self.recording_complete is not None and not self.flushed:
            raise ExperimentValidationError("recording result requires a flush")
        if self.destroyed and not self.flushed:
            raise ExperimentValidationError("only flushed Runs can be destroyed")
        if not isinstance(self.submission_replayed, bool):
            raise ExperimentValidationError("submission_replayed must be a boolean")

    @property
    def committed(self) -> bool:
        return self.run_id is not None

    @property
    def terminal(self) -> bool:
        return self.terminal_status is not None

    def canonical_payload(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "arrival_index": self.arrival_index,
            "record_id": self.record_id,
            "input_digest": self.input_digest,
            "submission_id": self.submission_id,
            "scheduled_offset_ms": self.scheduled_offset_ms,
            "scheduled_at_monotonic_ms": self.scheduled_at_monotonic_ms,
            "offered_at_monotonic_ms": self.offered_at_monotonic_ms,
            "issued_at_monotonic_ms": self.issued_at_monotonic_ms,
            "admitted_at_monotonic_ms": self.admitted_at_monotonic_ms,
            "arrival_lateness_ms": self.arrival_lateness_ms,
            "run_id": self.run_id,
            "submission_replayed": self.submission_replayed,
            "submission_error": self.submission_error,
            "terminal_status": self.terminal_status,
            "terminal_at_monotonic_ms": self.terminal_at_monotonic_ms,
            "flushed": self.flushed,
            "recording_complete": self.recording_complete,
            "destroyed": self.destroyed,
        }


@dataclass(frozen=True, slots=True)
class TrialCounters:
    offered: int
    issued: int
    committed: int
    terminal: int
    succeeded: int
    failed: int
    timed_out: int

    @classmethod
    def from_runs(
        cls, runs: tuple[TrialRunRecord, ...], *, phase: str
    ) -> "TrialCounters":
        selected = tuple(item for item in runs if item.phase == phase)
        return cls(
            offered=sum(item.offered_at_monotonic_ms is not None for item in selected),
            issued=sum(item.issued_at_monotonic_ms is not None for item in selected),
            committed=sum(item.committed for item in selected),
            terminal=sum(item.terminal for item in selected),
            succeeded=sum(item.terminal_status == "succeeded" for item in selected),
            failed=sum(
                item.terminal_status in {"failed", "cancelled", "interrupted"}
                for item in selected
            ),
            timed_out=sum(item.terminal_status == "timed_out" for item in selected),
        )

    def canonical_payload(self) -> dict[str, int]:
        return {
            "offered": self.offered,
            "issued": self.issued,
            "committed": self.committed,
            "terminal": self.terminal,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "timed_out": self.timed_out,
        }


@dataclass(frozen=True, slots=True)
class RunManifestData:
    trial_attempt_id: str
    runs: tuple[TrialRunRecord, ...]
    warmup_counters: TrialCounters
    measurement_counters: TrialCounters


def parse_run_manifest(payload: Mapping[str, object]) -> RunManifestData:
    _require_fields(
        payload,
        {
            "schema_version",
            "schema",
            "trial_attempt_id",
            "runs",
            "warmup_excluded_from_measurement",
            "warmup_counters",
            "measurement_counters",
        },
        "Run manifest",
    )
    if payload.get("schema_version") != 1 or payload.get("schema") != RUN_MANIFEST_SCHEMA:
        raise ExperimentValidationError("Run manifest schema is invalid")
    if payload.get("warmup_excluded_from_measurement") is not True:
        raise ExperimentValidationError("Run manifest must exclude warmup")
    runs = tuple(
        _parse_run(_mapping(item, "Trial Run"))
        for item in _list(payload.get("runs"), "Run manifest runs")
    )
    if len({(item.phase, item.arrival_index) for item in runs}) != len(runs):
        raise ExperimentValidationError("Run manifest contains duplicate arrivals")
    warmup = TrialCounters.from_runs(runs, phase="warmup")
    measurement = TrialCounters.from_runs(runs, phase="measurement")
    if _mapping(payload.get("warmup_counters"), "warmup counters") != (
        warmup.canonical_payload()
    ):
        raise ExperimentValidationError("Run manifest warmup counters are invalid")
    if _mapping(payload.get("measurement_counters"), "measurement counters") != (
        measurement.canonical_payload()
    ):
        raise ExperimentValidationError("Run manifest measurement counters are invalid")
    return RunManifestData(
        trial_attempt_id=_string(payload, "trial_attempt_id"),
        runs=runs,
        warmup_counters=warmup,
        measurement_counters=measurement,
    )


@dataclass(frozen=True, slots=True)
class TrialExecutionState:
    schema_version: int
    trial_attempt_id: str
    trial_id: str
    attempt_index: int
    state: str
    revision: int
    created_at_wall_ms: int
    updated_at_wall_ms: int
    warmup_started_monotonic_ms: int | None
    measurement_started_monotonic_ms: int | None
    drain_deadline_monotonic_ms: int | None
    runs: tuple[TrialRunRecord, ...]
    flush_results: tuple[RunFlushResult, ...] = ()
    resource_before: ResourceSnapshot | None = None
    resource_after: ResourceSnapshot | None = None
    recovery: ResourceRecoveryResult | None = None
    invalid_reasons: tuple[str, ...] = ()
    last_error: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.state not in TRIAL_STATES:
            raise ExperimentValidationError("Trial execution state is invalid")
        if not self.trial_attempt_id or not self.trial_id:
            raise ExperimentValidationError("Trial execution identity is incomplete")
        for name in (
            "attempt_index",
            "revision",
            "created_at_wall_ms",
            "updated_at_wall_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExperimentValidationError(f"Trial state {name} is invalid")
        for name in (
            "warmup_started_monotonic_ms",
            "measurement_started_monotonic_ms",
            "drain_deadline_monotonic_ms",
        ):
            _optional_non_negative(name, getattr(self, name))
        runs = tuple(self.runs)
        keys = {(item.phase, item.arrival_index) for item in runs}
        if len(keys) != len(runs):
            raise ExperimentValidationError("Trial state contains duplicate arrivals")
        submissions = {item.submission_id for item in runs}
        if len(submissions) != len(runs):
            raise ExperimentValidationError(
                "Trial state contains duplicate submissions"
            )
        object.__setattr__(self, "runs", runs)
        flush_results = tuple(self.flush_results)
        if len({item.run_id for item in flush_results}) != len(flush_results):
            raise ExperimentValidationError(
                "Trial state contains duplicate flush results"
            )
        object.__setattr__(self, "flush_results", flush_results)
        reasons = tuple(self.invalid_reasons)
        if any(not isinstance(item, str) or not item for item in reasons):
            raise ExperimentValidationError("Trial invalid reasons are invalid")
        object.__setattr__(self, "invalid_reasons", reasons)
        if self.state == "valid" and reasons:
            raise ExperimentValidationError("valid Trial cannot have invalid reasons")
        if self.state == "invalid" and not reasons:
            raise ExperimentValidationError("invalid Trial requires a reason")

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_TRIAL_STATES

    @property
    def measurement_counters(self) -> TrialCounters:
        return TrialCounters.from_runs(self.runs, phase="measurement")

    @property
    def warmup_counters(self) -> TrialCounters:
        return TrialCounters.from_runs(self.runs, phase="warmup")

    def transition(self, target: str, *, wall_ms: int) -> "TrialExecutionState":
        if target not in _NEXT_STATE[self.state]:
            raise ExperimentValidationError(
                f"invalid Trial transition: {self.state} -> {target}"
            )
        return replace(
            self,
            state=target,
            revision=self.revision + 1,
            updated_at_wall_ms=wall_ms,
        )

    def revised(self, *, wall_ms: int, **changes: Any) -> "TrialExecutionState":
        if self.terminal:
            raise ExperimentValidationError("terminal Trial state is immutable")
        return replace(
            self,
            revision=self.revision + 1,
            updated_at_wall_ms=wall_ms,
            **changes,
        )

    def run(self, phase: str, arrival_index: int) -> TrialRunRecord:
        for item in self.runs:
            if item.phase == phase and item.arrival_index == arrival_index:
                return item
        raise ExperimentValidationError(
            f"Trial arrival does not exist: {phase}/{arrival_index}"
        )

    def replace_run(
        self, updated: TrialRunRecord, *, wall_ms: int
    ) -> "TrialExecutionState":
        found = False
        runs: list[TrialRunRecord] = []
        for item in self.runs:
            if (item.phase, item.arrival_index) == (
                updated.phase,
                updated.arrival_index,
            ):
                runs.append(updated)
                found = True
            else:
                runs.append(item)
        if not found:
            raise ExperimentValidationError("cannot update an unknown Trial arrival")
        return self.revised(wall_ms=wall_ms, runs=tuple(runs))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "schema": TRIAL_STATE_SCHEMA,
            "trial_attempt_id": self.trial_attempt_id,
            "trial_id": self.trial_id,
            "attempt_index": self.attempt_index,
            "state": self.state,
            "revision": self.revision,
            "created_at_wall_ms": self.created_at_wall_ms,
            "updated_at_wall_ms": self.updated_at_wall_ms,
            "warmup_started_monotonic_ms": self.warmup_started_monotonic_ms,
            "measurement_started_monotonic_ms": self.measurement_started_monotonic_ms,
            "drain_deadline_monotonic_ms": self.drain_deadline_monotonic_ms,
            "warmup_counters": self.warmup_counters.canonical_payload(),
            "measurement_counters": self.measurement_counters.canonical_payload(),
            "runs": [item.canonical_payload() for item in self.runs],
            "flush_results": [item.canonical_payload() for item in self.flush_results],
            "resource_before": (
                None
                if self.resource_before is None
                else self.resource_before.canonical_payload()
            ),
            "resource_after": (
                None
                if self.resource_after is None
                else self.resource_after.canonical_payload()
            ),
            "recovery": None
            if self.recovery is None
            else self.recovery.canonical_payload(),
            "invalid_reasons": self.invalid_reasons,
            "last_error": self.last_error,
        }

    def run_manifest_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "schema": RUN_MANIFEST_SCHEMA,
            "trial_attempt_id": self.trial_attempt_id,
            "runs": [item.canonical_payload() for item in self.runs],
            "warmup_excluded_from_measurement": True,
            "warmup_counters": self.warmup_counters.canonical_payload(),
            "measurement_counters": self.measurement_counters.canonical_payload(),
        }


def parse_trial_execution_state(
    payload: Mapping[str, object],
) -> TrialExecutionState:
    allowed = {
        "schema_version",
        "schema",
        "trial_attempt_id",
        "trial_id",
        "attempt_index",
        "state",
        "revision",
        "created_at_wall_ms",
        "updated_at_wall_ms",
        "warmup_started_monotonic_ms",
        "measurement_started_monotonic_ms",
        "drain_deadline_monotonic_ms",
        "warmup_counters",
        "measurement_counters",
        "runs",
        "flush_results",
        "resource_before",
        "resource_after",
        "recovery",
        "invalid_reasons",
        "last_error",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ExperimentValidationError(f"Trial state field is unknown: {unknown[0]}")
    if payload.get("schema") != TRIAL_STATE_SCHEMA:
        raise ExperimentValidationError("Trial state schema is invalid")
    runs_raw = _list(payload.get("runs"), "Trial state runs")
    flush_raw = _list(payload.get("flush_results"), "Trial state flush results")
    reasons_raw = _list(payload.get("invalid_reasons"), "Trial invalid reasons")
    state = TrialExecutionState(
        schema_version=_integer(payload, "schema_version"),
        trial_attempt_id=_string(payload, "trial_attempt_id"),
        trial_id=_string(payload, "trial_id"),
        attempt_index=_integer(payload, "attempt_index"),
        state=_string(payload, "state"),
        revision=_integer(payload, "revision"),
        created_at_wall_ms=_integer(payload, "created_at_wall_ms"),
        updated_at_wall_ms=_integer(payload, "updated_at_wall_ms"),
        warmup_started_monotonic_ms=_optional_integer(
            payload, "warmup_started_monotonic_ms"
        ),
        measurement_started_monotonic_ms=_optional_integer(
            payload, "measurement_started_monotonic_ms"
        ),
        drain_deadline_monotonic_ms=_optional_integer(
            payload, "drain_deadline_monotonic_ms"
        ),
        runs=tuple(_parse_run(_mapping(item, "Trial Run")) for item in runs_raw),
        flush_results=tuple(
            _parse_flush(_mapping(item, "flush result")) for item in flush_raw
        ),
        resource_before=_parse_optional_snapshot(payload.get("resource_before")),
        resource_after=_parse_optional_snapshot(payload.get("resource_after")),
        recovery=_parse_optional_recovery(payload.get("recovery")),
        invalid_reasons=tuple(
            _string_value(item, "Trial invalid reason") for item in reasons_raw
        ),
        last_error=_optional_string(payload.get("last_error"), "last_error"),
    )
    if _mapping(payload.get("warmup_counters"), "warmup counters") != (
        state.warmup_counters.canonical_payload()
    ):
        raise ExperimentValidationError(
            "Trial warmup counters do not match Run records"
        )
    if _mapping(payload.get("measurement_counters"), "measurement counters") != (
        state.measurement_counters.canonical_payload()
    ):
        raise ExperimentValidationError(
            "Trial measurement counters do not match Run records"
        )
    return state


def _parse_run(payload: Mapping[str, object]) -> TrialRunRecord:
    _require_fields(
        payload,
        {
            "phase",
            "arrival_index",
            "record_id",
            "input_digest",
            "submission_id",
            "scheduled_offset_ms",
            "scheduled_at_monotonic_ms",
            "offered_at_monotonic_ms",
            "issued_at_monotonic_ms",
            "admitted_at_monotonic_ms",
            "arrival_lateness_ms",
            "run_id",
            "submission_replayed",
            "submission_error",
            "terminal_status",
            "terminal_at_monotonic_ms",
            "flushed",
            "recording_complete",
            "destroyed",
        },
        "Trial Run",
    )
    return TrialRunRecord(
        phase=_string(payload, "phase"),
        arrival_index=_integer(payload, "arrival_index"),
        record_id=_string(payload, "record_id"),
        input_digest=_string(payload, "input_digest"),
        submission_id=_string(payload, "submission_id"),
        scheduled_offset_ms=_optional_integer(payload, "scheduled_offset_ms"),
        scheduled_at_monotonic_ms=_optional_integer(
            payload, "scheduled_at_monotonic_ms"
        ),
        offered_at_monotonic_ms=_optional_integer(payload, "offered_at_monotonic_ms"),
        issued_at_monotonic_ms=_optional_integer(payload, "issued_at_monotonic_ms"),
        admitted_at_monotonic_ms=_optional_integer(payload, "admitted_at_monotonic_ms"),
        arrival_lateness_ms=_optional_integer(payload, "arrival_lateness_ms"),
        run_id=_optional_string(payload.get("run_id"), "run_id"),
        submission_replayed=_boolean(payload, "submission_replayed"),
        submission_error=_optional_string(
            payload.get("submission_error"), "submission_error"
        ),
        terminal_status=_optional_string(
            payload.get("terminal_status"), "terminal_status"
        ),
        terminal_at_monotonic_ms=_optional_integer(payload, "terminal_at_monotonic_ms"),
        flushed=_boolean(payload, "flushed"),
        recording_complete=_optional_boolean(payload, "recording_complete"),
        destroyed=_boolean(payload, "destroyed"),
    )


def _parse_flush(payload: Mapping[str, object]) -> RunFlushResult:
    _require_fields(
        payload,
        {"run_id", "recording_complete", "committed_files", "payload"},
        "flush result",
    )
    files = _list(payload.get("committed_files"), "committed files")
    return RunFlushResult.create(
        _string(payload, "run_id"),
        _boolean(payload, "recording_complete"),
        tuple(_string_value(item, "committed file") for item in files),
        _mapping(payload.get("payload"), "flush payload"),
    )


def _parse_optional_snapshot(value: object) -> ResourceSnapshot | None:
    if value is None:
        return None
    payload = _mapping(value, "resource snapshot")
    _require_fields(
        payload,
        {
            "captured_at_wall_ms",
            "controller_generation",
            "config_fingerprint",
            "snapshot_digest",
            "payload",
        },
        "resource snapshot",
    )
    return ResourceSnapshot(
        captured_at_wall_ms=_integer(payload, "captured_at_wall_ms"),
        controller_generation=_string(payload, "controller_generation"),
        config_fingerprint=_string(payload, "config_fingerprint"),
        snapshot_digest=_string(payload, "snapshot_digest"),
        payload=cast(
            FrozenMap, freeze_canonical(_mapping(payload.get("payload"), "payload"))
        ),
    )


def _parse_optional_recovery(value: object) -> ResourceRecoveryResult | None:
    if value is None:
        return None
    payload = _mapping(value, "resource recovery")
    _require_fields(
        payload,
        {"recovered", "checked_at_wall_ms", "reason_code", "details"},
        "resource recovery",
    )
    return ResourceRecoveryResult.create(
        recovered=_boolean(payload, "recovered"),
        checked_at_wall_ms=_integer(payload, "checked_at_wall_ms"),
        reason_code=_optional_string(payload.get("reason_code"), "reason_code"),
        details=_mapping(payload.get("details"), "recovery details"),
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ExperimentValidationError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ExperimentValidationError(f"{name} must be an array")
    return value


def _string(payload: Mapping[str, object], name: str) -> str:
    return _string_value(payload.get(name), name)


def _string_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExperimentValidationError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _string_value(value, name)


def _integer(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExperimentValidationError(f"{name} must be an integer")
    return value


def _optional_integer(payload: Mapping[str, object], name: str) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExperimentValidationError(f"{name} must be an integer or null")
    return value


def _boolean(payload: Mapping[str, object], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise ExperimentValidationError(f"{name} must be a boolean")
    return value


def _optional_boolean(payload: Mapping[str, object], name: str) -> bool | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ExperimentValidationError(f"{name} must be a boolean or null")
    return value


def _require_fields(
    payload: Mapping[str, object], expected: set[str], name: str
) -> None:
    missing = sorted(expected - set(payload))
    unknown = sorted(set(payload) - expected)
    if missing:
        raise ExperimentValidationError(f"{name} field is missing: {missing[0]}")
    if unknown:
        raise ExperimentValidationError(f"{name} field is unknown: {unknown[0]}")
