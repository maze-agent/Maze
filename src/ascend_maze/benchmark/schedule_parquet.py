"""Stable Parquet representation of a pre-materialized Trial schedule."""

from __future__ import annotations

from pathlib import Path

from ascend_maze.benchmark.canonical import canonical_json_digest
from ascend_maze.benchmark.persistence import atomic_write_bytes
from ascend_maze.benchmark.schedule import TrialSchedule
from ascend_maze.core.errors import ExperimentValidationError

SCHEDULE_SCHEMA_NAME = "ascend-maze.arrival-schedule.v1"


def write_schedule_parquet(path: Path, schedule: TrialSchedule) -> str:
    import pyarrow as pa
    import pyarrow.parquet as pq

    digest = canonical_json_digest(schedule.canonical_payload())
    entries = (*schedule.warmup, *schedule.measurement)
    schema = pa.schema(
        [
            pa.field("schema_version", pa.int32(), nullable=False),
            pa.field("trial_attempt_id", pa.string(), nullable=False),
            pa.field("mode", pa.string(), nullable=False),
            pa.field("phase", pa.string(), nullable=False),
            pa.field("arrival_index", pa.int64(), nullable=False),
            pa.field("scheduled_offset_ms", pa.int64(), nullable=True),
            pa.field("record_id", pa.string(), nullable=False),
            pa.field("input_digest", pa.string(), nullable=False),
            pa.field("submission_id", pa.string(), nullable=False),
        ],
        metadata={
            b"ascend_maze.schema": SCHEDULE_SCHEMA_NAME.encode("ascii"),
            b"ascend_maze.schema_version": b"1",
            b"ascend_maze.schedule_digest": digest.encode("ascii"),
        },
    )
    table = pa.Table.from_pylist(
        [
            {
                "schema_version": 1,
                "trial_attempt_id": schedule.trial_attempt_id,
                "mode": schedule.mode,
                **entry.canonical_payload(),
            }
            for entry in entries
        ],
        schema=schema,
    )
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd", version="2.6")
    atomic_write_bytes(path, sink.getvalue().to_pybytes())
    return digest


def validate_schedule_parquet(path: Path, expected: TrialSchedule) -> str:
    import pyarrow.parquet as pq

    expected_digest = canonical_json_digest(expected.canonical_payload())
    try:
        table = pq.read_table(path)
    except (OSError, ValueError) as exc:
        raise ExperimentValidationError(
            f"cannot read arrival schedule Parquet: {exc}"
        ) from exc
    metadata = table.schema.metadata or {}
    if metadata.get(b"ascend_maze.schema") != SCHEDULE_SCHEMA_NAME.encode("ascii"):
        raise ExperimentValidationError("arrival schedule Parquet schema is invalid")
    if metadata.get(b"ascend_maze.schema_version") != b"1":
        raise ExperimentValidationError("arrival schedule Parquet version is invalid")
    if metadata.get(b"ascend_maze.schedule_digest") != expected_digest.encode("ascii"):
        raise ExperimentValidationError("arrival schedule changed during resume")
    entries = (*expected.warmup, *expected.measurement)
    expected_rows = [
        {
            "schema_version": 1,
            "trial_attempt_id": expected.trial_attempt_id,
            "mode": expected.mode,
            **entry.canonical_payload(),
        }
        for entry in entries
    ]
    if table.to_pylist() != expected_rows:
        raise ExperimentValidationError("arrival schedule Parquet rows changed")
    return expected_digest
