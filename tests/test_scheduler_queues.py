from __future__ import annotations

import time

import pytest

from maze.core.scheduler.queues import HeterogeneousTaskQueues
from maze.core.scheduler.runtime import TaskRuntime
from maze.core.scheduler.strategy import (
    FcfsSchedulingStrategy,
    HacsSchedulingStrategy,
    create_scheduling_strategy,
)


def make_task(task_id: str, task_kind: str = "cpu", scheduling_context: dict | None = None):
    resources = {"cpu_num": 1, "gpu_mem": 0, "io_num": 0}
    if task_kind == "gpu":
        resources["gpu_mem"] = 1024
    elif task_kind == "io":
        resources["io_num"] = 1
    return TaskRuntime(
        workflow_id="run-1",
        task_id=task_id,
        task_input={},
        task_output={},
        resources=resources,
        task_kind=task_kind,
        code_str="def task(input_data):\n    return {}",
        scheduling_context=scheduling_context or {
            "mode": "dynamic",
            "workflow_submitted_time": time.time(),
        },
    )


def test_heterogeneous_queues_keep_resource_heads_separate():
    queues = HeterogeneousTaskQueues(FcfsSchedulingStrategy())
    gpu_task = make_task("gpu-1", "gpu")
    cpu_task = make_task("cpu-1", "cpu")
    io_task = make_task("io-1", "io")

    queues.put(gpu_task)
    queues.put(cpu_task)
    queues.put(io_task)

    gpu_task.pending_reason = "insufficient_gpu_mem"

    assert queues.peek("gpu") is gpu_task
    assert queues.peek("cpu") is cpu_task
    assert queues.peek("io") is io_task


def test_same_queue_head_is_not_skipped_or_requeued():
    queues = HeterogeneousTaskQueues(FcfsSchedulingStrategy())
    first = make_task("gpu-1", "gpu")
    second = make_task("gpu-2", "gpu")

    queues.put(first)
    queues.put(second)
    first.pending_reason = "insufficient_gpu_mem"

    assert queues.peek("gpu") is first
    assert queues.peek("gpu") is first
    with pytest.raises(ValueError):
        queues.pop_head("gpu", second)
    assert queues.peek("gpu") is first


def test_retry_wait_keeps_queue_head_order():
    queues = HeterogeneousTaskQueues(FcfsSchedulingStrategy())
    first = make_task("cpu-1", "cpu")
    second = make_task("cpu-2", "cpu")

    queues.put(first)
    queues.put(second)
    first.next_eligible_time = time.time() + 30

    assert queues.peek("cpu") is first
    assert queues.snapshot()[0] is first


def test_fcfs_strategy_ignores_priority_and_preserves_arrival_order():
    queues = HeterogeneousTaskQueues(FcfsSchedulingStrategy())
    first = make_task("cpu-1", "cpu")
    second = make_task("cpu-2", "cpu")
    first.set_priority(100)
    second.set_priority(1)

    queues.put(first)
    queues.put(second)

    assert queues.peek("cpu") is first
    assert [task.task_id for task in queues.snapshot()] == ["cpu-1", "cpu-2"]


def test_strategy_factory_creates_fcfs_strategy():
    assert isinstance(create_scheduling_strategy("FCFS"), FcfsSchedulingStrategy)
    assert isinstance(create_scheduling_strategy(None), FcfsSchedulingStrategy)
    assert isinstance(create_scheduling_strategy("HACS"), HacsSchedulingStrategy)
    with pytest.raises(ValueError):
        create_scheduling_strategy("unknown")


def test_hacs_equal_scores_preserve_arrival_order():
    strategy = HacsSchedulingStrategy()
    queues = HeterogeneousTaskQueues(strategy)
    context = {
        "mode": "static",
        "workflow_submitted_time": time.time(),
        "n_desc": 1,
        "n_anc": 0,
        "remaining_value_tasks": 0,
    }
    first = make_task("cpu-1", "cpu", context)
    second = make_task("cpu-2", "cpu", context)

    queues.put(first)
    queues.put(second)

    assert queues.peek("cpu") is first
    assert [task.task_id for task in queues.snapshot()] == ["cpu-1", "cpu-2"]
