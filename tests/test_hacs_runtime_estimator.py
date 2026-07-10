from __future__ import annotations

import pytest

from maze.core.path.path import MaPath
from maze.core.scheduler.runtime_estimator import RuntimeEstimator
from maze.core.scheduler.strategy import DEFAULT_PREDICTED_DURATION_SECONDS
from maze.core.workflow.dynamic import DynamicRun
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


def make_mapath(estimator: RuntimeEstimator | None = None):
    path = MaPath.__new__(MaPath)
    path.strategy = "HACS"
    path.runtime_estimator = estimator or RuntimeEstimator()
    return path


def make_task(workflow_id: str, task_id: str, task_kind: str = "gpu", value=1):
    task = CodeTask(workflow_id, task_id, "runtime_task")
    task.task_kind = task_kind
    task.code_str = "def runtime_task(input_data):\n    return input_data"
    task.code_ser = None
    task.task_input = {
        "input_params": {
            "x": {
                "input_schema": "from_user",
                "key": "x",
                "value": value,
            }
        }
    }
    return task


def test_static_hacs_context_uses_runtime_estimator_prediction_when_available():
    estimator = RuntimeEstimator()
    workflow = Workflow("workflow-1")
    task = make_task("workflow-1", "task-1", "gpu")
    workflow.add_task(task.task_id, task)
    workflow.prepare_for_strategy("HACS")
    estimator.observe_task(task, 12.5, success=True)
    path = make_mapath(estimator)

    context = path._static_scheduling_context(workflow, task.task_id, "run-1")

    assert context["predicted_duration"] == pytest.approx(12.5)
    assert context["prediction_source"] == "task_code_ema"
    assert context["prediction_sample_count"] == 1
    assert context["code_hash"]
    assert workflow.graph.nodes[task.task_id]["predicted_duration"] == pytest.approx(12.5)


def test_static_hacs_context_uses_default_without_runtime_history():
    workflow = Workflow("workflow-1")
    task = make_task("workflow-1", "task-1", "gpu")
    workflow.add_task(task.task_id, task)
    workflow.prepare_for_strategy("HACS")
    path = make_mapath()

    context = path._static_scheduling_context(workflow, task.task_id, "run-1")

    assert context["predicted_duration"] == DEFAULT_PREDICTED_DURATION_SECONDS["gpu"]
    assert context["prediction_source"] == "task_kind_default"
    assert context["prediction_sample_count"] == 0


def test_dynamic_hacs_context_uses_runtime_estimator_prediction_when_available():
    estimator = RuntimeEstimator()
    dynamic_run = DynamicRun("run-1")
    task = make_task("run-1", "task-1", "io")
    dynamic_run.tasks[task.task_id] = task
    dynamic_run.task_parents[task.task_id] = set()
    estimator.observe_task(task, 4.25, success=True)
    path = make_mapath(estimator)

    context = path._dynamic_scheduling_context(dynamic_run, task)

    assert context["predicted_duration"] == pytest.approx(4.25)
    assert context["prediction_source"] == "task_code_ema"
    assert context["prediction_sample_count"] == 1
    assert context["mode"] == "dynamic"
