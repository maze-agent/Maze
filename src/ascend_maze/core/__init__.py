"""Runtime-independent core utilities."""

from ascend_maze.core.canonical import (
    CanonicalValue,
    FrozenMap,
    canonical_bytes,
    canonical_digest,
    canonical_size,
    freeze_canonical,
)
from ascend_maze.core.identifiers import GenerationRef, new_id, stable_id
from ascend_maze.core.time import monotonic_time_ms, wall_time_ms

__all__ = [
    "CanonicalValue",
    "FrozenMap",
    "GenerationRef",
    "canonical_bytes",
    "canonical_digest",
    "canonical_size",
    "freeze_canonical",
    "monotonic_time_ms",
    "new_id",
    "stable_id",
    "wall_time_ms",
]
