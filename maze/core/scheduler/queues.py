from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from maze.core.scheduler.strategy import SchedulingStrategy


QUEUE_NAMES = ("gpu", "cpu", "io")


@dataclass
class QueuedTask:
    key: tuple
    sequence: int
    task: Any


class HeterogeneousTaskQueues:
    """Three resource queues with stable head semantics.

    A queue head is only removed via ``pop_head`` after the scheduler has
    selected resources for that exact task. Resource-pending heads are never
    popped and reinserted, so their same-queue order cannot drift.
    """

    def __init__(self, strategy: SchedulingStrategy):
        self.strategy = strategy
        self._queues: Dict[str, List[QueuedTask]] = {name: [] for name in QUEUE_NAMES}
        self._sequence = 0
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    def put(self, task: Any, queue_name: str | None = None) -> str:
        with self._not_empty:
            resolved_queue = self._normalize_queue_name(queue_name or self.strategy.queue_name(task))
            sequence = self._sequence
            self._sequence += 1
            now = time.time()
            key = self.strategy.enqueue_key(task, sequence, now)
            setattr(task, "queue_name", resolved_queue)
            setattr(task, "queue_sequence", sequence)
            setattr(task, "queue_entered_time", now)
            item = QueuedTask(key=key, sequence=sequence, task=task)
            self._queues[resolved_queue].append(item)
            self._queues[resolved_queue].sort(key=lambda queued: queued.key)
            self._not_empty.notify()
            return resolved_queue

    def peek(self, queue_name: str, now: float | None = None) -> Any | None:
        with self._lock:
            queue = self._queues[self._normalize_queue_name(queue_name)]
            if not queue:
                return None
            task = queue[0].task
            self.strategy.refresh_task_metadata(task, now)
            return task

    def pop_head(self, queue_name: str, task: Any | None = None) -> Any:
        with self._lock:
            queue = self._queues[self._normalize_queue_name(queue_name)]
            if not queue:
                raise IndexError(f"pop from empty {queue_name} queue")
            head = queue[0].task
            if task is not None and head is not task:
                raise ValueError("cannot pop a non-head task from a resource queue")
            return queue.pop(0).task

    def wait_for_task(self, timeout: float | None = None) -> bool:
        with self._not_empty:
            return self._not_empty.wait_for(
                lambda: any(self._queues[name] for name in QUEUE_NAMES),
                timeout=timeout,
            )

    def queue_snapshot(self, now: float | None = None) -> Dict[str, List[Any]]:
        with self._lock:
            out: Dict[str, List[Any]] = {}
            for name in QUEUE_NAMES:
                tasks = []
                for queued in self._queues[name]:
                    task = queued.task
                    self.strategy.refresh_task_metadata(task, now)
                    setattr(task, "queue_name", name)
                    tasks.append(task)
                out[name] = tasks
            return out

    def queue_names(self) -> Iterable[str]:
        return QUEUE_NAMES

    def _normalize_queue_name(self, value: str | None) -> str:
        normalized = str(value or "cpu").strip().lower()
        return normalized if normalized in self._queues else "cpu"
