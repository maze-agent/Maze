"""Public Ascend-Maze workflow authoring API."""

from ascend_maze.api.task import task
from ascend_maze.api.workflow import OutputRef, TaskHandle, Workflow, WorkflowInputRef

__all__ = [
    "OutputRef",
    "TaskHandle",
    "Workflow",
    "WorkflowInputRef",
    "task",
]

__version__ = "0.1.0"
