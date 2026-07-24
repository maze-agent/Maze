"""Stage-5D mainline and one-switch ablations on the shared execution path."""

from __future__ import annotations

from dataclasses import dataclass

from ascend_maze.ascend.contracts import (
    AscendColocationConfig,
    AscendEnvironmentSnapshot,
)
from ascend_maze.contracts.config import ConfigSnapshot
from ascend_maze.contracts.recording import (
    ExecutionRecorder,
    ParquetRecorderConfig,
)
from ascend_maze.contracts.resources import ReservationVector
from ascend_maze.contracts.worker import (
    WarmupManifest,
    WorkerPoolConfig,
    WorkerPoolProfileConfig,
    WorkerProfile,
)
from ascend_maze.core.clock import Clock
from ascend_maze.core.errors import ContractValidationError
from ascend_maze.recording import NoopRecorder, ParquetRecorder
from ascend_maze.resources import (
    DeclaredOnlyAnchorProvider,
    ResourceAnchorProvider,
    StaticAnchorProvider,
)
from ascend_maze.scheduler import (
    FcfsPolicy,
    HacsConfig,
    HacsNoTpStaticPolicy,
    HeterogeneousPartitioner,
    QueuePartitioner,
    SchedulingPolicy,
    UnifiedPartitioner,
)


@dataclass(frozen=True, slots=True)
class Stage5DConfig:
    """Every stage-5D ablation changes one field of this immutable profile."""

    policy: str = "hacs_no_tp"
    anchor: str = "static"
    partitioner: str = "heterogeneous"
    standby_enabled: bool = True
    recording_backend: str = "parquet"
    recorder: ParquetRecorderConfig | None = None
    hacs: HacsConfig = HacsConfig()
    task_slots_total: int = 2
    allow_colocation: bool = True
    standby_min_idle: int = 2
    npu_system_reserved_hbm_mb: int = 4_096
    npu_hbm_headroom_mb: int = 1_024
    host_mem_headroom_mb: int = 1_024
    io_slots_total: int = 8
    worker_binding_deadline_ms: int = 30_000
    hbm_recovery_deadline_ms: int = 30_000
    hbm_recovery_tolerance_mb: int = 64
    worker_max_total: int = 64
    worker_acquire_timeout_ms: int = 30_000
    worker_max_lifetime_ms: int = 120_000
    worker_max_rss_growth_mb: int = 256
    worker_termination_timeout_ms: int = 30_000
    worker_reconcile_interval_ms: int = 250
    worker_config_generation: int = 1

    def __post_init__(self) -> None:
        if self.policy not in {"hacs_no_tp", "fcfs"}:
            raise ContractValidationError("unsupported stage-5D policy")
        if self.anchor not in {"static", "declared_only"}:
            raise ContractValidationError("unsupported stage-5D anchor")
        if self.partitioner not in {"heterogeneous", "unified"}:
            raise ContractValidationError("unsupported stage-5D partitioner")
        if self.recording_backend not in {"parquet", "noop"}:
            raise ContractValidationError("unsupported stage-5D recording backend")
        if self.recording_backend == "parquet" and self.recorder is None:
            raise ContractValidationError("Parquet recording requires recorder config")
        if self.recorder is not None and not isinstance(
            self.recorder, ParquetRecorderConfig
        ):
            raise ContractValidationError("recorder must be ParquetRecorderConfig")
        if not isinstance(self.standby_enabled, bool):
            raise ContractValidationError("standby_enabled must be a boolean")
        AscendColocationConfig(
            anchor_strategy=self.anchor,
            scheduler_policy=self.policy,
            task_slots_total=self.task_slots_total,
            allow_colocation=self.allow_colocation,
            max_tasks_per_worker=1,
            standby_min_idle=self.standby_min_idle,
            npu_system_reserved_hbm_mb=self.npu_system_reserved_hbm_mb,
            npu_hbm_headroom_mb=self.npu_hbm_headroom_mb,
            host_mem_headroom_mb=self.host_mem_headroom_mb,
            io_slots_total=self.io_slots_total,
            worker_binding_deadline_ms=self.worker_binding_deadline_ms,
            hbm_recovery_deadline_ms=self.hbm_recovery_deadline_ms,
            hbm_recovery_tolerance_mb=self.hbm_recovery_tolerance_mb,
        )
        if self.standby_enabled and self.standby_min_idle < 1:
            raise ContractValidationError(
                "enabled Standby requires a positive idle watermark"
            )
        for name in (
            "worker_max_total",
            "worker_acquire_timeout_ms",
            "worker_max_lifetime_ms",
            "worker_max_rss_growth_mb",
            "worker_termination_timeout_ms",
            "worker_reconcile_interval_ms",
            "worker_config_generation",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractValidationError(f"{name} must be positive")
        if self.standby_min_idle > self.worker_max_total:
            raise ContractValidationError("standby_min_idle exceeds worker_max_total")


@dataclass(frozen=True, slots=True)
class Stage5DComponents:
    snapshot: ConfigSnapshot
    policy: SchedulingPolicy
    anchors: ResourceAnchorProvider
    partitioner: QueuePartitioner
    worker_pool: WorkerPoolConfig
    recorder: ExecutionRecorder


def _worker_pool_config(config: Stage5DConfig) -> WorkerPoolConfig:
    min_idle = config.standby_min_idle if config.standby_enabled else 0
    mode = "zero_hbm_standby" if config.standby_enabled else "cold_start"
    profiles = tuple(
        WorkerPoolProfileConfig(
            profile=profile,
            min_idle=min_idle,
            max_idle=min_idle,
            max_total=config.worker_max_total,
            replenish_concurrency=1,
            idle_ttl_ms=60_000,
            acquire_timeout_ms=config.worker_acquire_timeout_ms,
            max_tasks_per_worker=1,
            max_worker_lifetime_ms=config.worker_max_lifetime_ms,
            max_rss_growth_mb=config.worker_max_rss_growth_mb,
            standby_resources=ReservationVector(
                cpu_num=1,
                host_mem_mb=256,
                io_slots=1 if profile is WorkerProfile.IO else 0,
                npu_hbm_mb=0,
                npu_slots=0,
            ),
            termination_timeout_ms=config.worker_termination_timeout_ms,
            warmup_manifest=WarmupManifest(("json",)),
        )
        for profile in (WorkerProfile.CPU, WorkerProfile.IO, WorkerProfile.NPU_HOST)
    )
    return WorkerPoolConfig(
        mode=mode,
        profiles=profiles,
        reconcile_interval_ms=config.worker_reconcile_interval_ms,
        config_generation=config.worker_config_generation,
    )


def _hacs_payload(config: HacsConfig) -> dict[str, float]:
    return {
        "alpha": config.alpha,
        "beta": config.beta,
        "initial_avg_dct_seconds": config.initial_avg_dct_seconds,
        "dct_ema_gamma": config.dct_ema_gamma,
        "t_pred": config.t_pred,
    }


def create_stage5d_config_snapshot(
    config: Stage5DConfig,
    environment: AscendEnvironmentSnapshot,
    *,
    source_path: str,
    build_revision: str,
    project_version: str = "0.1.0",
    model_catalog_revision: str = "stage5d-no-model-catalog",
    created_at_ms: int | None = None,
) -> ConfigSnapshot:
    worker_pool = _worker_pool_config(config)
    recording: dict[str, object]
    if config.recording_backend == "noop":
        recording = {"backend": "noop"}
    else:
        assert config.recorder is not None
        recording = config.recorder.canonical_payload()
    resolved = {
        "profile": "stage5d",
        "environment_fingerprint": environment.environment_fingerprint,
        "scheduler": {
            "policy": config.policy,
            "policy_version": "1",
            "hacs": _hacs_payload(config.hacs),
        },
        "anchor": {"strategy": config.anchor},
        "queue": {"partitioner": config.partitioner},
        "placement": {
            "task_slots_total": config.task_slots_total,
            "allow_colocation": config.allow_colocation,
            "npu_system_reserved_hbm_mb": config.npu_system_reserved_hbm_mb,
            "npu_hbm_headroom_mb": config.npu_hbm_headroom_mb,
            "host_mem_headroom_mb": config.host_mem_headroom_mb,
            "io_slots_total": config.io_slots_total,
        },
        "worker": {
            "max_tasks_per_worker": 1,
            "binding_deadline_ms": config.worker_binding_deadline_ms,
        },
        "worker_pool": {
            "standby_enabled": config.standby_enabled,
            **worker_pool.canonical_payload(),
        },
        "recording": recording,
        "observation": {
            "device_metrics_attribution": "device_only",
            "process_hbm_attribution": "attempt",
        },
        "cleanup": {
            "hbm_recovery_deadline_ms": config.hbm_recovery_deadline_ms,
            "hbm_recovery_tolerance_mb": config.hbm_recovery_tolerance_mb,
        },
    }
    return ConfigSnapshot.create(
        schema_version=1,
        project_version=project_version,
        source_path=source_path,
        resolved=resolved,
        model_catalog_revision=model_catalog_revision,
        build_revision=build_revision,
        runtime_versions=dict(environment.versions),
        created_at_ms=created_at_ms,
    )


def build_stage5d_recorder(
    config: Stage5DConfig,
    *,
    cursor_signing_key: bytes | None = None,
) -> ExecutionRecorder:
    if config.recording_backend == "noop":
        return NoopRecorder()
    assert config.recorder is not None
    return ParquetRecorder(
        config.recorder,
        cursor_signing_key=cursor_signing_key,
    )


def build_stage5d_components(
    config: Stage5DConfig,
    environment: AscendEnvironmentSnapshot,
    *,
    source_path: str,
    build_revision: str,
    clock: Clock | None = None,
    cursor_signing_key: bytes | None = None,
    created_at_ms: int | None = None,
) -> Stage5DComponents:
    policy: SchedulingPolicy = (
        HacsNoTpStaticPolicy(clock=clock, config=config.hacs)
        if config.policy == "hacs_no_tp"
        else FcfsPolicy()
    )
    anchors: ResourceAnchorProvider = (
        StaticAnchorProvider(
            environment_fingerprint=environment.environment_fingerprint
        )
        if config.anchor == "static"
        else DeclaredOnlyAnchorProvider(
            environment_fingerprint=environment.environment_fingerprint
        )
    )
    partitioner: QueuePartitioner = (
        HeterogeneousPartitioner()
        if config.partitioner == "heterogeneous"
        else UnifiedPartitioner()
    )
    return Stage5DComponents(
        snapshot=create_stage5d_config_snapshot(
            config,
            environment,
            source_path=source_path,
            build_revision=build_revision,
            created_at_ms=created_at_ms,
        ),
        policy=policy,
        anchors=anchors,
        partitioner=partitioner,
        worker_pool=_worker_pool_config(config),
        recorder=build_stage5d_recorder(
            config, cursor_signing_key=cursor_signing_key
        ),
    )
