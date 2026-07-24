"""Ascend platform adapters kept outside the backend-neutral core."""

from ascend_maze.ascend.contracts import (
    AscendColocationConfig,
    AscendCorrectnessConfig,
    AscendDeviceSnapshot,
    AscendEnvironmentSnapshot,
    AscendProcessSnapshot,
    create_ascend_correctness_config_snapshot,
    create_ascend_colocation_config_snapshot,
)
from ascend_maze.ascend.dcmi import DcmiDeviceAdapter, DcmiError
from ascend_maze.ascend.discovery import (
    build_ascend_node_capacity,
    build_ascend_node_observation,
    discover_aicpu_runtime_library_paths,
    discover_atb_runtime_library_preloads,
    discover_ascend_environment,
)

__all__ = [
    "AscendCorrectnessConfig",
    "AscendColocationConfig",
    "AscendDeviceSnapshot",
    "AscendEnvironmentSnapshot",
    "AscendProcessSnapshot",
    "create_ascend_correctness_config_snapshot",
    "create_ascend_colocation_config_snapshot",
    "DcmiDeviceAdapter",
    "DcmiError",
    "build_ascend_node_capacity",
    "build_ascend_node_observation",
    "discover_aicpu_runtime_library_paths",
    "discover_atb_runtime_library_preloads",
    "discover_ascend_environment",
]
