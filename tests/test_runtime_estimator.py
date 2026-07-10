from __future__ import annotations

import pytest

from maze.core.scheduler.runtime_estimator import (
    PREDICTION_SOURCE_DEFAULT,
    PREDICTION_SOURCE_TASK_CODE_EMA,
    PREDICTION_SOURCE_TASK_KIND_EMA,
    RuntimeEstimator,
    code_hash_for_task,
)
from maze.core.scheduler.strategy import DEFAULT_PREDICTED_DURATION_SECONDS
from maze.core.workflow.task import CodeTask


def make_task(task_id: str, task_kind: str = "cpu", *, code: str = "def task(): pass", value=1):
    task = CodeTask("workflow-1", task_id, "unit_task")
    task.task_kind = task_kind
    task.code_str = code
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


def test_runtime_estimator_uses_task_kind_default_without_history():
    estimator = RuntimeEstimator()
    task = make_task("task-1", "gpu")

    prediction = estimator.predict(task)

    assert prediction.predicted_duration == pytest.approx(DEFAULT_PREDICTED_DURATION_SECONDS["gpu"])
    assert prediction.prediction_source == PREDICTION_SOURCE_DEFAULT
    assert prediction.sample_count == 0


def test_runtime_estimator_uses_task_kind_ema_when_code_profile_is_missing():
    estimator = RuntimeEstimator(alpha=0.2)
    observed = make_task("task-1", "cpu", code="def a(): pass")
    candidate = make_task("task-2", "cpu", code="def b(): pass")

    estimator.observe_task(observed, 12.0, success=True)
    prediction = estimator.predict(candidate)

    assert prediction.predicted_duration == pytest.approx(12.0)
    assert prediction.prediction_source == PREDICTION_SOURCE_TASK_KIND_EMA
    assert prediction.sample_count == 1


def test_runtime_estimator_prefers_task_kind_code_hash_ema():
    estimator = RuntimeEstimator(alpha=0.2)
    code_task = make_task("task-1", "io", code="def same(): pass")
    other_task = make_task("task-2", "io", code="def other(): pass")

    estimator.observe_task(code_task, 10.0, success=True)
    estimator.observe_task(other_task, 50.0, success=True)

    code_prediction = estimator.predict(code_task)
    kind_prediction = estimator.predict(make_task("task-3", "io", code="def new(): pass"))

    assert code_prediction.predicted_duration == pytest.approx(10.0)
    assert code_prediction.prediction_source == PREDICTION_SOURCE_TASK_CODE_EMA
    assert kind_prediction.predicted_duration == pytest.approx(18.0)
    assert kind_prediction.prediction_source == PREDICTION_SOURCE_TASK_KIND_EMA


def test_runtime_estimator_ignores_failed_observations():
    estimator = RuntimeEstimator()
    task = make_task("task-1", "gpu")

    estimator.observe_task(task, 7.0, success=False)
    prediction = estimator.predict(task)

    assert prediction.predicted_duration == pytest.approx(DEFAULT_PREDICTED_DURATION_SECONDS["gpu"])
    assert prediction.prediction_source == PREDICTION_SOURCE_DEFAULT


def test_code_hash_includes_task_input_parameters():
    task_a = make_task("task-1", "cpu", code="def same(): pass", value=1)
    task_b = make_task("task-2", "cpu", code="def same(): pass", value=2)

    assert code_hash_for_task(task_a) != code_hash_for_task(task_b)
