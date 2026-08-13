"""GAIA image workflow implemented with Maze's public API."""

from __future__ import annotations

from base64 import b64encode
import time

from maze import MaClient, task, workflow

from workflows.gaia._common import empty_time_record, read_input_file, text_features


IMAGE_EXTENSIONS = frozenset({".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"})


@task(
    resources={"cpu": 1},
    max_retries=1,
    retry_on=["node_lost", "resource_unavailable", "artifact_error"],
)
def task1_image_process(
    dag_id: str,
    question: str,
    supplementary_path: str,
) -> dict[str, object]:
    start_time = time.time()
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    file_info = read_input_file(
        supplementary_path,
        allowed_extensions=IMAGE_EXTENSIONS,
        kind="image",
    )
    image_feature = {
        "file_name": file_info["file_name"],
        "file_extension": file_info["file_extension"],
        "mime_type": file_info["mime_type"],
        "content_sha256": file_info["content_sha256"],
        "size_bytes": file_info["size_bytes"],
        **text_features(question),
    }
    return {
        "dag_id": dag_id,
        "image_base64": b64encode(bytes(file_info["content"])).decode("ascii"),
        "mime_type": file_info["mime_type"],
        "image_feature": image_feature,
        "curr_task_feat": None,
        "succ_task_feat": {"task2_vlm_process": image_feature},
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(resources={"cpu": 1}, max_retries=0)
def task2_vlm_process(
    dag_id: str,
    question: str,
    image_base64: str,
    mime_type: str,
    image_feature: dict[str, object],
    base_url: str,
    model: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, object]:
    from workflows._inference import chat
    from workflows.gaia._common import gaia_question_prompt

    start_time = time.time()
    prompt = gaia_question_prompt(question, "", "")
    answer = chat(
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
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
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return {
        "dag_id": dag_id,
        "vlm_answer": answer,
        "curr_task_feat": image_feature,
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(resources={"cpu": 1})
def task3_output_final_answer(
    dag_id: str,
    vlm_answer: str,
) -> dict[str, object]:
    start_time = time.time()
    return {
        "dag_id": dag_id,
        "final_answer": vlm_answer,
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@workflow
def gaia_vision(
    dag_id: str,
    question: str,
    supplementary_path: str,
    vision_base_url: str,
    vision_model: str,
    vision_api_key: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
):
    prepared = task1_image_process(
        dag_id=dag_id,
        question=question,
        supplementary_path=supplementary_path,
    )
    answered = task2_vlm_process(
        dag_id=prepared.dag_id,
        question=question,
        image_base64=prepared.image_base64,
        mime_type=prepared.mime_type,
        image_feature=prepared.image_feature,
        base_url=vision_base_url,
        model=vision_model,
        api_key=vision_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    output = task3_output_final_answer(
        dag_id=answered.dag_id,
        vlm_answer=answered.vlm_answer,
    )
    return {"final_answer": output.final_answer}


def create_template(
    *,
    server_url: str,
    vision_base_url: str,
    vision_model: str,
    vision_api_key: str,
):
    client = MaClient(server_url)
    return client.create_workflow_from(
        gaia_vision,
        inputs={
            "vision_base_url": vision_base_url,
            "vision_model": vision_model,
            "vision_api_key": vision_api_key,
        },
    )


__all__ = [
    "create_template",
    "gaia_vision",
    "task1_image_process",
    "task2_vlm_process",
    "task3_output_final_answer",
]
