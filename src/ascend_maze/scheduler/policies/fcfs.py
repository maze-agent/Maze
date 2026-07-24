"""Stable FIFO ordering independent of placement and runtime mechanisms."""

from __future__ import annotations

import heapq

from ascend_maze.scheduler.contracts import (
    DispatchProposal,
    PolicyCapabilities,
    QueueToken,
    SchedulableTaskView,
)


class FcfsPolicy:
    name = "fcfs"
    version = "1"
    capabilities = PolicyCapabilities(
        requires_prediction=False,
        requires_static_topology=False,
        supports_incremental_dag=True,
        uses_cluster_snapshot=False,
    )

    def __init__(self) -> None:
        self._heaps: dict[
            str,
            list[tuple[int, int, str, str, int, QueueToken]],
        ] = {}
        self._active: set[QueueToken] = set()
        self._token_partitions: dict[QueueToken, str] = {}

    def register_run(
        self,
        *,
        run_id: str,
        submitted_at_ms: int,
        total_value_tasks: int,
    ) -> None:
        del run_id, submitted_at_ms, total_value_tasks

    def unregister_run(self, run_id: str) -> None:
        del run_id

    def enqueue(self, partition: str, task: SchedulableTaskView) -> None:
        token = task.queue_token
        if token in self._active:
            return
        self._active.add(token)
        self._token_partitions[token] = partition
        heapq.heappush(
            self._heaps.setdefault(partition, []),
            (
                task.queued_at_ms,
                task.enqueue_sequence,
                token.task_key.run_id,
                token.task_key.task_id,
                token.queue_generation,
                token,
            ),
        )

    def depart(self, token: QueueToken) -> None:
        self._active.discard(token)
        partition = self._token_partitions.pop(token, None)
        if partition is None:
            return
        heap = self._heaps.get(partition)
        if heap is None:
            return
        self._heaps[partition] = [
            entry for entry in heap if entry[-1] in self._active
        ]
        heapq.heapify(self._heaps[partition])

    def propose(self, partition: str, limit: int) -> tuple[DispatchProposal, ...]:
        if limit < 1:
            return ()
        heap = self._heaps.get(partition, [])
        active_entries = [entry for entry in heap if entry[-1] in self._active]
        smallest = heapq.nsmallest(limit, active_entries)
        return tuple(
            DispatchProposal(
                task_key=entry[-1].task_key,
                queue_generation=entry[-1].queue_generation,
                policy_metadata=(("queued_at_ms", entry[0]),),
            )
            for entry in smallest
        )

    def task_succeeded(self, *, run_id: str, task_id: str, task_kind: str) -> None:
        del run_id, task_id, task_kind

    def run_terminal(
        self,
        *,
        run_id: str,
        status: str,
        finished_at_ms: int,
    ) -> None:
        del run_id, status, finished_at_ms

    def active_count(self) -> int:
        return len(self._active)

    def record_count(self) -> int:
        return sum(len(heap) for heap in self._heaps.values())
