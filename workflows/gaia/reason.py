"""GAIA reasoning workflow implemented with Maze's public API."""

from __future__ import annotations

import time

from maze import MaClient, task, workflow

from workflows.gaia._common import (
    empty_time_record,
    gaia_deepseek_prompt,
    gaia_fusion_prompt,
    gaia_initial_prompt,
    gaia_question_prompt,
    text_feature_for_answer,
    text_features,
)


@task(resources={"cpu": 1})
def task1_obtain_content(dag_id: str, question: str) -> dict[str, object]:
    start_time = time.time()
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    features = text_features(gaia_initial_prompt(question))
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
        "time_record": empty_time_record(),
    }


@task(resources={"cpu": 1}, max_retries=0)
def task2_llm_process_qwen(
    dag_id: str,
    question: str,
    base_url: str,
    model: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, object]:
    from workflows._inference import chat

    start_time = time.time()
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_question_prompt(question, "Extracted text from file", question)
    qwen_answer = chat(
        [{"role": "user", "content": prompt}],
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    text1_feature = text_feature_for_answer("text1", qwen_answer)
    next_prompt = gaia_fusion_prompt(question, qwen_answer, "")
    next_features = text_features(next_prompt)
    return {
        "qwen_answer": qwen_answer,
        "text1_feature": text1_feature,
        "dag_id": dag_id,
        "curr_task_feat": text_features(prompt, reason=0),
        "succ_task_feat": {
            "task4_llm_fuse_answer": {
                "prompt_length": len(next_prompt),
                "prompt_token_count": next_features["token_count"],
                "text1_length": text1_feature["text1_length"],
                "text1_token_count": text1_feature["text1_token_count"],
                "reason": 0,
            }
        },
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(resources={"cpu": 1}, max_retries=0)
def task3_llm_process_deepseek(
    dag_id: str,
    question: str,
    base_url: str,
    model: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, object]:
    from workflows._inference import chat

    start_time = time.time()
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_deepseek_prompt(question, "Extracted text from file", question)
    deepseek_answer = chat(
        [{"role": "user", "content": prompt}],
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    text2_feature = text_feature_for_answer("text2", deepseek_answer)
    next_prompt = gaia_fusion_prompt(question, "", deepseek_answer)
    next_features = text_features(next_prompt)
    return {
        "deepseek_answer": deepseek_answer,
        "text2_feature": text2_feature,
        "dag_id": dag_id,
        "curr_task_feat": text_features(prompt, reason=1),
        "succ_task_feat": {
            "task4_llm_fuse_answer": {
                "prompt_length": len(next_prompt),
                "prompt_token_count": next_features["token_count"],
                "text2_length": text2_feature["text2_length"],
                "text2_token_count": text2_feature["text2_token_count"],
                "reason": 0,
            }
        },
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(resources={"cpu": 1}, max_retries=0)
def task4_llm_fuse_answer(
    qwen_answer: str,
    deepseek_answer: str,
    dag_id: str,
    question: str,
    text1_feature: dict[str, object],
    text2_feature: dict[str, object],
    base_url: str,
    model: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, object]:
    from workflows._inference import chat

    start_time = time.time()
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_fusion_prompt(question, qwen_answer, deepseek_answer)
    final_answer = chat(
        [{"role": "user", "content": prompt}],
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
    )
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
        "time_record": empty_time_record(),
    }


@workflow
def gaia_reason(
    dag_id: str,
    question: str,
    qwen_base_url: str,
    qwen_model: str,
    qwen_api_key: str,
    deepseek_base_url: str,
    deepseek_model: str,
    deepseek_api_key: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
):
    prepared = task1_obtain_content(dag_id=dag_id, question=question)
    qwen = task2_llm_process_qwen(
        dag_id=prepared.dag_id,
        question=question,
        base_url=qwen_base_url,
        model=qwen_model,
        api_key=qwen_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    deepseek = task3_llm_process_deepseek(
        dag_id=prepared.dag_id,
        question=question,
        base_url=deepseek_base_url,
        model=deepseek_model,
        api_key=deepseek_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    fused = task4_llm_fuse_answer(
        qwen_answer=qwen.qwen_answer,
        deepseek_answer=deepseek.deepseek_answer,
        dag_id=qwen.dag_id,
        question=question,
        text1_feature=qwen.text1_feature,
        text2_feature=deepseek.text2_feature,
        base_url=qwen_base_url,
        model=qwen_model,
        api_key=qwen_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return {"final_answer": fused.final_answer}


def submit(
    *,
    server_url: str,
    dag_id: str,
    question: str,
    qwen_base_url: str,
    qwen_model: str,
    qwen_api_key: str,
    deepseek_base_url: str,
    deepseek_model: str,
    deepseek_api_key: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout_seconds: float | None = None,
) -> tuple[object, str]:
    """Submit this workflow to a running Maze server using real model endpoints.

    Pass API keys as ``env:VARIABLE_NAME`` to keep secrets out of run snapshots.
    """

    client = MaClient(server_url)
    maze_workflow = client.create_workflow_from(
        gaia_reason,
        inputs={
            "dag_id": dag_id,
            "question": question,
            "qwen_base_url": qwen_base_url,
            "qwen_model": qwen_model,
            "qwen_api_key": qwen_api_key,
            "deepseek_base_url": deepseek_base_url,
            "deepseek_model": deepseek_model,
            "deepseek_api_key": deepseek_api_key,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
    )
    return maze_workflow, maze_workflow.run(timeout_seconds=timeout_seconds)


__all__ = [
    "gaia_reason",
    "submit",
    "task1_obtain_content",
    "task2_llm_process_qwen",
    "task3_llm_process_deepseek",
    "task4_llm_fuse_answer",
]
