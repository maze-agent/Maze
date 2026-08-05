"""Generate one routed-model answer for the GAIA reasoning demo."""

from __future__ import annotations

import os
import time

from maze import task
from workflows.gaia._common import (
    empty_time_record,
    gaia_question_prompt,
    text_feature_for_answer,
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
def gaia_model_answer(
    dag_id: str,
    question: str,
    style: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, object]:
    from workflows._inference import chat

    start_time = time.time()
    base_url, model = _model_route()
    normalized_style = str(style).strip().lower()
    if normalized_style == "reasoned":
        prompt = gaia_question_prompt(question, "", "")
    elif normalized_style == "concise":
        prompt = (
            f"{gaia_question_prompt(question, '', '')}\n"
            "Solve the question independently and keep the reasoning concise."
        )
    else:
        raise ValueError("style must be reasoned or concise")

    answer = chat(
        [{"role": "user", "content": prompt}],
        base_url=base_url,
        model=model,
        api_key="",
        temperature=float(temperature),
        max_tokens=int(max_tokens),
    )
    return {
        "answer": answer,
        "answer_feature": text_feature_for_answer("answer", answer),
        "style": normalized_style,
        "dag_id": dag_id,
        "curr_task_feat": text_features(prompt),
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }
