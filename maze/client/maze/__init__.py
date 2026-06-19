from maze.client.maze.client import MaClient
from maze.client.maze.dynamic import DynamicRun, DynamicTaskInvocation, DynamicTaskSpec
from maze.client.maze.workflow import MaWorkflow
from maze.client.maze.models import MaTask, TaskOutput, TaskOutputs
from maze.client.maze.decorator import task, get_task_metadata
from maze.client.maze.workflow_authoring import OutputRef, TaskInvocation, WorkflowDefinition, workflow

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
    'workflow',
    'WorkflowDefinition',
    'TaskInvocation',
    'OutputRef',
]

for _legacy_module in (
    'agent_exec',
    'agent_permissions',
    'agent_sandbox',
):
    globals().pop(_legacy_module, None)
del _legacy_module
