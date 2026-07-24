"""Deterministic C14 arrival schedule materialization."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math

from ascend_maze.benchmark.canonical import derive_seed, stable_payload_id
from ascend_maze.benchmark.contracts import ArrivalSpec, ExperimentSpec, TrialSpec
from ascend_maze.benchmark.workload import (
    TraceSchedule,
    WorkloadDataset,
    WorkloadRecord,
)
from ascend_maze.core.errors import ExperimentValidationError

MAX_SCHEDULE_ENTRIES = 1_000_000


@dataclass(frozen=True, slots=True)
class ArrivalEntry:
    phase: str
    arrival_index: int
    scheduled_offset_ms: int | None
    record_id: str
    input_digest: str
    submission_id: str

    def __post_init__(self) -> None:
        if self.phase not in {"warmup", "measurement"}:
            raise ExperimentValidationError("arrival phase is invalid")
        if self.arrival_index < 0:
            raise ExperimentValidationError("arrival index must be non-negative")
        if self.scheduled_offset_ms is not None and self.scheduled_offset_ms < 0:
            raise ExperimentValidationError("arrival offset must be non-negative")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "arrival_index": self.arrival_index,
            "scheduled_offset_ms": self.scheduled_offset_ms,
            "record_id": self.record_id,
            "input_digest": self.input_digest,
            "submission_id": self.submission_id,
        }


@dataclass(frozen=True, slots=True)
class TrialSchedule:
    schema_version: int
    trial_attempt_id: str
    mode: str
    warmup: tuple[ArrivalEntry, ...]
    measurement: tuple[ArrivalEntry, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ExperimentValidationError(
                "TrialSchedule schema version is unsupported"
            )
        if not self.measurement:
            raise ExperimentValidationError("measurement schedule cannot be empty")
        identifiers = [item.submission_id for item in (*self.warmup, *self.measurement)]
        if len(identifiers) != len(set(identifiers)):
            raise ExperimentValidationError("schedule submission IDs must be unique")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "schema": "ascend-maze.arrival-schedule.v1",
            "trial_attempt_id": self.trial_attempt_id,
            "mode": self.mode,
            "warmup": [item.canonical_payload() for item in self.warmup],
            "measurement": [item.canonical_payload() for item in self.measurement],
        }


def materialize_trial_schedule(
    spec: ExperimentSpec,
    trial: TrialSpec,
    trial_attempt_id: str,
    dataset: WorkloadDataset,
    *,
    trace: TraceSchedule | None = None,
) -> TrialSchedule:
    if spec.arrival.mode == "trace_replay" and trace is None:
        raise ExperimentValidationError("trace_replay requires a trace schedule")
    if spec.arrival.mode != "trace_replay" and trace is not None:
        raise ExperimentValidationError("trace schedule is only valid for trace_replay")
    warmup_offsets: tuple[int | None, ...]
    if spec.windows.warmup_runs:
        warmup_offsets = (None,) * spec.windows.warmup_runs
    elif spec.windows.warmup_duration_ms:
        if spec.arrival.mode == "closed_loop":
            raise ExperimentValidationError(
                "closed_loop requires count-based warmup in schema version 1"
            )
        warmup_offsets = tuple(
            _open_offsets(
                spec.arrival,
                spec.windows.warmup_duration_ms,
                derive_seed(trial.pairing_seed, "warmup_arrivals"),
                trace,
            )
        )
    else:
        warmup_offsets = ()
    if spec.arrival.mode == "closed_loop":
        measurement_offsets: tuple[int | None, ...] = (
            None,
        ) * spec.windows.measurement_run_count
    else:
        measurement_offsets = tuple(
            _open_offsets(
                spec.arrival,
                spec.windows.measurement_duration_ms,
                derive_seed(trial.pairing_seed, "measurement_arrivals"),
                trace,
            )
        )
    return TrialSchedule(
        schema_version=1,
        trial_attempt_id=trial_attempt_id,
        mode=spec.arrival.mode,
        warmup=_entries(
            "warmup",
            warmup_offsets,
            dataset.records,
            trial.pairing_seed,
            trial_attempt_id,
        ),
        measurement=_entries(
            "measurement",
            measurement_offsets,
            dataset.records,
            trial.pairing_seed,
            trial_attempt_id,
        ),
    )


def catch_up_spacing_ms(arrival: ArrivalSpec) -> int:
    if arrival.mode not in {"fixed_rate", "poisson"}:
        return 0
    assert arrival.rate_per_second is not None
    return max(1, int(1_000 / arrival.rate_per_second))


def _open_offsets(
    arrival: ArrivalSpec,
    duration_ms: int,
    seed: int,
    trace: TraceSchedule | None,
) -> tuple[int, ...]:
    if duration_ms <= 0:
        raise ExperimentValidationError("open arrival duration must be positive")
    if arrival.mode == "trace_replay":
        assert trace is not None
        trace_offsets = tuple(
            value for value in trace.offsets_ms if value < duration_ms
        )
        if len(trace_offsets) != len(trace.offsets_ms):
            raise ExperimentValidationError(
                "trace offsets must all fall inside the measurement window"
            )
        return trace_offsets
    if arrival.mode == "fixed_rate":
        assert arrival.rate_per_second is not None
        interval = Fraction(1_000, 1) / Fraction(str(arrival.rate_per_second))
        ratio = Fraction(duration_ms, 1) / interval
        count = _bounded_count(
            (ratio.numerator + ratio.denominator - 1) // ratio.denominator
        )
        return tuple(int(index * interval) for index in range(count))
    if arrival.mode == "poisson":
        assert arrival.rate_per_second is not None
        poisson_offsets: list[int] = []
        elapsed = 0.0
        index = 0
        while True:
            random_bits = derive_seed(seed, "poisson_interval", index)
            uniform = (random_bits + 1) / ((1 << 63) + 1)
            elapsed += -math.log1p(-uniform) * 1_000 / arrival.rate_per_second
            offset = int(elapsed)
            if offset >= duration_ms:
                break
            poisson_offsets.append(offset)
            index += 1
            _bounded_count(index)
        return tuple(poisson_offsets)
    raise ExperimentValidationError("closed_loop has no precomputed open offsets")


def _bounded_count(count: int) -> int:
    if count < 1:
        raise ExperimentValidationError("arrival schedule cannot be empty")
    if count > MAX_SCHEDULE_ENTRIES:
        raise ExperimentValidationError(
            f"arrival schedule exceeds {MAX_SCHEDULE_ENTRIES} entries"
        )
    return count


def _entries(
    phase: str,
    offsets: tuple[int | None, ...],
    records: tuple[WorkloadRecord, ...],
    pairing_seed: int,
    trial_attempt_id: str,
) -> tuple[ArrivalEntry, ...]:
    if len(offsets) > MAX_SCHEDULE_ENTRIES:
        raise ExperimentValidationError("arrival schedule is too large")
    selected = _input_sequence(records, len(offsets), pairing_seed, phase)
    return tuple(
        ArrivalEntry(
            phase=phase,
            arrival_index=index,
            scheduled_offset_ms=offset,
            record_id=record.record_id,
            input_digest=record.input_digest,
            submission_id=stable_payload_id(
                "submission",
                {
                    "trial_attempt_id": trial_attempt_id,
                    "phase": phase,
                    "arrival_index": index,
                },
                length=32,
            ),
        )
        for index, (offset, record) in enumerate(zip(offsets, selected, strict=True))
    )


def _input_sequence(
    records: tuple[WorkloadRecord, ...],
    count: int,
    pairing_seed: int,
    phase: str,
) -> tuple[WorkloadRecord, ...]:
    result: list[WorkloadRecord] = []
    epoch = 0
    while len(result) < count:
        ordered = sorted(
            records,
            key=lambda record: (
                derive_seed(pairing_seed, f"{phase}_inputs", epoch, record.record_id),
                record.record_id,
            ),
        )
        result.extend(ordered[: count - len(result)])
        epoch += 1
    return tuple(result)
