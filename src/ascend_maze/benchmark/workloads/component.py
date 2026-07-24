"""Lightweight workflow used by the C14 component microbenchmarks."""

from __future__ import annotations

from ascend_maze import Workflow, task


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 64})
def microbenchmark_barrier(value: int) -> dict[str, object]:
    import time

    time.sleep(0.001)
    return {"value": value}


def build() -> Workflow:
    workflow = Workflow("c14e-component-microbenchmark")
    value = workflow.input("value")
    workflow.add_task(
        microbenchmark_barrier,
        task_name="microbenchmark_barrier",
        inputs={"value": value},
    )
    return workflow
