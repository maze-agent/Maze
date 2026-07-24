"""Idempotent finalization of Attempt cleanup barriers."""

from __future__ import annotations

from threading import RLock

from ascend_maze.core.errors import StateTransitionError
from ascend_maze.fault.recovery import CleanupBarrier


class RecoveryCoordinator:
    """Commit one resource-cleanup conclusion for each failed Attempt."""

    def __init__(self) -> None:
        self._barriers: dict[tuple[str, str, int], CleanupBarrier] = {}
        self._lock = RLock()

    def finalize_cleanup(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        dispatch_invalidated: bool,
        worker_released: bool,
        unpublished_data_released: bool,
        route_released: bool,
        placement_released: bool,
        node_or_device_quarantined: bool,
    ) -> CleanupBarrier:
        if placement_released and not (
            worker_released or node_or_device_quarantined
        ):
            raise StateTransitionError(
                "PlacementLease cannot be reusable before Worker cleanup or quarantine"
            )
        barrier = CleanupBarrier(
            dispatch_invalidated=dispatch_invalidated,
            worker_released=worker_released,
            unpublished_data_released=unpublished_data_released,
            route_released=route_released,
            placement_released=placement_released,
            node_or_device_quarantined=node_or_device_quarantined,
        )
        key = (run_id, task_id, attempt)
        with self._lock:
            existing = self._barriers.get(key)
            if existing is not None:
                if existing != barrier:
                    raise StateTransitionError(
                        "one Attempt produced conflicting cleanup barriers"
                    )
                return existing
            self._barriers[key] = barrier
            return barrier

    def barrier(
        self,
        run_id: str,
        task_id: str,
        attempt: int,
    ) -> CleanupBarrier | None:
        with self._lock:
            return self._barriers.get((run_id, task_id, attempt))

    def count_for_run(self, run_id: str) -> int:
        with self._lock:
            return sum(key[0] == run_id for key in self._barriers)

    def destroy_run(self, run_id: str) -> int:
        with self._lock:
            keys = [key for key in self._barriers if key[0] == run_id]
            for key in keys:
                del self._barriers[key]
            return len(keys)
