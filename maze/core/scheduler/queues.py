from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List

from maze.core.scheduler.strategy import SchedulingStrategy


QUEUE_NAMES = ("gpu", "cpu", "io")


@dataclass
class QueuedTask:
    key: tuple
    sequence: int
    task: Any


class HeterogeneousTaskQueues:
    """Three resource queues with dispatch-time priority refresh.

    HACS scores are refreshed before selection. A selected task is removed by
    identity so concurrent appends cannot invalidate the scheduler's claim.
    """

    def __init__(
        self,
        strategy: SchedulingStrategy,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.strategy = strategy
        self._clock = clock
        self._queues: Dict[str, List[QueuedTask]] = {
            name: [] for name in QUEUE_NAMES
        }
        self._sequence = 0
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    def put(self, task: Any, queue_name: str | None = None) -> str:
        with self._not_empty:
            resolved_queue = self._normalize_queue_name(
                queue_name or self.strategy.queue_name(task)
            )
            sequence = self._sequence
            self._sequence += 1
            now = self._clock()
            setattr(task, "queue_name", resolved_queue)
            setattr(task, "queue_sequence", sequence)
            setattr(task, "queue_entered_time", now)
            key = self.strategy.priority_key(task, sequence, now)
            item = QueuedTask(key=key, sequence=sequence, task=task)
            self._queues[resolved_queue].append(item)
            self._not_empty.notify()
            return resolved_queue

    def peek(self, queue_name: str, now: float | None = None) -> Any | None:
        with self._lock:
            resolved_queue = self._normalize_queue_name(queue_name)
            queue = self._refresh_queue(resolved_queue, now)
            if not queue:
                return None
            return queue[0].task

    def pop_head(self, queue_name: str, task: Any | None = None) -> Any:
        return self.take(queue_name, task).task

    def take(self, queue_name: str, task: Any | None = None) -> QueuedTask:
        with self._lock:
            queue = self._queues[self._normalize_queue_name(queue_name)]
            if not queue:
                raise IndexError(f"pop from empty {queue_name} queue")
            if task is None:
                return queue.pop(0)
            for index, queued in enumerate(queue):
                if queued.task is task:
                    return queue.pop(index)
            raise ValueError("cannot pop a task that is not in the resource queue")

    def restore(self, queued: QueuedTask, queue_name: str | None = None) -> str:
        with self._not_empty:
            resolved_queue = self._normalize_queue_name(
                queue_name or getattr(queued.task, "queue_name", None)
            )
            setattr(queued.task, "queue_name", resolved_queue)
            self._queues[resolved_queue].append(queued)
            self._not_empty.notify()
            return resolved_queue

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
                for queued in self._refresh_queue(name, now):
                    task = queued.task
                    setattr(task, "queue_name", name)
                    tasks.append(task)
                out[name] = tasks
            return out

    def queue_names(self) -> Iterable[str]:
        return QUEUE_NAMES

    def _normalize_queue_name(self, value: str | None) -> str:
        normalized = str(value or "cpu").strip().lower()
        return normalized if normalized in self._queues else "cpu"

    def _refresh_queue(
        self,
        queue_name: str,
        now: float | None = None,
    ) -> List[QueuedTask]:
        refresh_time = self._clock() if now is None else float(now)
        queue = self._queues[queue_name]
        for queued in queue:
            queued.key = self.strategy.priority_key(
                queued.task,
                queued.sequence,
                refresh_time,
            )
        queue.sort(key=lambda queued: queued.key)
        return queue
