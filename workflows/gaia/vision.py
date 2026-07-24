"""Ascend-Maze port of the Maze GAIA vision workflow."""

from __future__ import annotations

import time

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.gaia._common import (
    empty_time_record,
    gaia_question_prompt,
    model_runtime_inputs,
    summarize_image_file,
    text_features,
    vision_content_parts,
)

SPEC = WorkflowSpec(
    name="maze-gaia-vision",
    source="gaia",
    kind="vision",
    nodes=nodes(
        (
            ("task1_obtain_content", "cpu", None),
            ("task2_vlm_process", "npu", "qwen2.5-vl-32b"),
            ("task3_output_final_answer", "io", None),
        )
    ),
    edges=edges(
        (
            ("task1_obtain_content", "task2_vlm_process"),
            ("task2_vlm_process", "task3_output_final_answer"),
        )
    ),
)

INPUTS = spec_inputs()


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task1_obtain_content(
    dag_id: str,
    question: str,
    supplementary_files: object,
) -> dict[str, object]:
    start_time = time.time()
    image_summary = summarize_image_file(supplementary_files)
    file_content = image_summary["image_bytes"]
    image_features = dict(image_summary["image_features"])
    prompt = gaia_question_prompt(question, "", "")
    prompt_features = text_features(prompt)
    task2_vlm_process_feature = {
        **image_features,
        "prompt_length": prompt_features["text_length"],
        "prompt_token_count": prompt_features["token_count"],
    }
    return {
        "file_content": file_content,
        "task2_vlm_process_feature": task2_vlm_process_feature,
        "dag_id": dag_id,
        "succ_task_feat": {
            "task2_vlm_process": task2_vlm_process_feature,
        },
        "curr_task_feat": None,
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task2_vlm_process(
    dag_id: str,
    question: str,
    file_content: bytes,
    task2_vlm_process_feature: dict[str, object],
    use_online_model: bool,
    model_folder: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    repetition_penalty: float,
    task2_vlm_process_request_api_url: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del (
        use_online_model,
        model_folder,
        top_p,
        repetition_penalty,
        task2_vlm_process_request_api_url,
    )
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    response = chat(
        [
            {
                "role": "user",
                "content": vision_content_parts(
                    question,
                    file_content,
                    task2_vlm_process_feature,
                ),
            }
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return {
        "vlm_answer": response.text,
        "task_id": dag_id,
        "curr_task_feat": task2_vlm_process_feature,
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
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


def build() -> Workflow:
    workflow = Workflow(SPEC.name)
    dag_id = workflow.input("dag_id")
    question = workflow.input("question")
    workflow.input("answer")
    supplementary_files = workflow.input("supplementary_files")
    workflow.input("metadata")

    prepared = workflow.add_task(
        task1_obtain_content,
        task_name="task1_obtain_content",
        inputs={
            "dag_id": dag_id,
            "question": question,
            "supplementary_files": supplementary_files,
        },
    )
    answered = workflow.add_task(
        task2_vlm_process,
        task_name="task2_vlm_process",
        model_anchor={"model": "qwen2.5-vl-32b", "mode": "service"},
        inputs={
            "dag_id": dag_id,
            "question": question,
            "file_content": prepared.outputs["file_content"],
            "task2_vlm_process_feature": prepared.outputs[
                "task2_vlm_process_feature"
            ],
            **model_runtime_inputs("task2_vlm_process_request_api_url"),
        },
    )
    workflow.add_task(
        task3_output_final_answer,
        task_name="task3_output_final_answer",
        inputs={
            "dag_id": dag_id,
            "vlm_answer": answered.outputs["vlm_answer"],
        },
    )
    return workflow
