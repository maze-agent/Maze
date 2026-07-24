"""Pure deterministic matrix expansion and Study planning."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import hashlib

from ascend_maze.benchmark.canonical import derive_seed, stable_payload_id, thaw
from ascend_maze.benchmark.contracts import (
    BENCHMARK_OVERRIDE_PATHS,
    INTERNAL_ABLATION_MATRIX,
    SCHEMA_VERSION,
    CellDefinition,
    CellSpec,
    ConfigDifference,
    ExperimentSpec,
    StudyPlan,
    TrialSpec,
)
from ascend_maze.benchmark.schema_registry import schema_digests
from ascend_maze.config import load_config
from ascend_maze.core.canonical import freeze_canonical
from ascend_maze.core.errors import ExperimentValidationError


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ExperimentValidationError(f"input file is unavailable: {path}") from exc
    return digest.hexdigest()


def build_study_plan(spec: ExperimentSpec) -> StudyPlan:
    base_path = Path(spec.base_config_path)
    if file_sha256(base_path) != spec.base_config_source_digest:
        raise ExperimentValidationError(
            "base_config_sha256: source changed after ExperimentSpec was loaded"
        )
    current_base = load_config(
        base_path,
        build_revision=spec.build_revision,
        created_at_ms=0,
    )
    if (
        current_base.snapshot.config_fingerprint
        != spec.base_config_snapshot.config_fingerprint
    ):
        raise ExperimentValidationError(
            "base ConfigSnapshot changed after ExperimentSpec was loaded"
        )
    for artifact in spec.workload.inputs:
        path = Path(artifact.source_path)
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            raise ExperimentValidationError(
                f"workload input is unavailable: {artifact.logical_name}"
            ) from exc
        if (
            size_bytes != artifact.size_bytes
            or file_sha256(path) != artifact.content_sha256
        ):
            raise ExperimentValidationError(
                f"workload input changed after ExperimentSpec was loaded: "
                f"{artifact.logical_name}"
            )
    _validate_internal_ablation(spec)
    base_resolved = _plain_mapping(current_base.snapshot.resolved, "base config")
    cells = tuple(
        _build_cell(spec, definition, base_resolved) for definition in spec.matrix.cells
    )
    if len({cell.cell_id for cell in cells}) != len(cells):
        raise ExperimentValidationError(
            "matrix contains two Cells with the same relative factor payload"
        )
    trials = _build_trials(spec, cells)
    return StudyPlan(
        schema_version=SCHEMA_VERSION,
        spec=spec,
        cells=cells,
        trials=trials,
        schema_digests=schema_digests(),
    )


def _build_cell(
    spec: ExperimentSpec,
    definition: CellDefinition,
    base_resolved: Mapping[str, object],
) -> CellSpec:
    override_pairs = tuple(
        (item.path, thaw(item.value))
        for item in definition.overrides
        if item.path not in BENCHMARK_OVERRIDE_PATHS
    )
    loaded = load_config(
        spec.base_config_path,
        build_revision=spec.build_revision,
        created_at_ms=0,
        config_overrides=override_pairs,
    )
    resolved = _plain_mapping(loaded.snapshot.resolved, f"cell {definition.name}")
    differences = _config_differences(base_resolved, resolved) + tuple(
        ConfigDifference(
            path=item.path,
            before=None,
            after=item.value,
        )
        for item in definition.overrides
        if item.path in BENCHMARK_OVERRIDE_PATHS
    )
    differences = tuple(sorted(differences, key=lambda item: item.path))
    expected_paths = {item.path for item in definition.overrides}
    actual_paths = {item.path for item in differences}
    if expected_paths != actual_paths:
        missing = sorted(expected_paths - actual_paths)
        hidden = sorted(actual_paths - expected_paths)
        details: list[str] = []
        if missing:
            details.append("ineffective overrides=" + ",".join(missing))
        if hidden:
            details.append("hidden differences=" + ",".join(hidden))
        raise ExperimentValidationError(
            f"matrix cell {definition.name}: config diff does not match overrides: "
            + "; ".join(details)
        )
    factor_payload = {
        "matrix_kind": spec.matrix.kind,
        "factors": definition.factors,
        "overrides": [item.canonical_payload() for item in definition.overrides],
    }
    return CellSpec(
        name=definition.name,
        cell_id=stable_payload_id("cell", factor_payload, length=32),
        factors=definition.factors,
        overrides=definition.overrides,
        differences=differences,
        config_snapshot=loaded.snapshot,
        confirmatory=definition.confirmatory,
    )


def _build_trials(
    spec: ExperimentSpec, cells: tuple[CellSpec, ...]
) -> tuple[TrialSpec, ...]:
    result: list[TrialSpec] = []
    for block_index in range(spec.block_count):
        for repetition_index in range(spec.repetition_count):
            pairing_seed = derive_seed(
                spec.base_seed,
                spec.study_id,
                "pairing",
                block_index,
                repetition_index,
            )
            ordered = sorted(
                cells,
                key=lambda cell: (
                    derive_seed(pairing_seed, "cell_order", cell.cell_id),
                    cell.cell_id,
                ),
            )
            for position, cell in enumerate(ordered):
                trial_seed = derive_seed(
                    spec.base_seed,
                    spec.study_id,
                    cell.cell_id,
                    block_index,
                    repetition_index,
                )
                identity_payload = {
                    "study_id": spec.study_id,
                    "cell_id": cell.cell_id,
                    "block_index": block_index,
                    "repetition_index": repetition_index,
                    "trial_seed": trial_seed,
                }
                result.append(
                    TrialSpec(
                        trial_id=stable_payload_id(
                            "trial", identity_payload, length=32
                        ),
                        study_id=spec.study_id,
                        cell_id=cell.cell_id,
                        cell_name=cell.name,
                        block_index=block_index,
                        repetition_index=repetition_index,
                        position_in_block=position,
                        trial_seed=trial_seed,
                        pairing_seed=pairing_seed,
                    )
                )
    return tuple(result)


def _plain_mapping(value: object, name: str) -> Mapping[str, object]:
    plain = thaw(value)
    if not isinstance(plain, dict) or any(not isinstance(key, str) for key in plain):
        raise ExperimentValidationError(
            f"{name}: resolved snapshot is not a string map"
        )
    return plain


def _config_differences(
    before: Mapping[str, object], after: Mapping[str, object]
) -> tuple[ConfigDifference, ...]:
    raw: list[tuple[str, object, object]] = []
    _walk_differences(before, after, "", raw)
    return tuple(
        ConfigDifference(
            path=path,
            before=freeze_canonical(old),
            after=freeze_canonical(new),
        )
        for path, old, new in sorted(raw, key=lambda item: item[0])
    )


def _walk_differences(
    before: object,
    after: object,
    prefix: str,
    output: list[tuple[str, object, object]],
) -> None:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        keys = sorted(set(before) | set(after))
        for key in keys:
            if not isinstance(key, str):
                raise ExperimentValidationError("config snapshots require string keys")
            path = f"{prefix}.{key}" if prefix else key
            if key not in before:
                output.append((path, None, after[key]))
            elif key not in after:
                output.append((path, before[key], None))
            else:
                _walk_differences(before[key], after[key], path, output)
        return
    if before != after:
        if not prefix:
            raise ExperimentValidationError("config snapshot root changed type")
        output.append((prefix, before, after))


def _validate_internal_ablation(spec: ExperimentSpec) -> None:
    if spec.matrix.kind != INTERNAL_ABLATION_MATRIX:
        return
    expected_factors = {
        "anchor": ("placement.anchor_strategy",),
        "ordering": ("scheduler.policy",),
        "partitioner": ("scheduler.partitioner",),
        "worker_mode": (
            "worker.standby_max_idle",
            "worker.standby_min_idle",
        ),
    }
    actual_factors = {item.name: item.allowed_paths for item in spec.matrix.factors}
    if actual_factors != expected_factors:
        raise ExperimentValidationError(
            "internal_ablation_v1: factor names and allowed paths are frozen"
        )
    expected_cells: dict[str, tuple[tuple[str, ...], dict[str, object]]] = {
        "maze_full": ((), {}),
        "fcfs": (("ordering",), {"scheduler.policy": "fcfs"}),
        "no_resource_anchor": (
            ("anchor",),
            {"placement.anchor_strategy": "declared_only"},
        ),
        "no_heterogeneous_queue": (
            ("partitioner",),
            {"scheduler.partitioner": "unified"},
        ),
        "no_standby": (
            ("worker_mode",),
            {"worker.standby_min_idle": 0, "worker.standby_max_idle": 0},
        ),
    }
    actual_cells = {
        cell.name: (
            cell.factors,
            {item.path: thaw(item.value) for item in cell.overrides},
        )
        for cell in spec.matrix.cells
    }
    if spec.matrix.baseline_cell != "maze_full" or actual_cells != expected_cells:
        raise ExperimentValidationError(
            "internal_ablation_v1: the five Cell definitions are frozen"
        )
    resolved = _plain_mapping(spec.base_config_snapshot.resolved, "base config")
    scheduler = _nested_mapping(resolved, "scheduler")
    placement = _nested_mapping(resolved, "placement")
    worker = _nested_mapping(resolved, "worker")
    expected_base = {
        "scheduler.policy": (scheduler.get("policy"), "hacs_no_tp"),
        "scheduler.partitioner": (
            scheduler.get("partitioner"),
            "heterogeneous",
        ),
        "placement.anchor_strategy": (
            placement.get("anchor_strategy"),
            "static",
        ),
    }
    invalid = sorted(
        path for path, (actual, expected) in expected_base.items() if actual != expected
    )
    min_idle = worker.get("standby_min_idle")
    max_idle = worker.get("standby_max_idle")
    if (
        isinstance(min_idle, bool)
        or not isinstance(min_idle, int)
        or min_idle < 1
        or isinstance(max_idle, bool)
        or not isinstance(max_idle, int)
        or max_idle < min_idle
    ):
        invalid.append("worker standby watermarks")
    if invalid:
        raise ExperimentValidationError(
            "internal_ablation_v1: base config is not maze_full: " + ", ".join(invalid)
        )


def _nested_mapping(root: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = root.get(key)
    if not isinstance(value, Mapping):
        raise ExperimentValidationError(f"base config is missing table: {key}")
    return value
