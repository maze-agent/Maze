"""In-memory stage-two submission controller and RuntimeClient."""

from ascend_maze.control.client import InMemoryRuntimeClient, PreparedSubmission
from ascend_maze.control.controller import (
    InMemoryController,
    SubmissionOutcome,
    SubmitRequest,
)
from ascend_maze.control.lifecycle import (
    ControllerLifecycleState,
    NodeAction,
    NodeActionResult,
    ShutdownMode,
    ShutdownResource,
    ShutdownResult,
)
from ascend_maze.control.recovery import (
    ControllerCheckpoint,
    ControllerRecoveryStore,
    InMemoryControllerRecoveryStore,
    RecoveryClaim,
    RecoveryIdentity,
    SqliteControllerRecoveryStore,
)
from ascend_maze.control.local_rpc import UdsRuntimeClient

RuntimeClient = UdsRuntimeClient

__all__ = [
    "InMemoryController",
    "InMemoryRuntimeClient",
    "PreparedSubmission",
    "ControllerLifecycleState",
    "NodeAction",
    "NodeActionResult",
    "ShutdownMode",
    "ShutdownResource",
    "ShutdownResult",
    "SubmissionOutcome",
    "ControllerCheckpoint",
    "ControllerRecoveryStore",
    "InMemoryControllerRecoveryStore",
    "RecoveryClaim",
    "RecoveryIdentity",
    "SqliteControllerRecoveryStore",
    "SubmitRequest",
    "RuntimeClient",
    "UdsRuntimeClient",
]
