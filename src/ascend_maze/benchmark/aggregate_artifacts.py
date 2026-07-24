"""Stable Parquet and CSV materialization for C14 aggregate products."""

from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
from typing import Mapping, Sequence

from ascend_maze.benchmark.persistence import atomic_write_bytes
from ascend_maze.core.errors import ExperimentValidationError


RUN_METRICS_SCHEMA = "ascend-maze.run-metrics.v1"
TRIAL_METRICS_SCHEMA = "ascend-maze.trial-metrics.v1"


def run_metrics_schema() -> object:
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("schema_version", pa.int32(), nullable=False),
            pa.field("study_id", pa.string(), nullable=False),
            pa.field("cell_id", pa.string(), nullable=False),
            pa.field("cell_name", pa.string(), nullable=False),
            pa.field("block_index", pa.int64(), nullable=False),
            pa.field("repetition_index", pa.int64(), nullable=False),
            pa.field("pairing_seed", pa.int64(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("trial_attempt_id", pa.string(), nullable=False),
            pa.field("trial_valid", pa.bool_(), nullable=False),
            pa.field("metric_name", pa.string(), nullable=False),
            pa.field("unit", pa.string(), nullable=False),
            pa.field("sample_index", pa.int64(), nullable=False),
            pa.field("run_id", pa.string(), nullable=True),
            pa.field("node_id", pa.string(), nullable=True),
            pa.field("device_id", pa.string(), nullable=True),
            pa.field("producer_id", pa.string(), nullable=True),
            pa.field("value", pa.float64(), nullable=True),
            pa.field("valid", pa.bool_(), nullable=False),
            pa.field("reason_codes", pa.list_(pa.string()), nullable=False),
        ],
        metadata={
            b"ascend_maze.schema": RUN_METRICS_SCHEMA.encode("ascii"),
            b"ascend_maze.schema_version": b"1",
            b"ascend_maze.quantile_method": b"hyndman_fan_type_7",
        },
    )


def trial_metrics_schema() -> object:
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("schema_version", pa.int32(), nullable=False),
            pa.field("study_id", pa.string(), nullable=False),
            pa.field("cell_id", pa.string(), nullable=False),
            pa.field("cell_name", pa.string(), nullable=False),
            pa.field("block_index", pa.int64(), nullable=False),
            pa.field("repetition_index", pa.int64(), nullable=False),
            pa.field("pairing_seed", pa.int64(), nullable=False),
            pa.field("trial_id", pa.string(), nullable=False),
            pa.field("trial_attempt_id", pa.string(), nullable=False),
            pa.field("trial_valid", pa.bool_(), nullable=False),
            pa.field("measurement_id", pa.string(), nullable=False),
            pa.field("metric_name", pa.string(), nullable=False),
            pa.field("unit", pa.string(), nullable=False),
            pa.field("higher_is_better", pa.bool_(), nullable=False),
            pa.field("metric_valid", pa.bool_(), nullable=False),
            pa.field("reason_codes", pa.list_(pa.string()), nullable=False),
            pa.field("sample_count", pa.int64(), nullable=False),
            pa.field("primary_value", pa.float64(), nullable=True),
            pa.field("mean", pa.float64(), nullable=True),
            pa.field("standard_deviation", pa.float64(), nullable=True),
            pa.field("mad", pa.float64(), nullable=True),
            pa.field("minimum", pa.float64(), nullable=True),
            pa.field("maximum", pa.float64(), nullable=True),
            pa.field("p50", pa.float64(), nullable=True),
            pa.field("p95", pa.float64(), nullable=True),
            pa.field("p99", pa.float64(), nullable=True),
            pa.field("p99_status", pa.string(), nullable=False),
        ],
        metadata={
            b"ascend_maze.schema": TRIAL_METRICS_SCHEMA.encode("ascii"),
            b"ascend_maze.schema_version": b"1",
            b"ascend_maze.quantile_method": b"hyndman_fan_type_7",
        },
    )


def write_run_metrics(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    _write_parquet(path, rows, run_metrics_schema())


def write_trial_metrics(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    _write_parquet(path, rows, trial_metrics_schema())


def read_run_metrics(path: Path) -> list[dict[str, object]]:
    return _read_parquet(path, RUN_METRICS_SCHEMA, run_metrics_schema())


def read_trial_metrics(path: Path) -> list[dict[str, object]]:
    return _read_parquet(path, TRIAL_METRICS_SCHEMA, trial_metrics_schema())


def write_csv(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    fieldnames: Sequence[str],
) -> None:
    stream = StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=fieldnames,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})
    atomic_write_bytes(path, stream.getvalue().encode("utf-8"))


def _write_parquet(
    path: Path, rows: Sequence[Mapping[str, object]], schema: object
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist([dict(row) for row in rows], schema=schema)
    sink = pa.BufferOutputStream()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )
    atomic_write_bytes(path, sink.getvalue().to_pybytes())


def _read_parquet(path: Path, schema_name: str, expected_schema: object) -> list[dict[str, object]]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(path)
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise ExperimentValidationError(f"cannot read aggregate Parquet: {path.name}") from exc
    metadata = table.schema.metadata or {}
    if metadata.get(b"ascend_maze.schema") != schema_name.encode("ascii"):
        raise ExperimentValidationError(f"aggregate Parquet schema is invalid: {path.name}")
    if table.schema != expected_schema:
        raise ExperimentValidationError(f"aggregate Parquet fields are invalid: {path.name}")
    return [dict(row) for row in table.to_pylist()]


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, float):
        return format(value, ".17g")
    if isinstance(value, (tuple, list)):
        return "|".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value
