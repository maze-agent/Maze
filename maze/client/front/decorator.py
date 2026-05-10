"""
Task decorators for the Playground/frontend client.
"""

from maze.client.maze.decorator import (
    TaskMetadata,
    TaskOutputInferenceError,
    get_task_metadata,
    infer_output_keys_from_source,
    task,
)


def tool(func=None, *, resources=None, data_types=None):
    return task(func, resources=resources, data_types=data_types)


__all__ = [
    "TaskMetadata",
    "TaskOutputInferenceError",
    "get_task_metadata",
    "infer_output_keys_from_source",
    "task",
    "tool",
]
