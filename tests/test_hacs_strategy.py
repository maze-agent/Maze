from __future__ import annotations

import time

import pytest

from maze.core.scheduler.runtime import TaskRuntime
from maze.core.scheduler.scheduler import Scheduler
from maze.core.scheduler.strategy import (
    DEFAULT_PREDICTED_DURATION_SECONDS,
    HacsSchedulingStrategy,
)


def make_task(task_kind: str, context: dict | None = None):
    resources = {"cpu_num": 1, "gpu_mem": 0, "io_num": 0}
    if task_kind == "gpu":
        resources["gpu_mem"] = 1024
    elif task_kind == "io":
        resources["io_num"] = 1
    return TaskRuntime(
        workflow_id="run-1",
        task_id=f"{task_kind}-1",
        task_input={},
        task_output={},
        resources=resources,
        task_kind=task_kind,
        code_str="def task(input_data):\n    return {}",
        scheduling_context=context or {},
    )


def test_hacs_uses_default_predicted_duration_by_task_kind():
    strategy = HacsSchedulingStrategy()
    task = make_task("gpu", {
        "mode": "static",
        "workflow_submitted_time": time.time(),
        "n_desc": 2,
        "remaining_value_tasks": 1,
    })

    metadata = strategy.refresh_task_metadata(task)

    assert metadata["predicted_duration"] == DEFAULT_PREDICTED_DURATION_SECONDS["gpu"]
    assert metadata["prediction_source"] == "task_kind_default"
    assert metadata["hacs_breakdown"]["mode"] == "static"
    assert metadata["hacs_score"] > 0


def test_hacs_dynamic_fallback_does_not_require_descendants():
    strategy = HacsSchedulingStrategy()
    task = make_task("io", {
        "mode": "dynamic",
        "workflow_submitted_time": time.time() - 10,
        "n_anc": 3,
    })

    metadata = strategy.refresh_task_metadata(task)

    assert metadata["predicted_duration"] == DEFAULT_PREDICTED_DURATION_SECONDS["io"]
    assert metadata["hacs_breakdown"]["n_desc"] == 0
    assert metadata["hacs_breakdown"]["n_anc"] == 3
    assert metadata["topological_weight"] >= 3


def test_scheduler_queue_snapshot_contains_hacs_fields():
    scheduler = Scheduler(1, 2, 6379, None, "HACS")
    task = make_task("cpu", {
        "mode": "dynamic",
        "workflow_submitted_time": time.time(),
        "n_anc": 1,
    })
    scheduler.task_queues.put(task)

    snapshot = scheduler.get_queue_snapshot()
    ready = snapshot["ready_tasks"][0]

    assert snapshot["scheduling_algorithm"] == "HACS"
    assert ready["queue_name"] == "cpu"
    assert ready["predicted_duration"] == pytest.approx(DEFAULT_PREDICTED_DURATION_SECONDS["cpu"])
    assert ready["prediction_source"] == "task_kind_default"
    assert ready["prediction_sample_count"] == 0
    assert ready["prediction_confidence"] == pytest.approx(0.0)
    assert isinstance(ready["hacs_score"], float)
    assert ready["hacs_breakdown"]["mode"] == "dynamic"
    assert snapshot["counts"]["by_queue"]["cpu"]["total"] == 1
