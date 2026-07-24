"""Ascend-Maze-native GAIA reasoning workflow."""

from __future__ import annotations

import time

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.gaia._common import (
    gaia_deepseek_prompt,
    gaia_fusion_prompt,
    gaia_initial_prompt,
    gaia_question_prompt,
    text_feature_for_answer,
    text_features,
)

SPEC = WorkflowSpec(
    name="maze-gaia-reason",
    source="gaia",
    kind="reason",
    nodes=nodes(
        (
            ("task1_obtain_content", "io", None),
            ("task2_llm_process_qwen", "npu", "qwen3-32b"),
            ("task3_llm_process_deepseek", "npu", "deepseek-r1-32b"),
            ("task4_llm_fuse_answer", "npu", "qwen3-32b"),
        )
    ),
    edges=edges(
        (
            ("task1_obtain_content", "task2_llm_process_qwen"),
            ("task1_obtain_content", "task3_llm_process_deepseek"),
            ("task2_llm_process_qwen", "task4_llm_fuse_answer"),
            ("task3_llm_process_deepseek", "task4_llm_fuse_answer"),
        )
    ),
)

INPUTS = spec_inputs()


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task1_obtain_content(
    dag_id: str,
    question: str,
) -> dict[str, object]:
    start_time = time.time()
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_initial_prompt(question)
    features = text_features(prompt)
    return {
        "succ_task_feat": {
            "task2_llm_process_qwen": {
                "text_length": features["text_length"],
                "token_count": features["token_count"],
                "reason": 1,
            },
            "task3_llm_process_deepseek": {
                "text_length": features["text_length"],
                "token_count": features["token_count"],
                "reason": 0,
            },
        },
        "dag_id": dag_id,
        "curr_task_feat": None,
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": {
            "get_time": 0.0,
            "put_size_bytes": 0,
            "get_size_bytes": 0,
        },
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task2_llm_process_qwen(
    dag_id: str,
    question: str,
    use_online_model: bool,
    model_folder: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    repetition_penalty: float,
    task2_llm_process_qwen_request_api_url: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del (
        use_online_model,
        model_folder,
        top_p,
        repetition_penalty,
        task2_llm_process_qwen_request_api_url,
    )
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_question_prompt(
        question,
        "Extracted text from file",
        question,
    )
    response = chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    qwen_answer = response.text
    text1_feature = text_feature_for_answer("text1", qwen_answer)
    next_prompt = gaia_fusion_prompt(question, qwen_answer, "")
    return {
        "qwen_answer": qwen_answer,
        "text1_feature": text1_feature,
        "dag_id": dag_id,
        "curr_task_feat": text_features(prompt, reason=0),
        "succ_task_feat": {
            "task4_llm_fuse_answer": {
                "prompt_length": len(next_prompt),
                "prompt_token_count": text_features(next_prompt)["token_count"],
                "text1_length": text1_feature["text1_length"],
                "text1_token_count": text1_feature["text1_token_count"],
                "reason": 0,
            }
        },
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": {
            "get_time": 0.0,
            "put_size_bytes": 0,
            "get_size_bytes": 0,
        },
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task3_llm_process_deepseek(
    dag_id: str,
    question: str,
    use_online_model: bool,
    model_folder: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    repetition_penalty: float,
    task3_llm_process_deepseek_request_api_url: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del (
        use_online_model,
        model_folder,
        top_p,
        repetition_penalty,
        task3_llm_process_deepseek_request_api_url,
    )
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_deepseek_prompt(
        question,
        "Extracted text from file",
        question,
    )
    response = chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    deepseek_answer = response.text
    text2_feature = text_feature_for_answer("text2", deepseek_answer)
    next_prompt = gaia_fusion_prompt(question, "", deepseek_answer)
    return {
        "deepseek_answer": deepseek_answer,
        "text2_feature": text2_feature,
        "dag_id": dag_id,
        "curr_task_feat": text_features(prompt, reason=1),
        "succ_task_feat": {
            "task4_llm_fuse_answer": {
                "prompt_length": len(next_prompt),
                "prompt_token_count": text_features(next_prompt)["token_count"],
                "text2_length": text2_feature["text2_length"],
                "text2_token_count": text2_feature["text2_token_count"],
                "reason": 0,
            }
        },
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": {
            "get_time": 0.0,
            "put_size_bytes": 0,
            "get_size_bytes": 0,
        },
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task4_llm_fuse_answer(
    qwen_answer: str,
    deepseek_answer: str,
    dag_id: str,
    question: str,
    text1_feature: dict[str, object],
    text2_feature: dict[str, object],
    use_online_model: bool,
    model_folder: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    repetition_penalty: float,
    task4_llm_fuse_answer_request_api_url: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del (
        use_online_model,
        model_folder,
        top_p,
        repetition_penalty,
        task4_llm_fuse_answer_request_api_url,
    )
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_fusion_prompt(question, qwen_answer, deepseek_answer)
    response = chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    final_answer = response.text
    merged_features = text_features(prompt)
    return {
        "dag_id": dag_id,
        "final_answer": final_answer,
        "curr_task_feat": {
            "prompt_length": merged_features["text_length"],
            "prompt_token_count": merged_features["token_count"],
            "text1_length": text1_feature["text1_length"],
            "text1_token_count": text1_feature["text1_token_count"],
            "text2_length": text2_feature["text2_length"],
            "text2_token_count": text2_feature["text2_token_count"],
            "reason": 0,
        },
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": {
            "get_time": 0.0,
            "put_size_bytes": 0,
            "get_size_bytes": 0,
        },
    }


def build() -> Workflow:
    workflow = Workflow(SPEC.name)
    dag_id = workflow.input("dag_id")
    question = workflow.input("question")
    workflow.input("answer")
    workflow.input("supplementary_files")
    workflow.input("metadata")

    prepared = workflow.add_task(
        task1_obtain_content,
        task_name="task1_obtain_content",
        inputs={
            "dag_id": dag_id,
            "question": question,
        },
    )
    qwen = workflow.add_task(
        task2_llm_process_qwen,
        task_name="task2_llm_process_qwen",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": dag_id,
            "question": question,
            "use_online_model": False,
            "model_folder": "",
            "temperature": 0.0,
            "max_tokens": 4096,
            "top_p": 0.9,
            "repetition_penalty": 1.1,
            "task2_llm_process_qwen_request_api_url": "",
        },
    )
    deepseek = workflow.add_task(
        task3_llm_process_deepseek,
        task_name="task3_llm_process_deepseek",
        model_anchor={"model": "deepseek-r1-32b", "mode": "service"},
        inputs={
            "dag_id": dag_id,
            "question": question,
            "use_online_model": False,
            "model_folder": "",
            "temperature": 0.0,
            "max_tokens": 4096,
            "top_p": 0.9,
            "repetition_penalty": 1.1,
            "task3_llm_process_deepseek_request_api_url": "",
        },
    )
    workflow.add_edge(prepared, qwen)
    workflow.add_edge(prepared, deepseek)
    workflow.add_task(
        task4_llm_fuse_answer,
        task_name="task4_llm_fuse_answer",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "qwen_answer": qwen.outputs["qwen_answer"],
            "deepseek_answer": deepseek.outputs["deepseek_answer"],
            "dag_id": dag_id,
            "question": question,
            "text1_feature": qwen.outputs["text1_feature"],
            "text2_feature": deepseek.outputs["text2_feature"],
            "use_online_model": False,
            "model_folder": "",
            "temperature": 0.0,
            "max_tokens": 4096,
            "top_p": 0.9,
            "repetition_penalty": 1.1,
            "task4_llm_fuse_answer_request_api_url": "",
        },
    )
    return workflow
