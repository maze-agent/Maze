"""C14E Qwen3-4B pilot and formal Study specification generation."""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
from typing import Iterable

from ascend_maze.benchmark.persistence import atomic_write_bytes
from ascend_maze.benchmark.planning import file_sha256
from ascend_maze.benchmark.workloads.qwen3_4b import build
from ascend_maze.config import load_config
from ascend_maze.core.errors import ExperimentValidationError

QWEN3_4B_MODEL_DIGEST = (
    "89074cf96df6136182c2ec9e458d44f640dfd96073813dfe46d0c9c060f41f91"
)
DEFAULT_C14E_RATES = (4.0, 8.0, 12.0)


def prepare_c14e_specs(
    *,
    base_config: str | Path,
    output_directory: str | Path,
    study_kind: str,
    rates: Iterable[float] = DEFAULT_C14E_RATES,
) -> tuple[Path, ...]:
    if study_kind not in {"pilot", "formal"}:
        raise ExperimentValidationError("C14E study_kind must be pilot or formal")
    normalized_rates = tuple(float(value) for value in rates)
    if (
        len(normalized_rates) < 3
        or any(value <= 0 for value in normalized_rates)
        or len(set(normalized_rates)) != len(normalized_rates)
    ):
        raise ExperimentValidationError(
            "C14E requires at least three distinct positive load rates"
        )
    config = Path(base_config).expanduser().resolve(strict=True)
    output = Path(output_directory).expanduser().resolve(strict=False)
    repository = _repository_root(config.parent)
    build_revision = _clean_build_revision(repository)
    loaded = load_config(config, build_revision=build_revision, created_at_ms=0)
    workflow_fingerprint = build().compile().workflow_fingerprint
    dataset = repository / "experiments" / "c14e" / "qwen3_4b_dataset.json"
    _validate_dataset_fingerprint(dataset, workflow_fingerprint)
    block_count = 3 if study_kind == "pilot" else 10
    measurement_duration_ms = 20_000 if study_kind == "pilot" else 30_000
    warmup_duration_ms = 10_000
    drain_deadline_ms = 240_000
    paths: list[Path] = []
    for rate in normalized_rates:
        rate_name = _rate_name(rate)
        spec = _render_spec(
            study_kind=study_kind,
            rate=rate,
            rate_name=rate_name,
            block_count=block_count,
            warmup_duration_ms=warmup_duration_ms,
            measurement_duration_ms=measurement_duration_ms,
            drain_deadline_ms=drain_deadline_ms,
            build_revision=build_revision,
            base_config=config,
            base_config_sha256=file_sha256(config),
            dataset=dataset,
            dataset_sha256=file_sha256(dataset),
            dataset_size=dataset.stat().st_size,
            workflow_fingerprint=workflow_fingerprint,
            model_catalog_revision=loaded.snapshot.model_catalog_revision,
            environment_fingerprint=loaded.config.cluster.environment_fingerprint,
        )
        path = output / f"{study_kind}-rate-{rate_name}.toml"
        atomic_write_bytes(path, spec.encode("ascii"))
        paths.append(path)
    return tuple(paths)


def _render_spec(
    *,
    study_kind: str,
    rate: float,
    rate_name: str,
    block_count: int,
    warmup_duration_ms: int,
    measurement_duration_ms: int,
    drain_deadline_ms: int,
    build_revision: str,
    base_config: Path,
    base_config_sha256: str,
    dataset: Path,
    dataset_sha256: str,
    dataset_size: int,
    workflow_fingerprint: str,
    model_catalog_revision: str,
    environment_fingerprint: str,
) -> str:
    lines = [
        "schema_version = 1",
        f'study_name = "c14e-qwen3-4b-{study_kind}-rate-{rate_name}"',
        f'study_kind = "{study_kind}"',
        "base_seed = 1405001",
        f"block_count = {block_count}",
        "repetition_count = 1",
        f'build_revision = "{build_revision}"',
        f'base_config = "{base_config}"',
        f'base_config_sha256 = "{base_config_sha256}"',
        "",
        "[workload]",
        'name = "qwen3-4b-service"',
        'workflow_factory = "ascend_maze.benchmark.workloads.qwen3_4b:build"',
        f'workflow_fingerprint = "{workflow_fingerprint}"',
        f'model_catalog_revision = "{model_catalog_revision}"',
        f'model_artifact_digest = "{QWEN3_4B_MODEL_DIGEST}"',
        f'required_environment_fingerprint = "{environment_fingerprint}"',
        "",
        "[[workload.inputs]]",
        'logical_name = "dataset"',
        f'path = "{dataset}"',
        f'sha256 = "{dataset_sha256}"',
        f"size_bytes = {dataset_size}",
        "",
        "[arrival]",
        'mode = "poisson"',
        f"rate_per_second = {rate!r}",
        "",
        "[windows]",
        "warmup_runs = 0",
        f"warmup_duration_ms = {warmup_duration_ms}",
        "measurement_run_count = 0",
        f"measurement_duration_ms = {measurement_duration_ms}",
        f"drain_deadline_ms = {drain_deadline_ms}",
        "",
        "[analysis]",
        "metric_set = [",
        '  "active_lease_count",',
        '  "arrival_lateness_ms",',
        '  "dct_ms",',
        '  "device_hbm_free_mb",',
        '  "device_hbm_used_mb",',
        '  "inference_engine_queue_depth",',
        '  "inference_queue_ms",',
        '  "inference_token_throughput_per_s",',
        '  "model_cold_start_ms",',
        '  "queue_ms",',
        '  "scheduler_placement_ms",',
        '  "scheduler_policy_select_ms",',
        '  "scheduler_score_ms",',
        '  "scheduler_total_ms",',
        '  "steady_state_throughput_success_per_s",',
        '  "throughput_success_per_s",',
        '  "throughput_terminal_per_s",',
        '  "ttft_ms",',
        '  "worker_acquire_ms",',
        '  "worker_cold_start_ms",',
        '  "worker_standby_hit_rate",',
        "]",
        'validity_policy = "c14_v1"',
        'statistics_policy = "c14_v1"',
        'performance_budget_set = "c14_v1"',
        'quantile_method = "hyndman_fan_type_7"',
        "bootstrap_samples = 10000",
        "confidence_level = 0.95",
        "familywise_confidence_level = 0.9875",
        "automatic_outlier_removal = false",
        "",
        "[matrix]",
        'kind = "internal_ablation_v1"',
        'baseline_cell = "maze_full"',
        "",
        "[[matrix.factors]]",
        'name = "ordering"',
        'allowed_paths = ["scheduler.policy"]',
        "",
        "[[matrix.factors]]",
        'name = "anchor"',
        'allowed_paths = ["placement.anchor_strategy"]',
        "",
        "[[matrix.factors]]",
        'name = "partitioner"',
        'allowed_paths = ["scheduler.partitioner"]',
        "",
        "[[matrix.factors]]",
        'name = "worker_mode"',
        'allowed_paths = ["worker.standby_min_idle", "worker.standby_max_idle"]',
        "",
        "[[matrix.cells]]",
        'name = "maze_full"',
        "factors = []",
        "confirmatory = true",
        "",
        "[[matrix.cells]]",
        'name = "fcfs"',
        'factors = ["ordering"]',
        "confirmatory = true",
        "",
        "[[matrix.cells.overrides]]",
        'path = "scheduler.policy"',
        'value = "fcfs"',
        "",
        "[[matrix.cells]]",
        'name = "no_resource_anchor"',
        'factors = ["anchor"]',
        "confirmatory = true",
        "",
        "[[matrix.cells.overrides]]",
        'path = "placement.anchor_strategy"',
        'value = "declared_only"',
        "",
        "[[matrix.cells]]",
        'name = "no_heterogeneous_queue"',
        'factors = ["partitioner"]',
        "confirmatory = true",
        "",
        "[[matrix.cells.overrides]]",
        'path = "scheduler.partitioner"',
        'value = "unified"',
        "",
        "[[matrix.cells]]",
        'name = "no_standby"',
        'factors = ["worker_mode"]',
        "confirmatory = true",
        "",
        "[[matrix.cells.overrides]]",
        'path = "worker.standby_min_idle"',
        "value = 0",
        "",
        "[[matrix.cells.overrides]]",
        'path = "worker.standby_max_idle"',
        "value = 0",
    ]
    return "\n".join(lines) + "\n"


def _validate_dataset_fingerprint(path: Path, fingerprint: str) -> None:
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentValidationError(f"C14E dataset is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("workflow_fingerprint") != fingerprint:
        raise ExperimentValidationError(
            "C14E dataset workflow fingerprint does not match the source workload"
        )


def _rate_name(rate: float) -> str:
    rendered = format(rate, ".6g").replace(".", "p")
    if rendered.startswith("-"):
        raise ExperimentValidationError("C14E load rate must be positive")
    return rendered


def _repository_root(start: Path) -> Path:
    return Path(
        _git_output(start, "rev-parse", "--show-toplevel")
    ).resolve(strict=True)


def _clean_build_revision(repository: Path) -> str:
    status = _git_output(
        repository, "status", "--porcelain", "--untracked-files=no"
    )
    if status.strip():
        raise ExperimentValidationError(
            "tracked worktree must be clean before C14E specs are frozen"
        )
    revision = _git_output(repository, "rev-parse", "HEAD")
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ExperimentValidationError("current Git revision is invalid")
    return revision


def _git_output(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExperimentValidationError(f"cannot inspect Git source: {exc}") from exc
    return completed.stdout.strip()


def spec_bundle_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        digest.update(path.name.encode("ascii"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()
