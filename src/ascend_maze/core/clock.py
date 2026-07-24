"""Injectable monotonic/wall clocks for deterministic scheduler tests."""

from __future__ import annotations

from threading import RLock
from typing import Protocol

from ascend_maze.core.time import monotonic_time_ms, wall_time_ms


class Clock(Protocol):
    automatic_wait: bool

    def monotonic_ms(self) -> int: ...

    def wall_ms(self) -> int: ...


class SystemClock:
    automatic_wait = True

    def monotonic_ms(self) -> int:
        return monotonic_time_ms()

    def wall_ms(self) -> int:
        return wall_time_ms()


class ManualClock:
    automatic_wait = False

    def __init__(self, *, monotonic_ms: int = 0, wall_ms: int = 0) -> None:
        self._monotonic_ms = monotonic_ms
        self._wall_ms = wall_ms
        self._lock = RLock()

    def monotonic_ms(self) -> int:
        with self._lock:
            return self._monotonic_ms

    def wall_ms(self) -> int:
        with self._lock:
            return self._wall_ms

    def advance(self, milliseconds: int) -> int:
        if milliseconds < 0:
            raise ValueError("clock cannot move backwards")
        with self._lock:
            self._monotonic_ms += milliseconds
            self._wall_ms += milliseconds
            return self._monotonic_ms
