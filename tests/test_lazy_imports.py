import os
from pathlib import Path
import subprocess
import sys
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_fresh_python(*sources: str):
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT), existing_pythonpath) if part
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n\n".join(textwrap.dedent(source).strip() for source in sources),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout


HEAVY_IMPORT_ASSERTION = """
    unexpected = {
        "maze.client.maze.client",
        "maze.core.predictor.predictor",
        "pandas",
        "scipy",
        "sklearn",
        "xgboost",
    }.intersection(sys.modules)
    assert not unexpected, sorted(unexpected)
"""


STRICT_CLIENT_IMPORT_ASSERTION = """
    unexpected = {
        "maze.client",
        "maze.client.maze",
        "maze.client.maze.client",
        "maze.core.application.spec",
        "maze.core.predictor.predictor",
        "maze.core.workflow.task",
        "pandas",
        "scipy",
        "sklearn",
        "xgboost",
    }.intersection(sys.modules)
    assert not unexpected, sorted(unexpected)
"""


def test_scheduler_runner_import_does_not_load_client_or_predictor_stack():
    _run_fresh_python(
        """
        import sys
        import maze.core.scheduler.runner
        """,
        STRICT_CLIENT_IMPORT_ASSERTION,
    )


def test_plain_maze_import_is_lazy():
    _run_fresh_python(
        """
        import sys
        import maze

        assert maze.__all__ == [
            "MaClient",
            "DynamicRun",
            "DynamicTaskInvocation",
            "DynamicTaskSpec",
            "MaWorkflow",
            "MaTask",
            "TaskOutput",
            "TaskOutputs",
            "task",
            "get_task_metadata",
            "workflow",
            "WorkflowDefinition",
            "TaskInvocation",
            "OutputRef",
            "metrics",
        ]
        """,
        STRICT_CLIENT_IMPORT_ASSERTION,
    )


def test_task_export_does_not_load_unrelated_client_or_predictor_stack():
    _run_fresh_python(
        """
        import sys
        from maze import task
        from maze.client.maze.decorator import task as direct_task

        assert task is direct_task
        """,
        HEAVY_IMPORT_ASSERTION,
    )


def test_application_spec_uses_lightweight_predictor_features():
    _run_fresh_python(
        """
        import sys
        import maze.core.application.spec
        """,
        HEAVY_IMPORT_ASSERTION,
    )


def test_lazy_public_exports_preserve_identity_and_discovery():
    _run_fresh_python(
        """
        from importlib import import_module
        import maze

        expected = {
            "MaClient": ("maze.client.maze.client", "MaClient"),
            "DynamicRun": ("maze.client.maze.dynamic", "DynamicRun"),
            "DynamicTaskInvocation": ("maze.client.maze.dynamic", "DynamicTaskInvocation"),
            "DynamicTaskSpec": ("maze.client.maze.dynamic", "DynamicTaskSpec"),
            "MaWorkflow": ("maze.client.maze.workflow", "MaWorkflow"),
            "MaTask": ("maze.client.maze.models", "MaTask"),
            "TaskOutput": ("maze.client.maze.models", "TaskOutput"),
            "TaskOutputs": ("maze.client.maze.models", "TaskOutputs"),
            "task": ("maze.client.maze.decorator", "task"),
            "get_task_metadata": ("maze.client.maze.decorator", "get_task_metadata"),
            "workflow": ("maze.client.maze.workflow_authoring", "workflow"),
            "WorkflowDefinition": ("maze.client.maze.workflow_authoring", "WorkflowDefinition"),
            "TaskInvocation": ("maze.client.maze.workflow_authoring", "TaskInvocation"),
            "OutputRef": ("maze.client.maze.workflow_authoring", "OutputRef"),
        }

        for name, (module_name, attribute_name) in expected.items():
            value = getattr(maze, name)
            assert value is getattr(import_module(module_name), attribute_name)
            assert getattr(maze, name) is value

        assert maze.metrics is import_module("maze.metrics")
        assert set(maze.__all__).issubset(dir(maze))

        namespace = {}
        exec("from maze import *", namespace)
        assert set(namespace) - {"__builtins__"} == set(maze.__all__)
        for name in maze.__all__:
            assert namespace[name] is getattr(maze, name)

        try:
            maze.not_a_public_export
        except AttributeError:
            pass
        else:
            raise AssertionError("unknown exports must raise AttributeError")
        """
    )


def test_nested_client_package_preserves_workflow_decorator():
    scenarios = (
        """
        from maze.client.maze import MaWorkflow, workflow
        from maze.client.maze.workflow import MaWorkflow as direct_ma_workflow
        from maze.client.maze.workflow_authoring import workflow as direct_workflow

        assert MaWorkflow is direct_ma_workflow
        assert workflow is direct_workflow
        """,
        """
        from maze.client.maze import workflow, MaWorkflow
        from maze.client.maze.workflow import MaWorkflow as direct_ma_workflow
        from maze.client.maze.workflow_authoring import workflow as direct_workflow

        assert MaWorkflow is direct_ma_workflow
        assert workflow is direct_workflow
        """,
        """
        import maze.client.maze as client_api
        from maze.client.maze.workflow import MaWorkflow as direct_ma_workflow
        from maze.client.maze.workflow_authoring import workflow as direct_workflow

        namespace = {}
        exec("from maze.client.maze import *", namespace)
        assert namespace["MaWorkflow"] is direct_ma_workflow
        assert namespace["workflow"] is direct_workflow
        assert client_api.workflow is direct_workflow
        """,
    )
    for scenario in scenarios:
        _run_fresh_python(scenario)
