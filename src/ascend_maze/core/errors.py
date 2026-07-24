"""Stable exceptions raised before distributed execution begins."""


class AscendMazeError(Exception):
    """Base class for Ascend-Maze errors."""


class CanonicalizationError(AscendMazeError, ValueError):
    """Raised when a value cannot be represented deterministically."""


class LiteralSizeError(CanonicalizationError):
    """Raised when a literal exceeds a configured canonical byte limit."""


class TaskDefinitionError(AscendMazeError, ValueError):
    """Raised when a callable violates the phase-one task contract."""


class TaskOutputInferenceError(TaskDefinitionError):
    """Raised when static output names cannot be proven."""


class WorkflowValidationError(AscendMazeError, ValueError):
    """Raised when a workflow cannot be compiled."""


class WorkflowFrozenError(AscendMazeError, RuntimeError):
    """Raised when a compiled workflow is modified."""


class ContractValidationError(AscendMazeError, ValueError):
    """Raised when a cross-component contract object is invalid."""


class ExperimentValidationError(ContractValidationError):
    """Raised when a C14 experiment cannot be planned deterministically."""


class EnvironmentValidationError(ContractValidationError):
    """Raised when the local cluster environment fails a hard check."""


class ModelValidationError(ContractValidationError):
    """Raised when a configured model catalog or artifact is invalid."""


class StateTransitionError(AscendMazeError, RuntimeError):
    """Raised when a lifecycle transition violates the state machine."""


class DataStoreError(AscendMazeError, RuntimeError):
    """Base class for data storage and ownership failures."""


class DataHandleInvalidError(DataStoreError):
    """Raised when a data handle is unknown, released or generation-stale."""


class DataOwnershipError(DataStoreError):
    """Raised when an ownership transition is invalid."""


class DataStoreWriteError(DataStoreError):
    """Raised when a staged value cannot be stored."""


class RunDataIndexError(DataStoreError):
    """Raised when a run data index operation is invalid."""


class SubmissionConflictError(AscendMazeError, RuntimeError):
    """Raised when one submission ID is reused with a different payload."""


class SubmissionAbortedError(AscendMazeError, RuntimeError):
    """Raised when submission prepare or commit explicitly aborts."""


class ResponseLostError(AscendMazeError, ConnectionError):
    """Raised after a committed operation loses its response."""


class RunNotTerminalError(AscendMazeError, RuntimeError):
    """Raised when destroy is requested for a non-terminal run."""
