"""Definition-time and reservation-time resource contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections.abc import Mapping
import warnings

from ascend_maze.core.canonical import CanonicalValue, FrozenMap, freeze_canonical
from ascend_maze.core.errors import ContractValidationError


def _non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{name} must be a non-negative integer")
    return value


def _optional_non_negative_int(name: str, value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{name} must be a non-negative integer")
    return value


class ExecutionTarget(str, Enum):
    LOCAL_WORKER = "local_worker"
    MODEL_SERVICE = "model_service"


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    cpu_num: int
    mem_mb: int
    npu_mem_mb: int
    io_num: int

    def __post_init__(self) -> None:
        _non_negative_int("cpu_num", self.cpu_num)
        _non_negative_int("mem_mb", self.mem_mb)
        _non_negative_int("npu_mem_mb", self.npu_mem_mb)
        _non_negative_int("io_num", self.io_num)


@dataclass(frozen=True, slots=True)
class ResourceDeclaration:
    cpu_num: int | None = None
    mem_mb: int | None = None
    npu_mem_mb: int | None = None
    io_num: int | None = None

    def __post_init__(self) -> None:
        for name in ("cpu_num", "mem_mb", "npu_mem_mb", "io_num"):
            value = getattr(self, name)
            if value is not None:
                _non_negative_int(name, value)

    def resolve(self, defaults: ResourceSpec) -> ResourceSpec:
        return ResourceSpec(
            cpu_num=defaults.cpu_num if self.cpu_num is None else self.cpu_num,
            mem_mb=defaults.mem_mb if self.mem_mb is None else self.mem_mb,
            npu_mem_mb=(
                defaults.npu_mem_mb
                if self.npu_mem_mb is None
                else self.npu_mem_mb
            ),
            io_num=defaults.io_num if self.io_num is None else self.io_num,
        )

    @classmethod
    def from_public(
        cls,
        resources: Mapping[str, object] | None,
    ) -> "ResourceDeclaration":
        if resources is not None and not isinstance(resources, Mapping):
            raise ContractValidationError("resources must be a mapping or None")
        values = dict(resources or {})
        if any(not isinstance(key, str) for key in values):
            raise ContractValidationError("resource field names must be strings")
        allowed = {"cpu_num", "mem", "npu_mem", "io_num", "gpu_mem"}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ContractValidationError(
                f"unknown resource fields: {', '.join(unknown)}"
            )
        if "npu_mem" in values and "gpu_mem" in values:
            raise ContractValidationError(
                "resources cannot contain both npu_mem and deprecated gpu_mem"
            )
        if "gpu_mem" in values:
            warnings.warn(
                "gpu_mem is deprecated; use npu_mem",
                DeprecationWarning,
                stacklevel=3,
            )
            values["npu_mem"] = values.pop("gpu_mem")

        mapped = {
            "cpu_num": values.get("cpu_num"),
            "mem_mb": values.get("mem"),
            "npu_mem_mb": values.get("npu_mem"),
            "io_num": values.get("io_num"),
        }
        return cls(
            cpu_num=_optional_non_negative_int("cpu_num", mapped["cpu_num"]),
            mem_mb=_optional_non_negative_int("mem_mb", mapped["mem_mb"]),
            npu_mem_mb=_optional_non_negative_int(
                "npu_mem_mb", mapped["npu_mem_mb"]
            ),
            io_num=_optional_non_negative_int("io_num", mapped["io_num"]),
        )


@dataclass(frozen=True, slots=True)
class ReservationVector:
    cpu_num: int
    host_mem_mb: int
    io_slots: int
    npu_hbm_mb: int
    npu_slots: int

    def __post_init__(self) -> None:
        for name in (
            "cpu_num",
            "host_mem_mb",
            "io_slots",
            "npu_hbm_mb",
            "npu_slots",
        ):
            _non_negative_int(name, getattr(self, name))

    def positive_difference(self, credit: "ReservationVector") -> "ReservationVector":
        """Return the additional capacity needed after replacing ``credit``."""

        if not isinstance(credit, ReservationVector):
            raise ContractValidationError("credit must be ReservationVector")
        return ReservationVector(
            cpu_num=max(0, self.cpu_num - credit.cpu_num),
            host_mem_mb=max(0, self.host_mem_mb - credit.host_mem_mb),
            io_slots=max(0, self.io_slots - credit.io_slots),
            npu_hbm_mb=max(0, self.npu_hbm_mb - credit.npu_hbm_mb),
            npu_slots=max(0, self.npu_slots - credit.npu_slots),
        )


@dataclass(frozen=True, slots=True)
class PlacementLease:
    lease_id: str
    reservation_kind: str
    run_id: str | None
    task_id: str | None
    attempt: int | None
    node_id: str
    boot_id: str
    npu_device_id: str | None
    resources: ReservationVector
    snapshot_version: int
    created_at_ms: int
    dispatch_deadline_ms: int
    converted_standby_lease_id: str | None = None
    standby_worker_id: str | None = None
    allow_npu_colocation: bool = True
    model_instance_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("lease_id", "reservation_kind", "node_id", "boot_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        for name in ("run_id", "task_id", "npu_device_id", "model_instance_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ContractValidationError(f"{name} must be a non-empty string or None")
        for name in ("converted_standby_lease_id", "standby_worker_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ContractValidationError(f"{name} must be a non-empty string or None")
        if self.reservation_kind not in {
            "task",
            "standby_worker",
            "model_instance",
            "model_request",
        }:
            raise ContractValidationError("unsupported reservation_kind")
        if not isinstance(self.resources, ReservationVector):
            raise ContractValidationError("resources must be ReservationVector")
        if self.attempt is not None:
            _non_negative_int("attempt", self.attempt)
        _non_negative_int("snapshot_version", self.snapshot_version)
        _non_negative_int("created_at_ms", self.created_at_ms)
        _non_negative_int("dispatch_deadline_ms", self.dispatch_deadline_ms)
        if self.dispatch_deadline_ms < self.created_at_ms:
            raise ContractValidationError(
                "dispatch_deadline_ms cannot precede created_at_ms"
            )
        converted = self.converted_standby_lease_id is not None
        if converted != (self.standby_worker_id is not None):
            raise ContractValidationError(
                "converted Standby lease and Worker identity must be present together"
            )
        if converted and self.reservation_kind not in {"task", "model_request"}:
            raise ContractValidationError(
                "only Task reservations can originate from a Standby Worker"
            )
        if not isinstance(self.allow_npu_colocation, bool):
            raise ContractValidationError("allow_npu_colocation must be a boolean")
        if self.reservation_kind == "model_instance":
            if self.model_instance_id is None:
                raise ContractValidationError(
                    "model_instance reservation requires model_instance_id"
                )
            if self.run_id is not None or self.task_id is not None or self.attempt is not None:
                raise ContractValidationError(
                    "model_instance reservation cannot belong to a Task Attempt"
                )
        elif self.model_instance_id is not None:
            raise ContractValidationError(
                "only model_instance reservations carry model_instance_id"
            )


@dataclass(frozen=True, slots=True)
class ResourceObservation:
    run_id: str
    task_id: str
    definition_id: str
    attempt: int
    code_hash: str
    environment_fingerprint: str
    requested: ResourceSpec
    status: str
    input_features: FrozenMap[CanonicalValue, CanonicalValue] = FrozenMap()
    peak_host_rss_mb: int | None = None
    peak_npu_allocated_mb: int | None = None
    peak_npu_reserved_mb: int | None = None
    peak_npu_process_hbm_mb: int | None = None
    npu_metric_source: str | None = None
    npu_metric_quality: str | None = None
    error_type: str | None = None
    device_id: str | None = None
    worker_pid: int | None = None
    binding_verified: bool = False

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "task_id",
            "definition_id",
            "code_hash",
            "environment_fingerprint",
            "status",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractValidationError(f"{name} is required")
        _non_negative_int("attempt", self.attempt)
        if not isinstance(self.requested, ResourceSpec):
            raise ContractValidationError("requested must be ResourceSpec")
        for name in (
            "peak_host_rss_mb",
            "peak_npu_allocated_mb",
            "peak_npu_reserved_mb",
            "peak_npu_process_hbm_mb",
            "worker_pid",
        ):
            value = getattr(self, name)
            if value is not None:
                _non_negative_int(name, value)
        if not isinstance(self.binding_verified, bool):
            raise ContractValidationError("binding_verified must be a boolean")
        frozen = freeze_canonical(self.input_features)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("input_features must be a mapping")
        object.__setattr__(self, "input_features", frozen)
