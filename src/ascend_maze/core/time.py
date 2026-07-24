"""Integer millisecond clock helpers."""

import time


def monotonic_time_ms() -> int:
    return int(time.monotonic() * 1000)


def wall_time_ms() -> int:
    return int(time.time() * 1000)
