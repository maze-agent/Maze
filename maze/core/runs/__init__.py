"""Run-level state, persistence, and metrics for static workflows.

The schema is designed so that future merges with dynamic / react runs
can reuse the same storage and event format (kind="static" today,
"dynamic"/"react" reserved for future use).
"""

from maze.core.runs.static_snapshot import (
    RunMetrics,
    StaticRunSnapshot,
    TaskSnapshot,
    merge_metrics,
)
from maze.core.runs.static_store import StaticRunStore
from maze.core.runs.global_metrics import GlobalMetrics

__all__ = [
    "RunMetrics",
    "StaticRunSnapshot",
    "TaskSnapshot",
    "merge_metrics",
    "StaticRunStore",
    "GlobalMetrics",
]
