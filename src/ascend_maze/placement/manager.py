"""Deterministic logical placement with one atomic reservation ledger."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from threading import RLock
from typing import Mapping

from ascend_maze.contracts.resources import (
    ExecutionTarget,
    PlacementLease,
    ReservationVector,
)
from ascend_maze.core.canonical import CanonicalValue, FrozenMap, freeze_canonical
from ascend_maze.core.errors import ContractValidationError, StateTransitionError
from ascend_maze.core.identifiers import new_id
from ascend_maze.resources.anchors import ResourceAnchor


def _non_negative(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractValidationError(f"{name} must be a non-negative integer")
    return value


class NodeStatus(str, Enum):
    JOINING = "joining"
    HEALTHY = "healthy"
    STALE = "stale"
    DRAINING = "draining"
    DRAINED = "drained"
    OFFLINE = "offline"
    UNSCHEDULABLE = "unschedulable"


class LeaseStatus(str, Enum):
    RESERVED = "reserved"
    BOUND = "bound"
    RELEASED = "released"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    CONVERTED = "converted"


ACTIVE_LEASE_STATUSES = frozenset({LeaseStatus.RESERVED, LeaseStatus.BOUND})


class StandbyReservationStatus(str, Enum):
    STARTING = "starting"
    READY = "ready"
    CONVERTED = "converted"
    RETIRING = "retiring"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class NpuCapacity:
    device_id: str
    chip_type: str
    total_hbm_mb: int
    system_reserved_hbm_mb: int
    task_slots_total: int
    observed_free_hbm_mb: int | None = None
    healthy: bool = True

    def __post_init__(self) -> None:
        if not self.device_id or not self.chip_type:
            raise ContractValidationError("NPU identity fields are required")
        for name in (
            "total_hbm_mb",
            "system_reserved_hbm_mb",
            "task_slots_total",
        ):
            _non_negative(name, getattr(self, name))
        if self.system_reserved_hbm_mb > self.total_hbm_mb:
            raise ContractValidationError("NPU system reservation exceeds total HBM")
        if self.observed_free_hbm_mb is not None:
            _non_negative("observed_free_hbm_mb", self.observed_free_hbm_mb)


@dataclass(frozen=True, slots=True)
class NodeCapacity:
    node_id: str
    boot_id: str
    node_ip: str
    cpu_total: int
    mem_total_mb: int
    cpu_system_reserved: int
    mem_system_reserved_mb: int
    io_slots_total: int
    npus: tuple[NpuCapacity, ...] = ()
    observed_free_mem_mb: int | None = None
    capabilities: FrozenMap[CanonicalValue, CanonicalValue] = field(
        default_factory=FrozenMap
    )

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (self.node_id, self.boot_id, self.node_ip)
        ):
            raise ContractValidationError("node identity fields are required")
        for name in (
            "cpu_total",
            "mem_total_mb",
            "cpu_system_reserved",
            "mem_system_reserved_mb",
            "io_slots_total",
        ):
            _non_negative(name, getattr(self, name))
        if self.cpu_system_reserved > self.cpu_total:
            raise ContractValidationError("CPU system reservation exceeds capacity")
        if self.mem_system_reserved_mb > self.mem_total_mb:
            raise ContractValidationError("memory system reservation exceeds capacity")
        if self.observed_free_mem_mb is not None:
            _non_negative("observed_free_mem_mb", self.observed_free_mem_mb)
        device_ids = [npu.device_id for npu in self.npus]
        if len(device_ids) != len(set(device_ids)):
            raise ContractValidationError("NPU device IDs must be unique per node")
        frozen = freeze_canonical(self.capabilities)
        if not isinstance(frozen, FrozenMap):
            raise ContractValidationError("capabilities must be a mapping")
        object.__setattr__(self, "capabilities", frozen)


@dataclass(frozen=True, slots=True)
class NpuObservation:
    device_id: str
    health: str
    observed_free_hbm_mb: int
    utilization: float | None = None

    def __post_init__(self) -> None:
        if not self.device_id or not self.health:
            raise ContractValidationError("NPU observation identity is required")
        _non_negative("observed_free_hbm_mb", self.observed_free_hbm_mb)
        if self.utilization is not None and not 0 <= self.utilization <= 100:
            raise ContractValidationError("NPU utilization must be within 0..100")


@dataclass(frozen=True, slots=True)
class NodeObservation:
    node_id: str
    boot_id: str
    sequence: int
    received_at_ms: int
    observed_free_mem_mb: int
    npus: tuple[NpuObservation, ...]

    def __post_init__(self) -> None:
        if not self.node_id or not self.boot_id:
            raise ContractValidationError("node observation identity is required")
        for name in ("sequence", "received_at_ms", "observed_free_mem_mb"):
            _non_negative(name, getattr(self, name))
        device_ids = [item.device_id for item in self.npus]
        if len(device_ids) != len(set(device_ids)):
            raise ContractValidationError("NPU observation IDs must be unique")


@dataclass(slots=True)
class _NodeRecord:
    capacity: NodeCapacity
    status: NodeStatus
    observation_sequence: int = 0


@dataclass(slots=True)
class _LeaseRecord:
    lease: PlacementLease
    status: LeaseStatus
    finished_at_ms: int | None = None
    finish_reason: str | None = None


@dataclass(slots=True)
class _StandbyReservationRecord:
    worker_id: str
    worker_generation: int
    profile: str
    lease_id: str
    status: StandbyReservationStatus
    converted_task_lease_id: str | None = None


@dataclass(slots=True)
class _RunPlacementRecord:
    run_id: str
    affinity_node_id: str | None = None
    affinity_boot_id: str | None = None
    affinity_epoch: int = 0
    confirmed: bool = False
    provisional_lease_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlacementResult:
    selected: bool
    lease: PlacementLease | None
    rejection_reason: str | None
    snapshot_version: int
    affinity_hit: bool
    standby_worker_id: str | None = None
    converted_standby_lease_id: str | None = None


@dataclass(frozen=True, slots=True)
class LeaseSnapshot:
    lease: PlacementLease
    status: LeaseStatus
    finished_at_ms: int | None
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class RunPlacementSnapshot:
    run_id: str
    affinity_node_id: str | None
    affinity_boot_id: str | None
    affinity_epoch: int
    confirmed: bool


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    capacity: NodeCapacity
    status: NodeStatus
    observation_sequence: int
    reserved: ReservationVector
    per_npu_reserved: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class ClusterSnapshot:
    snapshot_version: int
    nodes: tuple[NodeSnapshot, ...]
    active_lease_count: int
    active_leases: tuple[LeaseSnapshot, ...]
    host_mem_headroom_mb: int
    npu_hbm_headroom_mb: int


@dataclass(frozen=True, slots=True)
class StandbyReservationSnapshot:
    worker_id: str
    worker_generation: int
    profile: str
    lease: PlacementLease
    status: StandbyReservationStatus
    converted_task_lease_id: str | None


class PlacementManager:
    """Keep capacity, node health and Maze reservations under one lock."""

    def __init__(
        self,
        *,
        host_mem_headroom_mb: int = 0,
        npu_hbm_headroom_mb: int = 0,
        required_environment_fingerprint: str | None = None,
    ) -> None:
        self.host_mem_headroom_mb = _non_negative(
            "host_mem_headroom_mb", host_mem_headroom_mb
        )
        self.npu_hbm_headroom_mb = _non_negative(
            "npu_hbm_headroom_mb", npu_hbm_headroom_mb
        )
        if required_environment_fingerprint is not None and not required_environment_fingerprint:
            raise ValueError("required_environment_fingerprint cannot be empty")
        self.required_environment_fingerprint = required_environment_fingerprint
        self._nodes: dict[str, _NodeRecord] = {}
        self._leases: dict[str, _LeaseRecord] = {}
        self._standby: dict[str, _StandbyReservationRecord] = {}
        self._run_contexts: dict[str, _RunPlacementRecord] = {}
        self._snapshot_version = 0
        self._lock = RLock()

    def register_node(
        self,
        capacity: NodeCapacity,
        *,
        status: NodeStatus = NodeStatus.HEALTHY,
    ) -> None:
        with self._lock:
            required = self.required_environment_fingerprint
            if required is not None and capacity.capabilities.get(
                "environment_fingerprint"
            ) != required:
                status = NodeStatus.UNSCHEDULABLE
            current = self._nodes.get(capacity.node_id)
            if current is not None and current.capacity.boot_id != capacity.boot_id:
                self._invalidate_affinity_locked(
                    capacity.node_id,
                    current.capacity.boot_id,
                )
                self._invalidate_node_leases_locked(
                    capacity.node_id,
                    current.capacity.boot_id,
                    now_ms=0,
                    reason="boot_generation_changed",
                )
            self._nodes[capacity.node_id] = _NodeRecord(capacity, status)
            self._snapshot_version += 1

    def update_observation(self, observation: NodeObservation) -> bool:
        with self._lock:
            record = self._nodes.get(observation.node_id)
            if (
                record is None
                or record.capacity.boot_id != observation.boot_id
                or observation.sequence <= record.observation_sequence
            ):
                return False
            by_device = {item.device_id: item for item in observation.npus}
            unknown = set(by_device) - {item.device_id for item in record.capacity.npus}
            if unknown:
                raise ContractValidationError(
                    f"observation contains unknown NPU IDs: {sorted(unknown)}"
                )
            npus = tuple(
                replace(
                    npu,
                    observed_free_hbm_mb=(
                        by_device[npu.device_id].observed_free_hbm_mb
                        if npu.device_id in by_device
                        else npu.observed_free_hbm_mb
                    ),
                    healthy=(
                        by_device[npu.device_id].health == "healthy"
                        if npu.device_id in by_device
                        else npu.healthy
                    ),
                )
                for npu in record.capacity.npus
            )
            record.capacity = replace(
                record.capacity,
                observed_free_mem_mb=observation.observed_free_mem_mb,
                npus=npus,
            )
            record.observation_sequence = observation.sequence
            self._snapshot_version += 1
            return True

    def set_node_status(
        self,
        node_id: str,
        status: NodeStatus,
        *,
        now_ms: int,
    ) -> tuple[PlacementLease, ...]:
        with self._lock:
            record = self._require_node(node_id)
            if record.status is status:
                return ()
            record.status = status
            invalidated: tuple[PlacementLease, ...] = ()
            if status is NodeStatus.OFFLINE:
                self._invalidate_affinity_locked(
                    node_id,
                    record.capacity.boot_id,
                )
                invalidated = self._invalidate_node_leases_locked(
                    node_id,
                    record.capacity.boot_id,
                    now_ms=now_ms,
                    reason="node_offline",
                )
            elif status in {
                NodeStatus.DRAINING,
                NodeStatus.DRAINED,
                NodeStatus.STALE,
                NodeStatus.UNSCHEDULABLE,
            }:
                invalidated = self._invalidate_standby_leases_locked(
                    node_id,
                    record.capacity.boot_id,
                    now_ms=now_ms,
                    reason=f"node_{status.value}",
                )
            self._snapshot_version += 1
            return invalidated

    def reserve_standby(
        self,
        *,
        worker_id: str,
        worker_generation: int,
        profile: str,
        node_id: str,
        boot_id: str,
        resources: ReservationVector,
        now_ms: int,
        startup_deadline_ms: int,
    ) -> PlacementLease | None:
        """Reserve Host capacity before a Standby process is created."""

        if not worker_id or not profile:
            raise ContractValidationError("Standby Worker identity and profile are required")
        if worker_generation < 1:
            raise ContractValidationError("worker_generation must be positive")
        if resources.npu_hbm_mb or resources.npu_slots:
            raise ContractValidationError("Standby Worker cannot reserve NPU capacity")
        with self._lock:
            existing = self._standby.get(worker_id)
            if existing is not None:
                return self._leases[existing.lease_id].lease
            node = self._nodes.get(node_id)
            if (
                node is None
                or node.capacity.boot_id != boot_id
                or node.status is not NodeStatus.HEALTHY
            ):
                return None
            _, reason = self._fit(node, resources)
            if reason is not None:
                return None
            lease = PlacementLease(
                lease_id=new_id("lease"),
                reservation_kind="standby_worker",
                run_id=None,
                task_id=None,
                attempt=None,
                node_id=node_id,
                boot_id=boot_id,
                npu_device_id=None,
                resources=resources,
                snapshot_version=self._snapshot_version,
                created_at_ms=now_ms,
                dispatch_deadline_ms=startup_deadline_ms,
            )
            self._leases[lease.lease_id] = _LeaseRecord(
                lease=lease,
                status=LeaseStatus.RESERVED,
            )
            self._standby[worker_id] = _StandbyReservationRecord(
                worker_id=worker_id,
                worker_generation=worker_generation,
                profile=profile,
                lease_id=lease.lease_id,
                status=StandbyReservationStatus.STARTING,
            )
            self._snapshot_version += 1
            return lease

    def activate_standby(
        self,
        *,
        worker_id: str,
        worker_generation: int,
        lease_id: str,
        now_ms: int,
    ) -> bool:
        """Make a warmed Worker visible to Task placement."""

        with self._lock:
            standby = self._require_standby(worker_id)
            if standby.worker_generation != worker_generation or standby.lease_id != lease_id:
                raise StateTransitionError("Standby Worker generation or lease does not match")
            if standby.status is StandbyReservationStatus.READY:
                return False
            if standby.status is not StandbyReservationStatus.STARTING:
                return False
            lease_record = self._require_lease(lease_id)
            if lease_record.status is not LeaseStatus.RESERVED:
                return False
            if lease_record.lease.dispatch_deadline_ms <= now_ms:
                lease_record.status = LeaseStatus.EXPIRED
                lease_record.finished_at_ms = now_ms
                lease_record.finish_reason = "standby_startup_deadline"
                standby.status = StandbyReservationStatus.RETIRED
                self._snapshot_version += 1
                return False
            node = self._nodes.get(lease_record.lease.node_id)
            if (
                node is None
                or node.capacity.boot_id != lease_record.lease.boot_id
                or node.status is not NodeStatus.HEALTHY
            ):
                lease_record.status = LeaseStatus.INVALIDATED
                lease_record.finished_at_ms = now_ms
                lease_record.finish_reason = "standby_node_generation_invalid"
                standby.status = StandbyReservationStatus.RETIRED
                self._snapshot_version += 1
                return False
            lease_record.status = LeaseStatus.BOUND
            standby.status = StandbyReservationStatus.READY
            self._snapshot_version += 1
            return True

    def retire_standby(
        self,
        worker_id: str,
        *,
        now_ms: int,
        reason: str,
    ) -> bool:
        """Retire an unclaimed Standby whose process is already absent or dead."""

        if not self.begin_standby_retirement(
            worker_id,
            converted_task_lease_id=None,
        ):
            return False
        return self.complete_standby_retirement(
            worker_id,
            now_ms=now_ms,
            reason=reason,
        )

    def begin_standby_retirement(
        self,
        worker_id: str,
        *,
        converted_task_lease_id: str | None,
    ) -> bool:
        """Fence placement before the owning Worker process is terminated."""

        with self._lock:
            standby = self._standby.get(worker_id)
            if standby is None or standby.status is StandbyReservationStatus.RETIRED:
                return False
            if standby.status is StandbyReservationStatus.RETIRING:
                return True
            if converted_task_lease_id is None:
                if standby.status not in {
                    StandbyReservationStatus.STARTING,
                    StandbyReservationStatus.READY,
                }:
                    return False
            elif (
                standby.status is not StandbyReservationStatus.CONVERTED
                or standby.converted_task_lease_id != converted_task_lease_id
            ):
                return False
            standby.status = StandbyReservationStatus.RETIRING
            self._snapshot_version += 1
            return True

    def complete_standby_retirement(
        self,
        worker_id: str,
        *,
        now_ms: int,
        reason: str,
    ) -> bool:
        """Release a fenced Standby reservation after process-exit confirmation."""

        with self._lock:
            standby = self._standby.get(worker_id)
            if standby is None or standby.status is StandbyReservationStatus.RETIRED:
                return False
            if standby.status is not StandbyReservationStatus.RETIRING:
                raise StateTransitionError("Standby Worker retirement is not fenced")
            lease_record = self._require_lease(standby.lease_id)
            if lease_record.status in ACTIVE_LEASE_STATUSES:
                lease_record.status = LeaseStatus.RELEASED
                lease_record.finished_at_ms = now_ms
                lease_record.finish_reason = reason
            standby.status = StandbyReservationStatus.RETIRED
            self._snapshot_version += 1
            return True

    def purge_retired_standby(self, worker_id: str) -> bool:
        """Drop terminal Standby ledger history after its process is confirmed dead."""

        with self._lock:
            standby = self._standby.get(worker_id)
            if standby is None:
                return False
            if standby.status is not StandbyReservationStatus.RETIRED:
                raise StateTransitionError("Standby Worker is not retired")
            lease = self._require_lease(standby.lease_id)
            if lease.status in ACTIVE_LEASE_STATUSES:
                raise StateTransitionError("Standby reservation is still active")
            del self._leases[standby.lease_id]
            del self._standby[worker_id]
            self._snapshot_version += 1
            return True

    def restore_task_to_standby(
        self,
        *,
        task_lease_id: str,
        worker_id: str,
        worker_generation: int,
        profile: str,
        resources: ReservationVector,
        now_ms: int,
        idle_deadline_ms: int,
    ) -> PlacementLease | None:
        """Atomically replace a Task reservation after Worker sanitization."""

        if resources.npu_hbm_mb or resources.npu_slots:
            raise ContractValidationError("Standby Worker cannot reserve NPU capacity")
        with self._lock:
            task_record = self._require_lease(task_lease_id)
            task_lease = task_record.lease
            if task_record.status not in ACTIVE_LEASE_STATUSES:
                return None
            if task_lease.reservation_kind not in {"task", "model_request"}:
                raise StateTransitionError("only a Task Lease can return to Standby")
            node = self._nodes.get(task_lease.node_id)
            if (
                node is None
                or node.capacity.boot_id != task_lease.boot_id
                or node.status is not NodeStatus.HEALTHY
            ):
                return None
            _, reason = self._fit(node, resources, credit_lease_id=task_lease_id)
            if reason is not None:
                return None
            existing = self._standby.get(worker_id)
            if existing is not None and existing.worker_generation != worker_generation:
                raise StateTransitionError("Standby Worker generation changed")
            task_record.status = LeaseStatus.CONVERTED
            task_record.finished_at_ms = now_ms
            task_record.finish_reason = "returned_to_standby"
            self._clear_provisional_affinity_locked(task_lease)
            standby_lease = PlacementLease(
                lease_id=new_id("lease"),
                reservation_kind="standby_worker",
                run_id=None,
                task_id=None,
                attempt=None,
                node_id=task_lease.node_id,
                boot_id=task_lease.boot_id,
                npu_device_id=None,
                resources=resources,
                snapshot_version=self._snapshot_version,
                created_at_ms=now_ms,
                dispatch_deadline_ms=idle_deadline_ms,
            )
            self._leases[standby_lease.lease_id] = _LeaseRecord(
                lease=standby_lease,
                status=LeaseStatus.BOUND,
            )
            if existing is None:
                existing = _StandbyReservationRecord(
                    worker_id=worker_id,
                    worker_generation=worker_generation,
                    profile=profile,
                    lease_id=standby_lease.lease_id,
                    status=StandbyReservationStatus.READY,
                )
                self._standby[worker_id] = existing
            else:
                existing.profile = profile
                existing.lease_id = standby_lease.lease_id
                existing.status = StandbyReservationStatus.READY
                existing.converted_task_lease_id = None
            self._snapshot_version += 1
            return standby_lease

    def standby_snapshot(self, worker_id: str) -> StandbyReservationSnapshot:
        with self._lock:
            record = self._require_standby(worker_id)
            return StandbyReservationSnapshot(
                worker_id=record.worker_id,
                worker_generation=record.worker_generation,
                profile=record.profile,
                lease=self._require_lease(record.lease_id).lease,
                status=record.status,
                converted_task_lease_id=record.converted_task_lease_id,
            )

    def ready_standby_count(
        self,
        *,
        node_id: str | None = None,
        boot_id: str | None = None,
        profile: str | None = None,
    ) -> int:
        with self._lock:
            return sum(
                record.status is StandbyReservationStatus.READY
                and (node_id is None or self._leases[record.lease_id].lease.node_id == node_id)
                and (boot_id is None or self._leases[record.lease_id].lease.boot_id == boot_id)
                and (profile is None or record.profile == profile)
                for record in self._standby.values()
            )

    def try_reserve(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        anchor: ResourceAnchor,
        now_ms: int,
        dispatch_deadline_ms: int,
        preferred_node_id: str | None = None,
    ) -> PlacementResult:
        with self._lock:
            snapshot_version = self._snapshot_version
            reservation_kind = (
                "model_request"
                if anchor.execution_target is ExecutionTarget.MODEL_SERVICE
                else "task"
            )
            vector = self._reservation_vector(anchor)
            worker_profile = self._worker_profile(anchor)
            if self._permanently_unsatisfiable(vector):
                return PlacementResult(
                    False,
                    None,
                    "resource_request_unsatisfiable",
                    snapshot_version,
                    False,
                )
            healthy = [
                record
                for record in self._nodes.values()
                if record.status is NodeStatus.HEALTHY
            ]
            if not healthy:
                return PlacementResult(
                    False, None, "no_healthy_node", snapshot_version, False
                )

            context = self._run_contexts.setdefault(
                run_id, _RunPlacementRecord(run_id=run_id)
            )
            affinity_id = context.affinity_node_id
            requested_preference = preferred_node_id or affinity_id
            candidates: list[
                tuple[
                    tuple[float, str, str, str],
                    _NodeRecord,
                    str | None,
                    bool,
                    _StandbyReservationRecord | None,
                ]
            ] = []
            rejection_counts: dict[str, int] = {}
            for record in healthy:
                standby = self._ready_standby_for_node(
                    record.capacity.node_id,
                    record.capacity.boot_id,
                    worker_profile,
                )
                credit_lease_id = None if standby is None else standby.lease_id
                device_id, reason = self._fit(
                    record,
                    vector,
                    credit_lease_id=credit_lease_id,
                )
                if reason is not None:
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    continue
                affinity_hit = requested_preference == record.capacity.node_id
                score = self._pressure_score(
                    record,
                    vector,
                    device_id,
                    credit_lease_id=credit_lease_id,
                )
                priority = 0.0 if affinity_hit else 1.0 + score
                candidates.append(
                    (
                        (
                            priority,
                            record.capacity.node_id,
                            "" if device_id is None else device_id,
                            "" if standby is None else standby.worker_id,
                        ),
                        record,
                        device_id,
                        affinity_hit,
                        standby,
                    )
                )
            if not candidates:
                reason = self._dominant_rejection(rejection_counts)
                return PlacementResult(False, None, reason, snapshot_version, False)

            _, selected, device_id, affinity_hit, standby = min(
                candidates, key=lambda item: item[0]
            )
            lease = PlacementLease(
                lease_id=new_id("lease"),
                reservation_kind=reservation_kind,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                node_id=selected.capacity.node_id,
                boot_id=selected.capacity.boot_id,
                npu_device_id=device_id,
                resources=vector,
                snapshot_version=snapshot_version,
                created_at_ms=now_ms,
                dispatch_deadline_ms=dispatch_deadline_ms,
                converted_standby_lease_id=(
                    None if standby is None else standby.lease_id
                ),
                standby_worker_id=None if standby is None else standby.worker_id,
            )
            if standby is not None:
                standby_lease = self._require_lease(standby.lease_id)
                if (
                    standby.status is not StandbyReservationStatus.READY
                    or standby_lease.status is not LeaseStatus.BOUND
                ):
                    raise StateTransitionError("Standby reservation changed during placement")
                standby_lease.status = LeaseStatus.CONVERTED
                standby_lease.finished_at_ms = now_ms
                standby_lease.finish_reason = f"converted_to:{lease.lease_id}"
                standby.status = StandbyReservationStatus.CONVERTED
                standby.converted_task_lease_id = lease.lease_id
            self._leases[lease.lease_id] = _LeaseRecord(
                lease=lease,
                status=LeaseStatus.RESERVED,
            )
            if context.affinity_node_id is None:
                context.affinity_node_id = lease.node_id
                context.affinity_boot_id = lease.boot_id
                context.provisional_lease_id = lease.lease_id
            self._snapshot_version += 1
            return PlacementResult(
                True,
                lease,
                None,
                snapshot_version,
                affinity_hit,
                None if standby is None else standby.worker_id,
                None if standby is None else standby.lease_id,
            )

    def reserve_model_instance(
        self,
        *,
        instance_id: str,
        generation: int,
        resources: ReservationVector,
        allow_colocation: bool,
        now_ms: int,
        startup_deadline_ms: int,
        preferred_node_id: str | None = None,
    ) -> PlacementResult:
        """Atomically reserve global model capacity without a Run affinity record."""

        if not instance_id:
            raise ContractValidationError("instance_id is required")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise ContractValidationError("instance generation must be positive")
        if not isinstance(resources, ReservationVector):
            raise ContractValidationError("model resources must be ReservationVector")
        if resources.npu_hbm_mb < 1 or resources.npu_slots < 1:
            raise ContractValidationError(
                "model instance requires positive NPU HBM and slots"
            )
        if not isinstance(allow_colocation, bool):
            raise ContractValidationError("allow_colocation must be a boolean")
        with self._lock:
            snapshot_version = self._snapshot_version
            if self._permanently_unsatisfiable(resources):
                return PlacementResult(
                    False,
                    None,
                    "resource_request_unsatisfiable",
                    snapshot_version,
                    False,
                )
            candidates: list[
                tuple[tuple[float, str, str], _NodeRecord, str, bool]
            ] = []
            rejection_counts: dict[str, int] = {}
            for record in self._nodes.values():
                if record.status is not NodeStatus.HEALTHY:
                    continue
                device_id, reason = self._fit(
                    record,
                    resources,
                    allow_npu_colocation=allow_colocation,
                )
                if reason is not None:
                    rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
                    continue
                assert device_id is not None
                affinity_hit = preferred_node_id == record.capacity.node_id
                pressure = self._pressure_score(record, resources, device_id)
                candidates.append(
                    (
                        (
                            0.0 if affinity_hit else 1.0 + pressure,
                            record.capacity.node_id,
                            device_id,
                        ),
                        record,
                        device_id,
                        affinity_hit,
                    )
                )
            if not candidates:
                reason = self._dominant_rejection(rejection_counts)
                if not rejection_counts:
                    reason = "no_healthy_node"
                return PlacementResult(False, None, reason, snapshot_version, False)
            _, selected, device_id, affinity_hit = min(
                candidates, key=lambda item: item[0]
            )
            lease = PlacementLease(
                lease_id=new_id("lease"),
                reservation_kind="model_instance",
                run_id=None,
                task_id=None,
                attempt=None,
                node_id=selected.capacity.node_id,
                boot_id=selected.capacity.boot_id,
                npu_device_id=device_id,
                resources=resources,
                snapshot_version=snapshot_version,
                created_at_ms=now_ms,
                dispatch_deadline_ms=startup_deadline_ms,
                allow_npu_colocation=allow_colocation,
                model_instance_id=instance_id,
            )
            self._leases[lease.lease_id] = _LeaseRecord(
                lease=lease,
                status=LeaseStatus.RESERVED,
            )
            self._snapshot_version += 1
            return PlacementResult(
                True,
                lease,
                None,
                snapshot_version,
                affinity_hit,
            )

    def bind_lease(self, lease_id: str, *, now_ms: int) -> bool:
        with self._lock:
            record = self._require_lease(lease_id)
            if record.status is LeaseStatus.BOUND:
                return False
            if record.status is not LeaseStatus.RESERVED:
                return False
            if record.lease.dispatch_deadline_ms <= now_ms:
                record.status = LeaseStatus.EXPIRED
                record.finished_at_ms = now_ms
                record.finish_reason = "dispatch_deadline"
                self._clear_provisional_affinity_locked(record.lease)
                self._snapshot_version += 1
                return False
            node = self._nodes.get(record.lease.node_id)
            if (
                node is None
                or node.capacity.boot_id != record.lease.boot_id
                or node.status is not NodeStatus.HEALTHY
            ):
                record.status = LeaseStatus.INVALIDATED
                record.finished_at_ms = now_ms
                record.finish_reason = "node_generation_invalid"
                self._clear_provisional_affinity_locked(record.lease)
                self._snapshot_version += 1
                return False
            record.status = LeaseStatus.BOUND
            context = self._run_contexts.get(record.lease.run_id or "")
            if context is not None and context.provisional_lease_id == lease_id:
                context.confirmed = True
                context.provisional_lease_id = None
            self._snapshot_version += 1
            return True

    def expire_lease(self, lease_id: str, *, now_ms: int) -> bool:
        """Expire one unbound lease after its dispatch deadline."""

        with self._lock:
            record = self._require_lease(lease_id)
            if (
                record.status is not LeaseStatus.RESERVED
                or record.lease.dispatch_deadline_ms > now_ms
            ):
                return False
            record.status = LeaseStatus.EXPIRED
            record.finished_at_ms = now_ms
            record.finish_reason = "dispatch_deadline"
            self._clear_provisional_affinity_locked(record.lease)
            self._snapshot_version += 1
            return True

    def release_lease(
        self,
        lease_id: str,
        *,
        now_ms: int,
        run_id: str | None = None,
        task_id: str | None = None,
        attempt: int | None = None,
        reason: str = "completed",
    ) -> bool:
        with self._lock:
            record = self._require_lease(lease_id)
            lease = record.lease
            if run_id is not None and lease.run_id != run_id:
                raise StateTransitionError("lease run_id does not match")
            if task_id is not None and lease.task_id != task_id:
                raise StateTransitionError("lease task_id does not match")
            if attempt is not None and lease.attempt != attempt:
                raise StateTransitionError("lease attempt does not match")
            if record.status not in ACTIVE_LEASE_STATUSES:
                return False
            record.status = LeaseStatus.RELEASED
            record.finished_at_ms = now_ms
            record.finish_reason = reason
            context = self._run_contexts.get(lease.run_id or "")
            if (
                context is not None
                and not context.confirmed
                and context.provisional_lease_id == lease_id
            ):
                context.affinity_node_id = None
                context.affinity_boot_id = None
                context.provisional_lease_id = None
            self._snapshot_version += 1
            return True

    def invalidate_lease(
        self,
        lease_id: str,
        *,
        now_ms: int,
        reason: str,
    ) -> bool:
        with self._lock:
            record = self._require_lease(lease_id)
            if record.status not in ACTIVE_LEASE_STATUSES:
                return False
            record.status = LeaseStatus.INVALIDATED
            record.finished_at_ms = now_ms
            record.finish_reason = reason
            self._clear_provisional_affinity_locked(record.lease)
            self._snapshot_version += 1
            return True

    def expire_reserved(self, *, now_ms: int) -> tuple[PlacementLease, ...]:
        with self._lock:
            expired: list[PlacementLease] = []
            for record in self._leases.values():
                if (
                    record.status is LeaseStatus.RESERVED
                    and record.lease.dispatch_deadline_ms <= now_ms
                ):
                    record.status = LeaseStatus.EXPIRED
                    record.finished_at_ms = now_ms
                    record.finish_reason = "dispatch_deadline"
                    self._clear_provisional_affinity_locked(record.lease)
                    self._retire_standby_for_lease_locked(record.lease.lease_id)
                    expired.append(record.lease)
            if expired:
                self._snapshot_version += 1
            return tuple(expired)

    def lease_snapshot(self, lease_id: str) -> LeaseSnapshot:
        with self._lock:
            record = self._require_lease(lease_id)
            return LeaseSnapshot(
                lease=record.lease,
                status=record.status,
                finished_at_ms=record.finished_at_ms,
                finish_reason=record.finish_reason,
            )

    def lease_snapshots(self) -> tuple[LeaseSnapshot, ...]:
        with self._lock:
            return tuple(
                LeaseSnapshot(
                    lease=record.lease,
                    status=record.status,
                    finished_at_ms=record.finished_at_ms,
                    finish_reason=record.finish_reason,
                )
                for _, record in sorted(self._leases.items())
            )

    def restore_reconciled_leases(
        self,
        snapshots: tuple[LeaseSnapshot, ...],
        *,
        now_ms: int,
    ) -> None:
        """Retain old Lease identities as invalidated history after restart."""

        with self._lock:
            for snapshot in snapshots:
                lease = snapshot.lease
                if lease.lease_id in self._leases:
                    raise StateTransitionError(
                        f"lease already exists during recovery: {lease.lease_id}"
                    )
                status = snapshot.status
                finished_at_ms = snapshot.finished_at_ms
                finish_reason = snapshot.finish_reason
                if status in ACTIVE_LEASE_STATUSES:
                    status = LeaseStatus.INVALIDATED
                    finished_at_ms = now_ms
                    finish_reason = "controller_generation_changed"
                self._leases[lease.lease_id] = _LeaseRecord(
                    lease=lease,
                    status=status,
                    finished_at_ms=finished_at_ms,
                    finish_reason=finish_reason,
                )
            if snapshots:
                self._snapshot_version += 1

    def run_snapshot(self, run_id: str) -> RunPlacementSnapshot:
        with self._lock:
            context = self._run_contexts.get(run_id, _RunPlacementRecord(run_id))
            return RunPlacementSnapshot(
                run_id=run_id,
                affinity_node_id=context.affinity_node_id,
                affinity_boot_id=context.affinity_boot_id,
                affinity_epoch=context.affinity_epoch,
                confirmed=context.confirmed,
            )

    def destroy_run_context(self, run_id: str) -> bool:
        with self._lock:
            if self.active_lease_count(run_id) != 0:
                raise StateTransitionError("run still owns active placement leases")
            removed_context = self._run_contexts.pop(run_id, None) is not None
            lease_ids = [
                lease_id
                for lease_id, record in self._leases.items()
                if record.lease.run_id == run_id
            ]
            for lease_id in lease_ids:
                del self._leases[lease_id]
            return removed_context or bool(lease_ids)

    def active_lease_count(self, run_id: str | None = None) -> int:
        with self._lock:
            return sum(
                record.status in ACTIVE_LEASE_STATUSES
                and (run_id is None or record.lease.run_id == run_id)
                for record in self._leases.values()
            )

    def max_single_npu_allocatable_hbm_mb(self) -> int:
        with self._lock:
            return max(
                (
                    npu.total_hbm_mb - npu.system_reserved_hbm_mb
                    for record in self._nodes.values()
                    for npu in record.capacity.npus
                ),
                default=0,
            )

    def lease_record_count(self, run_id: str | None = None) -> int:
        with self._lock:
            return sum(
                run_id is None or record.lease.run_id == run_id
                for record in self._leases.values()
            )

    def snapshot(self) -> ClusterSnapshot:
        with self._lock:
            nodes: list[NodeSnapshot] = []
            for node_id in sorted(self._nodes):
                record = self._nodes[node_id]
                reserved = self._reserved_on_node(record.capacity)
                per_npu: list[tuple[str, int, int]] = []
                for npu in sorted(record.capacity.npus, key=lambda item: item.device_id):
                    hbm, slots = self._reserved_on_npu(node_id, npu.device_id)
                    per_npu.append((npu.device_id, hbm, slots))
                nodes.append(
                    NodeSnapshot(
                        capacity=record.capacity,
                        status=record.status,
                        observation_sequence=record.observation_sequence,
                        reserved=reserved,
                        per_npu_reserved=tuple(per_npu),
                    )
                )
            active_leases = tuple(
                LeaseSnapshot(
                    lease=record.lease,
                    status=record.status,
                    finished_at_ms=record.finished_at_ms,
                    finish_reason=record.finish_reason,
                )
                for _, record in sorted(self._leases.items())
                if record.status in ACTIVE_LEASE_STATUSES
            )
            return ClusterSnapshot(
                snapshot_version=self._snapshot_version,
                nodes=tuple(nodes),
                active_lease_count=len(active_leases),
                active_leases=active_leases,
                host_mem_headroom_mb=self.host_mem_headroom_mb,
                npu_hbm_headroom_mb=self.npu_hbm_headroom_mb,
            )

    @property
    def snapshot_version(self) -> int:
        with self._lock:
            return self._snapshot_version

    def _reservation_vector(self, anchor: ResourceAnchor) -> ReservationVector:
        local_npu = (
            anchor.execution_target is ExecutionTarget.LOCAL_WORKER
            and anchor.task_kind == "npu"
        )
        return ReservationVector(
            cpu_num=anchor.effective.cpu_num,
            host_mem_mb=anchor.effective.mem_mb,
            io_slots=anchor.effective.io_num,
            npu_hbm_mb=anchor.effective.npu_mem_mb if local_npu else 0,
            npu_slots=1 if local_npu else 0,
        )

    @staticmethod
    def _worker_profile(anchor: ResourceAnchor) -> str:
        if anchor.execution_target is ExecutionTarget.MODEL_SERVICE:
            return "io"
        try:
            return {"cpu": "cpu", "io": "io", "npu": "npu_host"}[
                anchor.task_kind
            ]
        except KeyError as exc:
            raise ContractValidationError(
                f"unsupported Task kind for Worker placement: {anchor.task_kind}"
            ) from exc

    def _fit(
        self,
        node: _NodeRecord,
        vector: ReservationVector,
        *,
        credit_lease_id: str | None = None,
        allow_npu_colocation: bool = True,
    ) -> tuple[str | None, str | None]:
        capacity = node.capacity
        credit = self._credit_vector(credit_lease_id)
        incremental = vector.positive_difference(credit)
        reserved = self._reserved_on_node(capacity, exclude_lease_id=credit_lease_id)
        cpu_free = capacity.cpu_total - capacity.cpu_system_reserved - reserved.cpu_num
        if cpu_free < vector.cpu_num:
            return None, "insufficient_cpu"
        ledger_mem_free = (
            capacity.mem_total_mb
            - capacity.mem_system_reserved_mb
            - reserved.host_mem_mb
        )
        observed_mem = (
            ledger_mem_free
            if capacity.observed_free_mem_mb is None
            else max(0, capacity.observed_free_mem_mb - self.host_mem_headroom_mb)
        )
        if ledger_mem_free < vector.host_mem_mb or observed_mem < incremental.host_mem_mb:
            return None, "insufficient_host_memory"
        if capacity.io_slots_total - reserved.io_slots < vector.io_slots:
            return None, "io_slots_full"
        if vector.npu_slots == 0 and vector.npu_hbm_mb == 0:
            return None, None
        if not capacity.npus:
            return None, "no_healthy_npu"
        fitting: list[tuple[int, str]] = []
        any_healthy = False
        any_hbm = False
        any_colocation = False
        for npu in capacity.npus:
            if not npu.healthy:
                continue
            any_healthy = True
            reserved_hbm, reserved_slots = self._reserved_on_npu(
                capacity.node_id,
                npu.device_id,
                exclude_lease_id=credit_lease_id,
            )
            existing_allows_colocation = self._npu_leases_allow_colocation(
                capacity.node_id,
                npu.device_id,
                exclude_lease_id=credit_lease_id,
            )
            if (
                (reserved_hbm > 0 or reserved_slots > 0)
                and (not allow_npu_colocation or not existing_allows_colocation)
            ):
                continue
            any_colocation = True
            ledger_hbm = (
                npu.total_hbm_mb
                - npu.system_reserved_hbm_mb
                - reserved_hbm
            )
            observed_hbm = (
                ledger_hbm
                if npu.observed_free_hbm_mb is None
                else max(0, npu.observed_free_hbm_mb - self.npu_hbm_headroom_mb)
            )
            placeable_hbm = min(ledger_hbm, observed_hbm)
            if placeable_hbm >= vector.npu_hbm_mb:
                any_hbm = True
                if npu.task_slots_total - reserved_slots >= vector.npu_slots:
                    fitting.append((placeable_hbm - vector.npu_hbm_mb, npu.device_id))
        if fitting:
            return min(fitting)[1], None
        if not any_healthy:
            return None, "no_healthy_npu"
        if not any_colocation:
            return None, "npu_colocation_forbidden"
        if not any_hbm:
            return None, "insufficient_npu_hbm"
        return None, "npu_task_slots_full"

    def _npu_leases_allow_colocation(
        self,
        node_id: str,
        device_id: str,
        *,
        exclude_lease_id: str | None = None,
    ) -> bool:
        return all(
            record.lease.allow_npu_colocation
            for record in self._leases.values()
            if record.status in ACTIVE_LEASE_STATUSES
            and record.lease.lease_id != exclude_lease_id
            and record.lease.node_id == node_id
            and record.lease.npu_device_id == device_id
        )

    def _permanently_unsatisfiable(self, vector: ReservationVector) -> bool:
        if vector.npu_hbm_mb == 0 and vector.npu_slots == 0:
            return False
        npus = [
            npu
            for record in self._nodes.values()
            for npu in record.capacity.npus
        ]
        if not npus:
            return False
        return not any(
            npu.total_hbm_mb - npu.system_reserved_hbm_mb >= vector.npu_hbm_mb
            and npu.task_slots_total >= vector.npu_slots
            for npu in npus
        )

    def _pressure_score(
        self,
        node: _NodeRecord,
        vector: ReservationVector,
        device_id: str | None,
        *,
        credit_lease_id: str | None = None,
    ) -> float:
        capacity = node.capacity
        reserved = self._reserved_on_node(
            capacity, exclude_lease_id=credit_lease_id
        )
        ratios: list[float] = []
        cpu_allocatable = capacity.cpu_total - capacity.cpu_system_reserved
        if cpu_allocatable > 0 and vector.cpu_num > 0:
            ratios.append((reserved.cpu_num + vector.cpu_num) / cpu_allocatable)
        mem_allocatable = capacity.mem_total_mb - capacity.mem_system_reserved_mb
        if mem_allocatable > 0 and vector.host_mem_mb > 0:
            ratios.append(
                (reserved.host_mem_mb + vector.host_mem_mb) / mem_allocatable
            )
        if capacity.io_slots_total > 0 and vector.io_slots > 0:
            ratios.append(
                (reserved.io_slots + vector.io_slots) / capacity.io_slots_total
            )
        if device_id is not None:
            npu = next(item for item in capacity.npus if item.device_id == device_id)
            hbm, slots = self._reserved_on_npu(capacity.node_id, device_id)
            if credit_lease_id is not None:
                hbm, slots = self._reserved_on_npu(
                    capacity.node_id,
                    device_id,
                    exclude_lease_id=credit_lease_id,
                )
            hbm_allocatable = npu.total_hbm_mb - npu.system_reserved_hbm_mb
            if hbm_allocatable > 0:
                ratios.append((hbm + vector.npu_hbm_mb) / hbm_allocatable)
            if npu.task_slots_total > 0:
                ratios.append((slots + vector.npu_slots) / npu.task_slots_total)
        return max(ratios, default=0.0)

    def _reserved_on_node(
        self,
        capacity: NodeCapacity,
        *,
        exclude_lease_id: str | None = None,
    ) -> ReservationVector:
        cpu = memory = io = 0
        for record in self._leases.values():
            if (
                record.status in ACTIVE_LEASE_STATUSES
                and record.lease.lease_id != exclude_lease_id
                and record.lease.node_id == capacity.node_id
                and record.lease.boot_id == capacity.boot_id
            ):
                cpu += record.lease.resources.cpu_num
                memory += record.lease.resources.host_mem_mb
                io += record.lease.resources.io_slots
        return ReservationVector(cpu, memory, io, 0, 0)

    def _reserved_on_npu(
        self,
        node_id: str,
        device_id: str,
        *,
        exclude_lease_id: str | None = None,
    ) -> tuple[int, int]:
        hbm = slots = 0
        for record in self._leases.values():
            if (
                record.status in ACTIVE_LEASE_STATUSES
                and record.lease.lease_id != exclude_lease_id
                and record.lease.node_id == node_id
                and record.lease.npu_device_id == device_id
            ):
                hbm += record.lease.resources.npu_hbm_mb
                slots += record.lease.resources.npu_slots
        return hbm, slots

    def _credit_vector(self, lease_id: str | None) -> ReservationVector:
        if lease_id is None:
            return ReservationVector(0, 0, 0, 0, 0)
        record = self._require_lease(lease_id)
        if record.status not in ACTIVE_LEASE_STATUSES:
            raise StateTransitionError("reservation credit is not active")
        return record.lease.resources

    def _ready_standby_for_node(
        self,
        node_id: str,
        boot_id: str,
        profile: str,
    ) -> _StandbyReservationRecord | None:
        candidates = [
            record
            for record in self._standby.values()
            if record.status is StandbyReservationStatus.READY
            and record.profile == profile
            and self._leases[record.lease_id].status is LeaseStatus.BOUND
            and self._leases[record.lease_id].lease.node_id == node_id
            and self._leases[record.lease_id].lease.boot_id == boot_id
        ]
        return min(candidates, key=lambda item: item.worker_id, default=None)

    @staticmethod
    def _dominant_rejection(counts: Mapping[str, int]) -> str:
        priority = (
            "insufficient_cpu",
            "insufficient_host_memory",
            "io_slots_full",
            "no_healthy_npu",
            "insufficient_npu_hbm",
            "npu_task_slots_full",
            "npu_colocation_forbidden",
        )
        for reason in priority:
            if counts.get(reason):
                return reason
        return "no_healthy_node"

    def _invalidate_node_leases_locked(
        self,
        node_id: str,
        boot_id: str,
        *,
        now_ms: int,
        reason: str,
    ) -> tuple[PlacementLease, ...]:
        invalidated: list[PlacementLease] = []
        for record in self._leases.values():
            if (
                record.status in ACTIVE_LEASE_STATUSES
                and record.lease.node_id == node_id
                and record.lease.boot_id == boot_id
            ):
                record.status = LeaseStatus.INVALIDATED
                record.finished_at_ms = now_ms
                record.finish_reason = reason
                self._clear_provisional_affinity_locked(record.lease)
                self._retire_standby_for_lease_locked(record.lease.lease_id)
                invalidated.append(record.lease)
        return tuple(invalidated)

    def _invalidate_standby_leases_locked(
        self,
        node_id: str,
        boot_id: str,
        *,
        now_ms: int,
        reason: str,
    ) -> tuple[PlacementLease, ...]:
        invalidated: list[PlacementLease] = []
        for standby in self._standby.values():
            lease_record = self._leases[standby.lease_id]
            if (
                standby.status
                in {StandbyReservationStatus.STARTING, StandbyReservationStatus.READY}
                and lease_record.status in ACTIVE_LEASE_STATUSES
                and lease_record.lease.node_id == node_id
                and lease_record.lease.boot_id == boot_id
            ):
                lease_record.status = LeaseStatus.INVALIDATED
                lease_record.finished_at_ms = now_ms
                lease_record.finish_reason = reason
                standby.status = StandbyReservationStatus.RETIRED
                invalidated.append(lease_record.lease)
        return tuple(invalidated)

    def _retire_standby_for_lease_locked(self, lease_id: str) -> None:
        for standby in self._standby.values():
            if standby.lease_id == lease_id:
                standby.status = StandbyReservationStatus.RETIRED
                return

    def _clear_provisional_affinity_locked(self, lease: PlacementLease) -> None:
        context = self._run_contexts.get(lease.run_id or "")
        if (
            context is not None
            and not context.confirmed
            and context.provisional_lease_id == lease.lease_id
        ):
            context.affinity_node_id = None
            context.affinity_boot_id = None
            context.provisional_lease_id = None

    def _invalidate_affinity_locked(self, node_id: str, boot_id: str) -> None:
        for context in self._run_contexts.values():
            if (
                context.affinity_node_id == node_id
                and context.affinity_boot_id == boot_id
            ):
                context.affinity_node_id = None
                context.affinity_boot_id = None
                context.confirmed = False
                context.provisional_lease_id = None
                context.affinity_epoch += 1

    def _require_node(self, node_id: str) -> _NodeRecord:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise KeyError(f"unknown node: {node_id}") from exc

    def _require_lease(self, lease_id: str) -> _LeaseRecord:
        try:
            return self._leases[lease_id]
        except KeyError as exc:
            raise KeyError(f"unknown placement lease: {lease_id}") from exc

    def _require_standby(self, worker_id: str) -> _StandbyReservationRecord:
        try:
            return self._standby[worker_id]
        except KeyError as exc:
            raise KeyError(f"unknown Standby Worker: {worker_id}") from exc
