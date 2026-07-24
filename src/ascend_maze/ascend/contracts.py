"""Immutable facts reported by the Ascend platform adapter."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, cast

from ascend_maze.contracts.config import ConfigSnapshot
from ascend_maze.core.canonical import FrozenMap, canonical_digest, freeze_canonical
from ascend_maze.core.errors import ContractValidationError

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class AscendProcessSnapshot:
    pid: int
    hbm_mb: int

    def __post_init__(self) -> None:
        if self.pid <= 0 or self.hbm_mb < 0:
            raise ContractValidationError("invalid Ascend process snapshot")


@dataclass(frozen=True, slots=True)
class AscendDeviceSnapshot:
    physical_device_id: str
    card_id: int
    card_device_id: int
    chip_type: str
    chip_version: str
    total_hbm_mb: int
    used_hbm_mb: int
    health: str
    utilization: float | None
    processes: tuple[AscendProcessSnapshot, ...] = ()

    def __post_init__(self) -> None:
        if not self.physical_device_id or not self.chip_type or not self.chip_version:
            raise ContractValidationError("Ascend device identity is required")
        if self.card_id < 0 or self.card_device_id < 0:
            raise ContractValidationError("Ascend card IDs must be non-negative")
        if self.total_hbm_mb <= 0 or not 0 <= self.used_hbm_mb <= self.total_hbm_mb:
            raise ContractValidationError("invalid Ascend HBM snapshot")
        if self.health not in {"healthy", "unhealthy", "unknown"}:
            raise ContractValidationError("invalid Ascend device health")
        if self.utilization is not None and not 0 <= self.utilization <= 100:
            raise ContractValidationError("Ascend utilization must be within 0..100")

    @property
    def free_hbm_mb(self) -> int:
        return self.total_hbm_mb - self.used_hbm_mb


@dataclass(frozen=True, slots=True)
class AscendEnvironmentSnapshot:
    schema_version: int
    machine: str
    chip_types: tuple[str, ...]
    versions: FrozenMap[str, str]
    environment_fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version < 1 or not self.machine or not self.chip_types:
            raise ContractValidationError("incomplete Ascend environment snapshot")
        if tuple(sorted(set(self.chip_types))) != self.chip_types:
            raise ContractValidationError("chip_types must be sorted and unique")
        frozen = freeze_canonical(self.versions)
        if not isinstance(frozen, FrozenMap) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in frozen.items_tuple()
        ):
            raise ContractValidationError("versions must map strings to strings")
        object.__setattr__(self, "versions", frozen)
        if not _SHA256_RE.fullmatch(self.environment_fingerprint):
            raise ContractValidationError("invalid environment fingerprint")
        expected = canonical_digest(
            {
                "schema_version": self.schema_version,
                "machine": self.machine,
                "chip_types": self.chip_types,
                "versions": self.versions,
            }
        )
        if expected != self.environment_fingerprint:
            raise ContractValidationError("Ascend environment fingerprint mismatch")

    @classmethod
    def create(
        cls,
        *,
        machine: str,
        chip_types: tuple[str, ...],
        versions: Mapping[str, str],
    ) -> "AscendEnvironmentSnapshot":
        normalized_chips = tuple(sorted(set(chip_types)))
        frozen = freeze_canonical(dict(versions))
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("versions must be a mapping")
        payload = {
            "schema_version": 1,
            "machine": machine,
            "chip_types": normalized_chips,
            "versions": frozen,
        }
        return cls(
            schema_version=1,
            machine=machine,
            chip_types=normalized_chips,
            versions=cast(FrozenMap[str, str], frozen),
            environment_fingerprint=canonical_digest(payload),
        )


@dataclass(frozen=True, slots=True)
class AscendCorrectnessConfig:
    anchor_strategy: str = "declared_only"
    task_slots_total: int = 1
    allow_colocation: bool = False
    max_tasks_per_worker: int = 1
    standby_min_idle: int = 0
    npu_system_reserved_hbm_mb: int = 4_096
    npu_hbm_headroom_mb: int = 1_024
    host_mem_headroom_mb: int = 1_024
    io_slots_total: int = 8
    worker_binding_deadline_ms: int = 30_000
    hbm_recovery_deadline_ms: int = 30_000
    hbm_recovery_tolerance_mb: int = 64

    def __post_init__(self) -> None:
        if self.anchor_strategy not in {"declared_only", "static"}:
            raise ContractValidationError("unsupported correctness anchor strategy")
        if (
            self.task_slots_total != 1
            or self.allow_colocation
            or self.max_tasks_per_worker != 1
            or self.standby_min_idle != 0
        ):
            raise ContractValidationError(
                "stage-four correctness requires one slot, one-shot workers and no standby"
            )
        for name in (
            "npu_system_reserved_hbm_mb",
            "npu_hbm_headroom_mb",
            "host_mem_headroom_mb",
            "io_slots_total",
            "worker_binding_deadline_ms",
            "hbm_recovery_deadline_ms",
            "hbm_recovery_tolerance_mb",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class AscendColocationConfig:
    """Explicit stage-5C correctness profile for multi-process NPU sharing."""

    anchor_strategy: str = "declared_only"
    scheduler_policy: str = "fcfs"
    task_slots_total: int = 2
    allow_colocation: bool = True
    max_tasks_per_worker: int = 1
    standby_min_idle: int = 2
    npu_system_reserved_hbm_mb: int = 4_096
    npu_hbm_headroom_mb: int = 1_024
    host_mem_headroom_mb: int = 1_024
    io_slots_total: int = 8
    worker_binding_deadline_ms: int = 30_000
    hbm_recovery_deadline_ms: int = 30_000
    hbm_recovery_tolerance_mb: int = 64

    def __post_init__(self) -> None:
        if self.anchor_strategy not in {"declared_only", "static"}:
            raise ContractValidationError("unsupported colocation anchor strategy")
        if self.scheduler_policy not in {"fcfs", "hacs_no_tp"}:
            raise ContractValidationError("unsupported colocation scheduler policy")
        if (
            isinstance(self.task_slots_total, bool)
            or not isinstance(self.task_slots_total, int)
            or self.task_slots_total < 2
        ):
            raise ContractValidationError(
                "stage-5C colocation requires at least two task slots per NPU"
            )
        if not self.allow_colocation:
            raise ContractValidationError(
                "stage-5C colocation must be explicitly enabled"
            )
        if self.max_tasks_per_worker != 1:
            raise ContractValidationError(
                "NPU colocation requires one Attempt per Worker process"
            )
        if (
            isinstance(self.standby_min_idle, bool)
            or not isinstance(self.standby_min_idle, int)
            or self.standby_min_idle < 0
        ):
            raise ContractValidationError("standby_min_idle must be non-negative")
        for name in (
            "npu_system_reserved_hbm_mb",
            "npu_hbm_headroom_mb",
            "host_mem_headroom_mb",
            "io_slots_total",
            "worker_binding_deadline_ms",
            "hbm_recovery_deadline_ms",
            "hbm_recovery_tolerance_mb",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractValidationError(f"{name} must be non-negative")


def create_ascend_correctness_config_snapshot(
    config: AscendCorrectnessConfig,
    environment: AscendEnvironmentSnapshot,
    *,
    source_path: str,
    build_revision: str,
    project_version: str = "0.1.0",
    model_catalog_revision: str = "stage4-no-model-catalog",
    created_at_ms: int | None = None,
) -> ConfigSnapshot:
    """Bind every stage-four correctness setting to the formal config identity."""

    resolved = {
        "profile": "correctness",
        "environment_fingerprint": environment.environment_fingerprint,
        "scheduler": {"policy": "fcfs"},
        "anchor": {"strategy": config.anchor_strategy},
        "placement": {
            "task_slots_total": config.task_slots_total,
            "allow_colocation": config.allow_colocation,
            "npu_system_reserved_hbm_mb": config.npu_system_reserved_hbm_mb,
            "npu_hbm_headroom_mb": config.npu_hbm_headroom_mb,
            "host_mem_headroom_mb": config.host_mem_headroom_mb,
            "io_slots_total": config.io_slots_total,
        },
        "worker": {
            "max_tasks_per_worker": config.max_tasks_per_worker,
            "binding_deadline_ms": config.worker_binding_deadline_ms,
            "standby": {"min_idle": config.standby_min_idle},
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


def create_ascend_colocation_config_snapshot(
    config: AscendColocationConfig,
    environment: AscendEnvironmentSnapshot,
    *,
    source_path: str,
    build_revision: str,
    project_version: str = "0.1.0",
    model_catalog_revision: str = "stage5c-no-model-catalog",
    created_at_ms: int | None = None,
) -> ConfigSnapshot:
    resolved = {
        "profile": "colocation_correctness",
        "environment_fingerprint": environment.environment_fingerprint,
        "scheduler": {"policy": config.scheduler_policy},
        "anchor": {"strategy": config.anchor_strategy},
        "placement": {
            "task_slots_total": config.task_slots_total,
            "allow_colocation": config.allow_colocation,
            "npu_system_reserved_hbm_mb": config.npu_system_reserved_hbm_mb,
            "npu_hbm_headroom_mb": config.npu_hbm_headroom_mb,
            "host_mem_headroom_mb": config.host_mem_headroom_mb,
            "io_slots_total": config.io_slots_total,
        },
        "worker": {
            "max_tasks_per_worker": config.max_tasks_per_worker,
            "binding_deadline_ms": config.worker_binding_deadline_ms,
            "standby": {"min_idle": config.standby_min_idle},
        },
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
