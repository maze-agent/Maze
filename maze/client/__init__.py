from importlib import import_module
from typing import TYPE_CHECKING

__all__ = [
    'MaClient',
    'DynamicRun',
    'DynamicTaskInvocation',
    'DynamicTaskSpec',
    'MaWorkflow',
    'MaTask',
    'TaskOutput',
    'TaskOutputs',
    'task',
    'get_task_metadata',
    'LanggraphClient',
]

_LAZY_EXPORTS = {
    'MaClient': ('maze.client.maze.client', 'MaClient'),
    'DynamicRun': ('maze.client.maze.dynamic', 'DynamicRun'),
    'DynamicTaskInvocation': ('maze.client.maze.dynamic', 'DynamicTaskInvocation'),
    'DynamicTaskSpec': ('maze.client.maze.dynamic', 'DynamicTaskSpec'),
    'MaWorkflow': ('maze.client.maze.workflow', 'MaWorkflow'),
    'MaTask': ('maze.client.maze.models', 'MaTask'),
    'TaskOutput': ('maze.client.maze.models', 'TaskOutput'),
    'TaskOutputs': ('maze.client.maze.models', 'TaskOutputs'),
    'task': ('maze.client.maze.decorator', 'task'),
    'get_task_metadata': ('maze.client.maze.decorator', 'get_task_metadata'),
    'LanggraphClient': ('maze.client.langgraph.client', 'LanggraphClient'),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from maze.client.langgraph.client import LanggraphClient
    from maze.client.maze.client import MaClient
    from maze.client.maze.decorator import get_task_metadata, task
    from maze.client.maze.dynamic import DynamicRun, DynamicTaskInvocation, DynamicTaskSpec
    from maze.client.maze.models import MaTask, TaskOutput, TaskOutputs
    from maze.client.maze.workflow import MaWorkflow
