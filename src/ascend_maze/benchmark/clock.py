"""Absolute-deadline clocks used by the C14 arrival orchestrator."""

from __future__ import annotations

import asyncio
from typing import Protocol

from ascend_maze.core.time import monotonic_time_ms, wall_time_ms


class BenchmarkClock(Protocol):
    def monotonic_ms(self) -> int: ...

    def wall_ms(self) -> int: ...

    async def wait_until(self, deadline_ms: int) -> int: ...


class SystemBenchmarkClock:
    def monotonic_ms(self) -> int:
        return monotonic_time_ms()

    def wall_ms(self) -> int:
        return wall_time_ms()

    async def wait_until(self, deadline_ms: int) -> int:
        while True:
            remaining_ms = deadline_ms - self.monotonic_ms()
            if remaining_ms <= 0:
                return self.monotonic_ms()
            await asyncio.sleep(remaining_ms / 1_000)


class VirtualBenchmarkClock:
    """A deterministic clock whose waits advance directly to absolute deadlines."""

    def __init__(self, *, monotonic_ms: int = 0, wall_ms: int = 0) -> None:
        if monotonic_ms < 0 or wall_ms < 0:
            raise ValueError("virtual clock values must be non-negative")
        self._monotonic_ms = monotonic_ms
        self._wall_ms = wall_ms
        self.waited_deadlines: list[int] = []

    def monotonic_ms(self) -> int:
        return self._monotonic_ms

    def wall_ms(self) -> int:
        return self._wall_ms

    async def wait_until(self, deadline_ms: int) -> int:
        if deadline_ms < 0:
            raise ValueError("deadline must be non-negative")
        self.waited_deadlines.append(deadline_ms)
        if deadline_ms > self._monotonic_ms:
            self.advance(deadline_ms - self._monotonic_ms)
        await asyncio.sleep(0)
        return self._monotonic_ms

    def advance(self, milliseconds: int) -> int:
        if milliseconds < 0:
            raise ValueError("virtual clock cannot move backwards")
        self._monotonic_ms += milliseconds
        self._wall_ms += milliseconds
        return self._monotonic_ms
