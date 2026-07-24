"""Typed C13 configuration schema and cross-component validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ascend_maze.core.errors import ContractValidationError


def _positive(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError(f"{name}: must be a positive integer")
    return value


def _non_negative(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{name}: must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class ControlConfig:
    socket_path: str
    runtime_directory: str
    pid_file: str
    cluster_token_file: str
    recovery_path: str
    node_rpc_bind_address: str = "127.0.0.1:0"
    node_rpc_advertised_host: str | None = None
    shutdown_drain_timeout_ms: int = 5_000
    shutdown_cleanup_timeout_ms: int = 30_000
    watch_retention_count: int = 10_000
    max_inline_control_bytes: int = 1_048_576
    max_inline_result_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        for name in (
            "socket_path",
            "runtime_directory",
            "pid_file",
            "cluster_token_file",
            "recovery_path",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"control.{name}: path is required")
        if not isinstance(self.node_rpc_bind_address, str) or not self.node_rpc_bind_address:
            raise ContractValidationError("control.node_rpc_bind_address: value is required")
        for name in (
            "shutdown_drain_timeout_ms",
            "shutdown_cleanup_timeout_ms",
            "watch_retention_count",
            "max_inline_control_bytes",
            "max_inline_result_bytes",
        ):
            _positive(f"control.{name}", getattr(self, name))


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    max_literal_value_bytes: int = 65_536
    max_compiled_literal_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        _positive("workflow.max_literal_value_bytes", self.max_literal_value_bytes)
        _positive("workflow.max_compiled_literal_bytes", self.max_compiled_literal_bytes)
        if self.max_literal_value_bytes > self.max_compiled_literal_bytes:
            raise ContractValidationError(
                "workflow.max_literal_value_bytes: must not exceed "
                "workflow.max_compiled_literal_bytes"
            )


@dataclass(frozen=True, slots=True)
class DataConfig:
    shared_filesystem_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.shared_filesystem_roots) != len(set(self.shared_filesystem_roots)):
            raise ContractValidationError(
                "data.shared_filesystem_roots: paths must be unique"
            )


@dataclass(frozen=True, slots=True)
class ClusterConfig:
    cluster_id: str = "ascend-maze"
    environment_fingerprint: str = "local-unverified"
    expected_node_count: int = 1
    head_node_id: str = "head"
    head_node_ip: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if not all(
            (self.cluster_id, self.environment_fingerprint, self.head_node_id, self.head_node_ip)
        ):
            raise ContractValidationError(
                "cluster.cluster_id and cluster.environment_fingerprint are required"
            )
        _positive("cluster.expected_node_count", self.expected_node_count)


@dataclass(frozen=True, slots=True)
class RayRuntimeConfig:
    namespace: str = "ascend-maze"
    temp_directory: str = ""
    object_store_memory_bytes: int | None = None
    include_dashboard: bool = False
    local_num_cpus: int | None = None
    disable_ray_npu_resource: bool = True

    def __post_init__(self) -> None:
        if not self.namespace or not self.temp_directory:
            raise ContractValidationError(
                "runtime.ray.namespace and runtime.ray.temp_directory are required"
            )
        if self.object_store_memory_bytes is not None:
            _positive(
                "runtime.ray.object_store_memory_bytes",
                self.object_store_memory_bytes,
            )
        if self.local_num_cpus is not None:
            _positive("runtime.ray.local_num_cpus", self.local_num_cpus)
        if not isinstance(self.include_dashboard, bool) or not isinstance(
            self.disable_ray_npu_resource, bool
        ):
            raise ContractValidationError(
                "runtime.ray.include_dashboard: must be a boolean"
            )


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    policy: str = "fcfs"
    partitioner: str = "heterogeneous"
    placement_lookahead: int = 8
    max_bypass_count: int = 8
    dispatch_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if self.policy not in {"fcfs", "hacs_no_tp"}:
            raise ContractValidationError("scheduler.policy: unsupported value")
        if self.partitioner not in {"heterogeneous", "unified"}:
            raise ContractValidationError("scheduler.partitioner: unsupported value")
        for name in ("placement_lookahead", "max_bypass_count", "dispatch_timeout_ms"):
            _positive(f"scheduler.{name}", getattr(self, name))


@dataclass(frozen=True, slots=True)
class PlacementConfig:
    anchor_strategy: str = "declared_only"
    task_slots_total: int = 1
    allow_colocation: bool = False
    npu_system_reserved_hbm_mb: int = 4_096
    npu_hbm_headroom_mb: int = 1_024
    host_mem_headroom_mb: int = 1_024
    io_slots_total: int = 8

    def __post_init__(self) -> None:
        if self.anchor_strategy not in {"declared_only", "static"}:
            raise ContractValidationError("placement.anchor_strategy: unsupported value")
        _positive("placement.task_slots_total", self.task_slots_total)
        _positive("placement.io_slots_total", self.io_slots_total)
        for name in (
            "npu_system_reserved_hbm_mb",
            "npu_hbm_headroom_mb",
            "host_mem_headroom_mb",
        ):
            _non_negative(f"placement.{name}", getattr(self, name))
        if not isinstance(self.allow_colocation, bool):
            raise ContractValidationError("placement.allow_colocation: must be a boolean")
        if not self.allow_colocation and self.task_slots_total != 1:
            raise ContractValidationError(
                "placement.task_slots_total: must be 1 when colocation is disabled"
            )


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    max_tasks_per_worker: int = 1
    standby_min_idle: int = 0
    standby_max_idle: int = 0
    max_total: int = 64
    binding_deadline_ms: int = 30_000
    hbm_recovery_deadline_ms: int = 30_000
    hbm_recovery_tolerance_mb: int = 64

    def __post_init__(self) -> None:
        for name in (
            "max_tasks_per_worker",
            "max_total",
            "binding_deadline_ms",
            "hbm_recovery_deadline_ms",
        ):
            _positive(f"worker.{name}", getattr(self, name))
        for name in (
            "standby_min_idle",
            "standby_max_idle",
            "hbm_recovery_tolerance_mb",
        ):
            _non_negative(f"worker.{name}", getattr(self, name))
        if self.standby_min_idle > self.standby_max_idle:
            raise ContractValidationError(
                "worker.standby_min_idle: must not exceed worker.standby_max_idle"
            )
        if self.standby_max_idle > self.max_total:
            raise ContractValidationError(
                "worker.standby_max_idle: must not exceed worker.max_total"
            )


@dataclass(frozen=True, slots=True)
class InferenceConfig:
    model_catalog_path: str | None = None
    reconcile_interval_ms: int = 100

    def __post_init__(self) -> None:
        _positive("inference.reconcile_interval_ms", self.reconcile_interval_ms)


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    backend: str = "parquet"
    root_directory: str = ""
    control_queue_capacity: int = 8_192
    telemetry_queue_capacity: int = 4_096
    batch_size: int = 256
    flush_interval_ms: int = 1_000
    compression: str = "zstd"
    max_page_size: int = 1_000
    flush_timeout_ms: int = 30_000
    cursor_signing_key_file: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in {"parquet", "noop"}:
            raise ContractValidationError("recording.backend: unsupported value")
        if self.backend == "parquet" and not self.root_directory:
            raise ContractValidationError(
                "recording.root_directory: required for parquet backend"
            )
        if self.compression not in {"none", "snappy", "zstd"}:
            raise ContractValidationError("recording.compression: unsupported value")
        for name in (
            "control_queue_capacity",
            "telemetry_queue_capacity",
            "batch_size",
            "flush_interval_ms",
            "max_page_size",
            "flush_timeout_ms",
        ):
            _positive(f"recording.{name}", getattr(self, name))


@dataclass(frozen=True, slots=True)
class FaultConfig:
    retry_backoff_ms: int = 100
    max_retries_default: int = 0

    def __post_init__(self) -> None:
        _non_negative("fault.retry_backoff_ms", self.retry_backoff_ms)
        _non_negative("fault.max_retries_default", self.max_retries_default)


@dataclass(frozen=True, slots=True)
class MainConfig:
    schema_version: int
    profile: str
    source_path: str
    control: ControlConfig
    workflow: WorkflowConfig
    data: DataConfig
    cluster: ClusterConfig
    ray: RayRuntimeConfig
    scheduler: SchedulerConfig
    placement: PlacementConfig
    worker: WorkerConfig
    inference: InferenceConfig
    recording: RecordingConfig
    fault: FaultConfig

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractValidationError("schema_version: only version 1 is supported")
        if self.profile not in {"correctness", "performance"}:
            raise ContractValidationError("profile: must be correctness or performance")
        if not Path(self.source_path).is_absolute():
            raise ContractValidationError("source_path: must be absolute")
        if self.profile == "correctness":
            checks = {
                "scheduler.policy": self.scheduler.policy == "fcfs",
                "placement.task_slots_total": self.placement.task_slots_total == 1,
                "placement.allow_colocation": not self.placement.allow_colocation,
                "worker.max_tasks_per_worker": self.worker.max_tasks_per_worker == 1,
                "worker.standby_min_idle": self.worker.standby_min_idle == 0,
            }
            invalid = [name for name, valid in checks.items() if not valid]
            if invalid:
                raise ContractValidationError(
                    "correctness profile conflicts with: " + ", ".join(invalid)
                )
