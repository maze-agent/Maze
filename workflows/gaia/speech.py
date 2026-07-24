"""Ascend-Maze port of the Maze GAIA speech workflow."""

from __future__ import annotations

import time

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.gaia._common import (
    empty_time_record,
    gaia_deepseek_prompt,
    gaia_fusion_prompt,
    gaia_question_prompt,
    model_runtime_inputs,
    speech_transcription_prompt,
    summarize_audio_file,
    text_feature_for_answer,
    text_features,
)

SPEC = WorkflowSpec(
    name="maze-gaia-speech",
    source="gaia",
    kind="speech",
    nodes=nodes(
        (
            ("task1_speech_process", "cpu", None),
            ("task2_speech_process", "npu", "whisper-large-v3"),
            ("task3_llm_process_qwen", "npu", "qwen3-32b"),
            ("task4_llm_process_deepseek", "npu", "deepseek-r1-32b"),
            ("task5_llm_fuse_answer", "npu", "qwen3-32b"),
        )
    ),
    edges=edges(
        (
            ("task1_speech_process", "task2_speech_process"),
            ("task2_speech_process", "task3_llm_process_qwen"),
            ("task2_speech_process", "task4_llm_process_deepseek"),
            ("task3_llm_process_qwen", "task5_llm_fuse_answer"),
            ("task4_llm_process_deepseek", "task5_llm_fuse_answer"),
        )
    ),
)

INPUTS = spec_inputs()


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task1_speech_process(
    supplementary_files: object,
) -> dict[str, object]:
    start_time = time.time()
    audio_summary = summarize_audio_file(supplementary_files)
    audio_features = dict(audio_summary["audio_features"])
    return {
        "file_content": audio_summary["audio_bytes"],
        "audio_features": audio_features,
        "succ_task_feat": audio_features,
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task2_speech_process(
    dag_id: str,
    question: str,
    file_content: bytes,
    model_folder: str,
    audio_features: dict[str, object],
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del file_content, model_folder
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    response = chat(
        [
            {
                "role": "user",
                "content": speech_transcription_prompt(question, audio_features),
            }
        ],
        max_tokens=4096,
        temperature=0.0,
    )
    processed_content = response.text
    prompt = gaia_question_prompt(
        question,
        "Extracted text from speech",
        processed_content,
    )
    features = text_features(prompt)
    return {
        "processed_content": processed_content,
        "succ_task_feat": {
            "task3_llm_process_qwen": {
                "text_length": features["text_length"],
                "token_count": features["token_count"],
                "reason": 1,
            },
            "task4_llm_process_deepseek": {
                "text_length": features["text_length"],
                "token_count": features["token_count"],
                "reason": 0,
            },
        },
        "dag_id": dag_id,
        "curr_task_feat": audio_features,
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task3_llm_process_qwen(
    dag_id: str,
    processed_content: str,
    question: str,
    use_online_model: bool,
    model_folder: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    repetition_penalty: float,
    task3_llm_process_qwen_request_api_url: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del (
        use_online_model,
        model_folder,
        top_p,
        repetition_penalty,
        task3_llm_process_qwen_request_api_url,
    )
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_question_prompt(
        question,
        "Extracted text from file",
        processed_content,
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
        "time_record": empty_time_record(),
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task4_llm_process_deepseek(
    dag_id: str,
    processed_content: str,
    question: str,
    use_online_model: bool,
    model_folder: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    repetition_penalty: float,
    task4_llm_process_deepseek_request_api_url: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del (
        use_online_model,
        model_folder,
        top_p,
        repetition_penalty,
        task4_llm_process_deepseek_request_api_url,
    )
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_deepseek_prompt(
        question,
        "Extracted text from file",
        processed_content,
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
            "task5_llm_fuse_answer": {
                "prompt_length": len(next_prompt),
                "prompt_token_count": text_features(next_prompt)["token_count"],
                "text2_length": text2_feature["text2_length"],
                "text2_token_count": text2_feature["text2_token_count"],
                "reason": 0,
            }
        },
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5_llm_fuse_answer(
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
    task5_llm_fuse_answer_request_api_url: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del (
        use_online_model,
        model_folder,
        top_p,
        repetition_penalty,
        task5_llm_fuse_answer_request_api_url,
    )
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    prompt = gaia_fusion_prompt(question, qwen_answer, deepseek_answer)
    response = chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    prompt_features = text_features(prompt)
    return {
        "dag_id": dag_id,
        "final_answer": response.text,
        "curr_task_feat": {
            "prompt_length": prompt_features["text_length"],
            "prompt_token_count": prompt_features["token_count"],
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


def build() -> Workflow:
    workflow = Workflow(SPEC.name)
    dag_id = workflow.input("dag_id")
    question = workflow.input("question")
    workflow.input("answer")
    supplementary_files = workflow.input("supplementary_files")
    workflow.input("metadata")

    prepared = workflow.add_task(
        task1_speech_process,
        task_name="task1_speech_process",
        inputs={"supplementary_files": supplementary_files},
    )
    transcribed = workflow.add_task(
        task2_speech_process,
        task_name="task2_speech_process",
        model_anchor={"model": "whisper-large-v3", "mode": "service"},
        inputs={
            "dag_id": dag_id,
            "question": question,
            "file_content": prepared.outputs["file_content"],
            "model_folder": "",
            "audio_features": prepared.outputs["audio_features"],
        },
    )
    qwen = workflow.add_task(
        task3_llm_process_qwen,
        task_name="task3_llm_process_qwen",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": dag_id,
            "processed_content": transcribed.outputs["processed_content"],
            "question": question,
            **model_runtime_inputs("task3_llm_process_qwen_request_api_url"),
        },
    )
    deepseek = workflow.add_task(
        task4_llm_process_deepseek,
        task_name="task4_llm_process_deepseek",
        model_anchor={"model": "deepseek-r1-32b", "mode": "service"},
        inputs={
            "dag_id": dag_id,
            "processed_content": transcribed.outputs["processed_content"],
            "question": question,
            **model_runtime_inputs("task4_llm_process_deepseek_request_api_url"),
        },
    )
    workflow.add_task(
        task5_llm_fuse_answer,
        task_name="task5_llm_fuse_answer",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "qwen_answer": qwen.outputs["qwen_answer"],
            "deepseek_answer": deepseek.outputs["deepseek_answer"],
            "dag_id": dag_id,
            "question": question,
            "text1_feature": qwen.outputs["text1_feature"],
            "text2_feature": deepseek.outputs["text2_feature"],
            **model_runtime_inputs("task5_llm_fuse_answer_request_api_url"),
        },
    )
    return workflow
