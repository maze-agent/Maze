"""Host Worker identity and per-Attempt occupancy lease contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re

from ascend_maze.contracts.resources import ReservationVector
from ascend_maze.core.errors import ContractValidationError


class WorkerProfile(str, Enum):
    CPU = "cpu"
    IO = "io"
    NPU_HOST = "npu_host"


class StandbyWorkerState(str, Enum):
    STARTING = "starting"
    IDLE = "idle"
    ACQUIRED = "acquired"
    RETIRING = "retiring"
    DEAD = "dead"


@dataclass(frozen=True, slots=True)
class WarmupManifest:
    """Explicit Host-only imports performed before a Standby Worker is ready."""

    modules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.modules))) != self.modules:
            raise ContractValidationError("warmup modules must be sorted and unique")
        for module in self.modules:
            if not isinstance(module, str) or not re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module
            ):
                raise ContractValidationError("invalid warmup module name")

    def validate_for(self, profile: WorkerProfile) -> None:
        if profile is not WorkerProfile.NPU_HOST:
            return
        forbidden = {"acl", "torch", "torch_npu"}
        roots = {module.split(".", 1)[0] for module in self.modules}
        blocked = sorted(roots & forbidden)
        if blocked:
            raise ContractValidationError(
                "NPU Host warmup cannot import device runtime modules: "
                + ", ".join(blocked)
            )


@dataclass(frozen=True, slots=True)
class WorkerPoolProfileConfig:
    profile: WorkerProfile
    min_idle: int
    max_idle: int
    max_total: int
    replenish_concurrency: int
    idle_ttl_ms: int
    acquire_timeout_ms: int
    max_tasks_per_worker: int
    max_worker_lifetime_ms: int
    max_rss_growth_mb: int
    standby_resources: ReservationVector
    termination_timeout_ms: int = 10_000
    warmup_manifest: WarmupManifest = WarmupManifest()

    def __post_init__(self) -> None:
        if not isinstance(self.profile, WorkerProfile):
            raise ContractValidationError("profile must be WorkerProfile")
        for name in (
            "min_idle",
            "max_idle",
            "max_total",
            "replenish_concurrency",
            "idle_ttl_ms",
            "acquire_timeout_ms",
            "max_tasks_per_worker",
            "max_worker_lifetime_ms",
            "max_rss_growth_mb",
            "termination_timeout_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be a non-negative integer")
        if not self.min_idle <= self.max_idle <= self.max_total:
            raise ContractValidationError(
                "Worker Pool watermarks must satisfy min_idle <= max_idle <= max_total"
            )
        if (
            self.replenish_concurrency < 1
            or self.acquire_timeout_ms < 1
            or self.termination_timeout_ms < 1
        ):
            raise ContractValidationError(
                "replenish_concurrency and Worker deadlines must be positive"
            )
        if self.max_tasks_per_worker < 1 or self.max_worker_lifetime_ms < 1:
            raise ContractValidationError(
                "Worker reuse limits must be positive"
            )
        if not isinstance(self.standby_resources, ReservationVector):
            raise ContractValidationError("standby_resources must be ReservationVector")
        if (
            self.standby_resources.npu_hbm_mb != 0
            or self.standby_resources.npu_slots != 0
        ):
            raise ContractValidationError("Standby Workers cannot reserve NPU capacity")
        if (
            self.standby_resources.cpu_num < 1
            or self.standby_resources.host_mem_mb < 1
        ):
            raise ContractValidationError(
                "Standby Workers require positive CPU and Host memory reservations"
            )
        if self.profile is WorkerProfile.IO and self.standby_resources.io_slots < 1:
            raise ContractValidationError("I/O Standby Workers require an I/O slot")
        if not isinstance(self.warmup_manifest, WarmupManifest):
            raise ContractValidationError("warmup_manifest must be WarmupManifest")
        self.warmup_manifest.validate_for(self.profile)


@dataclass(frozen=True, slots=True)
class WorkerPoolConfig:
    mode: str
    profiles: tuple[WorkerPoolProfileConfig, ...]
    reconcile_interval_ms: int = 250
    config_generation: int = 1

    def __post_init__(self) -> None:
        if self.mode not in {"cold_start", "zero_hbm_standby"}:
            raise ContractValidationError("unsupported Worker Pool mode")
        if (
            isinstance(self.reconcile_interval_ms, bool)
            or not isinstance(self.reconcile_interval_ms, int)
            or self.reconcile_interval_ms < 1
        ):
            raise ContractValidationError("reconcile_interval_ms must be positive")
        if (
            isinstance(self.config_generation, bool)
            or not isinstance(self.config_generation, int)
            or self.config_generation < 1
        ):
            raise ContractValidationError("config_generation must be positive")
        profile_names = [item.profile for item in self.profiles]
        if not self.profiles:
            raise ContractValidationError("Worker Pool requires at least one profile")
        if len(profile_names) != len(set(profile_names)):
            raise ContractValidationError("Worker Pool profiles must be unique")
        if self.mode == "cold_start" and any(item.min_idle for item in self.profiles):
            raise ContractValidationError("cold_start mode cannot maintain idle Workers")

    def profile_config(self, profile: WorkerProfile) -> WorkerPoolProfileConfig | None:
        return next((item for item in self.profiles if item.profile is profile), None)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "config_generation": self.config_generation,
            "reconcile_interval_ms": self.reconcile_interval_ms,
            "profiles": tuple(
                {
                    "profile": item.profile.value,
                    "min_idle": item.min_idle,
                    "max_idle": item.max_idle,
                    "max_total": item.max_total,
                    "replenish_concurrency": item.replenish_concurrency,
                    "idle_ttl_ms": item.idle_ttl_ms,
                    "acquire_timeout_ms": item.acquire_timeout_ms,
                    "max_tasks_per_worker": item.max_tasks_per_worker,
                    "max_worker_lifetime_ms": item.max_worker_lifetime_ms,
                    "max_rss_growth_mb": item.max_rss_growth_mb,
                    "termination_timeout_ms": item.termination_timeout_ms,
                    "standby_resources": {
                        "cpu_num": item.standby_resources.cpu_num,
                        "host_mem_mb": item.standby_resources.host_mem_mb,
                        "io_slots": item.standby_resources.io_slots,
                        "npu_hbm_mb": item.standby_resources.npu_hbm_mb,
                        "npu_slots": item.standby_resources.npu_slots,
                    },
                    "warmup_modules": item.warmup_manifest.modules,
                }
                for item in sorted(self.profiles, key=lambda value: value.profile.value)
            ),
        }


@dataclass(frozen=True, slots=True)
class StandbyWorkerDescriptor:
    worker_id: str
    worker_generation: int
    worker_endpoint_id: str
    node_id: str
    boot_id: str
    profile: WorkerProfile
    state: StandbyWorkerState
    standby_lease_id: str | None
    process_id: int | None
    created_at_ms: int
    idle_since_ms: int | None
    tasks_completed: int
    host_warmup_ms: int
    zero_hbm_verified: bool = False
    npu_context_device_ids: tuple[str, ...] = ()
    npu_used_hbm_mb: tuple[tuple[str, int], ...] = ()
    config_generation: int = 1

    def __post_init__(self) -> None:
        for name in ("worker_id", "worker_endpoint_id", "node_id", "boot_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        for name in (
            "worker_generation",
            "created_at_ms",
            "tasks_completed",
            "host_warmup_ms",
            "config_generation",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"invalid {name}")
        if self.worker_generation < 1:
            raise ContractValidationError("worker_generation must be positive")
        if self.config_generation < 1:
            raise ContractValidationError("config_generation must be positive")
        if self.process_id is not None and self.process_id < 1:
            raise ContractValidationError("process_id must be positive")
        if self.idle_since_ms is not None and self.idle_since_ms < 0:
            raise ContractValidationError("idle_since_ms must be non-negative")
        if not isinstance(self.zero_hbm_verified, bool):
            raise ContractValidationError("zero_hbm_verified must be a boolean")
        if tuple(sorted(set(self.npu_context_device_ids))) != self.npu_context_device_ids:
            raise ContractValidationError(
                "npu_context_device_ids must be sorted and unique"
            )
        if tuple(sorted(self.npu_used_hbm_mb)) != self.npu_used_hbm_mb or any(
            not device_id or used_mb < 0
            for device_id, used_mb in self.npu_used_hbm_mb
        ):
            raise ContractValidationError("invalid NPU HBM warmup observations")


@dataclass(frozen=True, slots=True)
class StandbyWarmupReport:
    worker_id: str
    worker_generation: int
    ray_node_id: str
    worker_pid: int
    imported_modules: tuple[str, ...]
    forbidden_device_modules: tuple[str, ...]
    host_rss_mb: int
    host_warmup_ms: int
    zero_hbm_verified: bool = False
    zero_hbm_error: str | None = None
    npu_context_device_ids: tuple[str, ...] = ()
    npu_used_hbm_mb: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        for name in ("worker_id", "ray_node_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        for name in (
            "worker_generation",
            "worker_pid",
            "host_rss_mb",
            "host_warmup_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"invalid {name}")
        if self.worker_generation < 1 or self.worker_pid < 1:
            raise ContractValidationError("Worker generation and PID must be positive")
        if not isinstance(self.zero_hbm_verified, bool):
            raise ContractValidationError("zero_hbm_verified must be a boolean")
        if self.zero_hbm_error is not None and (
            not isinstance(self.zero_hbm_error, str) or not self.zero_hbm_error
        ):
            raise ContractValidationError("zero_hbm_error must be a non-empty string")
        if tuple(sorted(set(self.npu_context_device_ids))) != self.npu_context_device_ids:
            raise ContractValidationError(
                "npu_context_device_ids must be sorted and unique"
            )
        if tuple(sorted(self.npu_used_hbm_mb)) != self.npu_used_hbm_mb or any(
            not device_id or used_mb < 0
            for device_id, used_mb in self.npu_used_hbm_mb
        ):
            raise ContractValidationError("invalid NPU HBM warmup observations")


@dataclass(frozen=True, slots=True)
class WorkerLease:
    worker_lease_id: str
    worker_endpoint_id: str
    worker_id: str
    worker_generation: int
    node_id: str
    boot_id: str
    profile: WorkerProfile
    source: str
    bound_device_id: str | None
    acquired_at_ms: int
    worker_acquire_ms: int = 0
    cold_start_ms: int = 0
    host_warmup_ms: int = 0

    def __post_init__(self) -> None:
        for name in (
            "worker_lease_id",
            "worker_endpoint_id",
            "worker_id",
            "node_id",
            "boot_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractValidationError(f"{name} is required")
        if self.source not in {"standby", "cold_start"}:
            raise ContractValidationError("unsupported WorkerLease source")
        if not isinstance(self.profile, WorkerProfile):
            raise ContractValidationError("profile must be WorkerProfile")
        if (
            isinstance(self.worker_generation, bool)
            or not isinstance(self.worker_generation, int)
            or self.worker_generation < 1
            or isinstance(self.acquired_at_ms, bool)
            or not isinstance(self.acquired_at_ms, int)
            or self.acquired_at_ms < 0
        ):
            raise ContractValidationError("invalid WorkerLease generation or timestamp")
        for name in ("worker_acquire_ms", "cold_start_ms", "host_warmup_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be non-negative")
        if self.source == "standby" and self.cold_start_ms != 0:
            raise ContractValidationError("Standby hit cannot report cold-start time")
