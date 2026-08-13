"""Expose a GAIA model answer as the final workflow result."""

from __future__ import annotations

from maze import task


@task(task_kind="cpu", resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0})
def gaia_output_answer(dag_id: str, answer: str) -> dict[str, object]:
    return {"dag_id": dag_id, "final_answer": answer}
