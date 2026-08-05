"""Prepare one public question for the GAIA reasoning demo."""

from __future__ import annotations

import time

from maze import task
from workflows.gaia._common import empty_time_record, text_features


@task(task_kind="cpu", resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0})
def gaia_prepare_question(dag_id: str, question: str) -> dict[str, object]:
    start_time = time.time()
    normalized_question = str(question).strip()
    if not normalized_question:
        raise ValueError(f"task {dag_id} missing Question")
    return {
        "dag_id": str(dag_id).strip() or "gaia-ui-demo",
        "question": normalized_question,
        "question_features": text_features(normalized_question),
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }
