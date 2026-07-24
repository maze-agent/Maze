"""Single-heap deadline tracking for the scheduler event loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import heapq

from ascend_maze.core.errors import ContractValidationError


class DeadlineKind(str, Enum):
    RUN = "run"
    LEASE = "lease"
    TASK = "task"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class DeadlineEvent:
    kind: DeadlineKind
    run_id: str
    task_id: str | None
    attempt: int | None
    due_at_ms: int
    generation: int


@dataclass(order=True, frozen=True, slots=True)
class _HeapEntry:
    due_at_ms: int
    sequence: int
    event: DeadlineEvent = field(compare=False)


class DeadlineManager:
    """Own all run, task and retry timers without spawning timer threads."""

    def __init__(self) -> None:
        self._heap: list[_HeapEntry] = []
        self._active: dict[tuple[DeadlineKind, str, str | None, int | None], int] = {}
        self._next_sequence = 0

    @staticmethod
    def _key(
        kind: DeadlineKind,
        run_id: str,
        task_id: str | None,
        attempt: int | None,
    ) -> tuple[DeadlineKind, str, str | None, int | None]:
        return (kind, run_id, task_id, attempt)

    def register(
        self,
        *,
        kind: DeadlineKind,
        run_id: str,
        due_at_ms: int,
        task_id: str | None = None,
        attempt: int | None = None,
    ) -> DeadlineEvent:
        if not isinstance(run_id, str) or not run_id:
            raise ContractValidationError("deadline run_id is required")
        if isinstance(due_at_ms, bool) or not isinstance(due_at_ms, int) or due_at_ms < 0:
            raise ContractValidationError("deadline due_at_ms must be non-negative")
        if kind is DeadlineKind.RUN and (task_id is not None or attempt is not None):
            raise ContractValidationError("run deadline cannot identify a task attempt")
        if kind in {DeadlineKind.LEASE, DeadlineKind.TASK, DeadlineKind.RETRY} and (
            not isinstance(task_id, str)
            or not task_id
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or attempt < 1
        ):
            raise ContractValidationError(
                "lease/task/retry deadline requires task_id and attempt"
            )

        key = self._key(kind, run_id, task_id, attempt)
        generation = self._active.get(key, 0) + 1
        self._active[key] = generation
        self._next_sequence += 1
        event = DeadlineEvent(
            kind=kind,
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            due_at_ms=due_at_ms,
            generation=generation,
        )
        heapq.heappush(
            self._heap,
            _HeapEntry(due_at_ms, self._next_sequence, event),
        )
        return event

    def cancel(
        self,
        *,
        kind: DeadlineKind,
        run_id: str,
        task_id: str | None = None,
        attempt: int | None = None,
    ) -> bool:
        key = self._key(kind, run_id, task_id, attempt)
        return self._active.pop(key, None) is not None

    def clear_run(self, run_id: str) -> int:
        keys = [key for key in self._active if key[1] == run_id]
        for key in keys:
            del self._active[key]
        return len(keys)

    def pop_due(self, now_ms: int) -> tuple[DeadlineEvent, ...]:
        due: list[DeadlineEvent] = []
        while self._heap and self._heap[0].due_at_ms <= now_ms:
            entry = heapq.heappop(self._heap)
            event = entry.event
            key = self._key(
                event.kind,
                event.run_id,
                event.task_id,
                event.attempt,
            )
            if self._active.get(key) != event.generation:
                continue
            del self._active[key]
            due.append(event)
        return tuple(due)

    def next_due_at_ms(self) -> int | None:
        while self._heap:
            event = self._heap[0].event
            key = self._key(
                event.kind,
                event.run_id,
                event.task_id,
                event.attempt,
            )
            if self._active.get(key) == event.generation:
                return event.due_at_ms
            heapq.heappop(self._heap)
        return None

    def count_for_run(self, run_id: str) -> int:
        return sum(1 for key in self._active if key[1] == run_id)

    @property
    def active_count(self) -> int:
        return len(self._active)
