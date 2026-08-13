"""Answer one GAIA image question through a routed multimodal model."""

from __future__ import annotations

import os

from maze import task
from workflows.gaia._common import gaia_question_prompt


GPU_RESOURCES = {"cpu_num": 1, "gpu_mem": 20480, "io_num": 0}


@task(task_kind="gpu", resources=GPU_RESOURCES, max_retries=0)
def gaia_vision_answer(
    dag_id: str,
    question: str,
    image_base64: str,
    mime_type: str,
) -> dict[str, object]:
    from workflows._inference import chat

    base_url = os.environ.get("MAZE_MODEL_ENDPOINT", "").strip()
    model = os.environ.get("MAZE_MODEL_NAME", "").strip()
    if not base_url or not model:
        raise RuntimeError("GAIA vision task requires a Scheduler model route")
    answer = chat(
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": gaia_question_prompt(question, "", "")},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{image_base64}",
                    },
                },
            ],
        }],
        base_url=base_url,
        model=model,
        api_key="",
        temperature=0.0,
        max_tokens=2048,
    )
    return {"dag_id": dag_id, "answer": answer}
