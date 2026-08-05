"""Fuse two routed-model answers for the GAIA reasoning demo."""

from __future__ import annotations

import os
import time

from maze import task
from workflows.gaia._common import (
    GAIA_FINAL_ANSWER_RULES,
    empty_time_record,
    text_features,
)


GPU_RESOURCES = {"cpu_num": 1, "gpu_mem": 8192, "io_num": 0}


def _model_route() -> tuple[str, str]:
    base_url = os.environ.get("MAZE_MODEL_ENDPOINT", "").strip()
    model = os.environ.get("MAZE_MODEL_NAME", "").strip()
    if not base_url or not model:
        raise RuntimeError("GAIA model task requires a Scheduler model route")
    return base_url, model


@task(task_kind="gpu", resources=GPU_RESOURCES, max_retries=0)
def gaia_fuse_answers(
    answer_one: str,
    answer_two: str,
    feature_one: dict[str, object],
    feature_two: dict[str, object],
    dag_id: str,
    question: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, object]:
    from workflows._inference import chat

    start_time = time.time()
    base_url, model = _model_route()
    prompt = (
        "Synthesize the two independent candidate answers below. Correct any "
        "errors and return the most accurate answer for the original question.\n\n"
        f"Original question:\n{question}\n\n"
        f"Candidate answer A:\n{answer_one}\n\n"
        f"Candidate answer B:\n{answer_two}\n\n"
        f"{GAIA_FINAL_ANSWER_RULES}"
    )
    final_answer = chat(
        [{"role": "user", "content": prompt}],
        base_url=base_url,
        model=model,
        api_key="",
        temperature=float(temperature),
        max_tokens=int(max_tokens),
    )
    prompt_features = text_features(prompt)
    return {
        "dag_id": dag_id,
        "final_answer": final_answer,
        "curr_task_feat": {
            "prompt_length": prompt_features["text_length"],
            "prompt_token_count": prompt_features["token_count"],
            "answer_one_length": feature_one["answer_length"],
            "answer_two_length": feature_two["answer_length"],
        },
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }
