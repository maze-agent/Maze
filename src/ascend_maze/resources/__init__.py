"""Resource anchoring independent of placement and runtime backends."""

from ascend_maze.resources.anchors import (
    DeclaredOnlyAnchorProvider,
    OomReanchorResult,
    ResourceAnchor,
    ResourceAnchorProvider,
    StaticAnchorProvider,
)

__all__ = [
    "DeclaredOnlyAnchorProvider",
    "OomReanchorResult",
    "ResourceAnchor",
    "ResourceAnchorProvider",
    "StaticAnchorProvider",
]
