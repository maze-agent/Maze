"""Correctness-mode cold Worker broker with one lease per Ray task."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from ascend_maze.contracts.resources import ExecutionTarget, PlacementLease
from ascend_maze.contracts.worker import WorkerLease, WorkerProfile
from ascend_maze.core.identifiers import new_id
from ascend_maze.runtime.ray_node_registry import RayNodeRegistry


@dataclass(slots=True)
class _WorkerLeaseRecord:
    lease: WorkerLease
    released: bool = False
    disposition: str | None = None


class ColdWorkerBroker:
    def __init__(
        self,
        *,
        node_registry: RayNodeRegistry,
        environment_fingerprint: str,
    ) -> None:
        self.node_registry = node_registry
        self.environment_fingerprint = environment_fingerprint
        self._records: dict[str, _WorkerLeaseRecord] = {}
        self._lock = RLock()

    def acquire(
        self,
        *,
        placement_lease: PlacementLease,
        task_kind: str,
        execution_target: ExecutionTarget,
        now_ms: int,
    ) -> WorkerLease:
        self.node_registry.resolve_lease(placement_lease)
        worker_id = new_id("worker")
        endpoint_id = new_id("worker_endpoint")
        lease = WorkerLease(
            worker_lease_id=new_id("worker_lease"),
            worker_endpoint_id=endpoint_id,
            worker_id=worker_id,
            worker_generation=1,
            node_id=placement_lease.node_id,
            boot_id=placement_lease.boot_id,
            profile=self._profile_for(task_kind, execution_target),
            source="cold_start",
            bound_device_id=placement_lease.npu_device_id,
            acquired_at_ms=now_ms,
        )
        with self._lock:
            self._records[lease.worker_lease_id] = _WorkerLeaseRecord(lease)
        return lease

    def release(self, worker_lease_id: str, *, disposition: str) -> bool:
        if disposition not in {"discard", "reuse"}:
            raise ValueError("unsupported WorkerLease disposition")
        with self._lock:
            record = self._records[worker_lease_id]
            if record.released:
                return False
            record.released = True
            record.disposition = disposition
            return True

    def invalidate_node(self, node_id: str, boot_id: str) -> tuple[WorkerLease, ...]:
        invalidated: list[WorkerLease] = []
        with self._lock:
            for record in self._records.values():
                lease = record.lease
                if (
                    not record.released
                    and lease.node_id == node_id
                    and lease.boot_id == boot_id
                ):
                    record.released = True
                    record.disposition = "discard"
                    invalidated.append(lease)
        return tuple(invalidated)

    def active_count(self, node_id: str | None = None) -> int:
        with self._lock:
            return sum(
                not record.released
                and (
                    node_id is None
                    or record.lease.node_id == node_id
                )
                for record in self._records.values()
            )

    def is_released(self, worker_lease_id: str) -> bool:
        with self._lock:
            record = self._records.get(worker_lease_id)
            return record is None or record.released

    def purge_released(self) -> int:
        with self._lock:
            keys = [key for key, record in self._records.items() if record.released]
            for key in keys:
                del self._records[key]
            return len(keys)

    @staticmethod
    def _profile_for(
        task_kind: str,
        execution_target: ExecutionTarget,
    ) -> WorkerProfile:
        if execution_target is ExecutionTarget.MODEL_SERVICE:
            return WorkerProfile.IO
        try:
            return {
                "cpu": WorkerProfile.CPU,
                "io": WorkerProfile.IO,
                "npu": WorkerProfile.NPU_HOST,
            }[task_kind]
        except KeyError as exc:
            raise ValueError(f"unsupported task kind: {task_kind}") from exc
