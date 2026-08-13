"""GAIA speech workflow implemented with Maze's public API."""

from __future__ import annotations

from base64 import b64decode, b64encode
import time

from maze import MaClient, task, workflow

from workflows.gaia._common import empty_time_record, read_input_file, text_features
from workflows.gaia.file import (
    task2_llm_process_qwen,
    task3_llm_process_deepseek,
    task4_llm_fuse_answer,
)


AUDIO_EXTENSIONS = frozenset({".flac", ".m4a", ".mp3", ".ogg", ".wav", ".webm"})


@task(
    resources={"cpu": 1},
    max_retries=1,
    retry_on=["node_lost", "resource_unavailable", "artifact_error"],
)
def task1_audio_process(
    dag_id: str,
    question: str,
    supplementary_path: str,
) -> dict[str, object]:
    start_time = time.time()
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    file_info = read_input_file(
        supplementary_path,
        allowed_extensions=AUDIO_EXTENSIONS,
        kind="audio",
    )
    audio_feature = {
        "file_name": file_info["file_name"],
        "file_extension": file_info["file_extension"],
        "mime_type": file_info["mime_type"],
        "content_sha256": file_info["content_sha256"],
        "size_bytes": file_info["size_bytes"],
    }
    return {
        "dag_id": dag_id,
        "audio_base64": b64encode(bytes(file_info["content"])).decode("ascii"),
        "file_name": file_info["file_name"],
        "audio_feature": audio_feature,
        "curr_task_feat": None,
        "succ_task_feat": {"task2_transcribe_audio": audio_feature},
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(resources={"cpu": 1}, max_retries=0)
def task2_transcribe_audio(
    dag_id: str,
    question: str,
    audio_base64: str,
    file_name: str,
    audio_feature: dict[str, object],
    base_url: str,
    model: str,
    api_key: str,
) -> dict[str, object]:
    from workflows._inference import transcribe
    from workflows.gaia._common import gaia_question_prompt

    start_time = time.time()
    transcript = transcribe(
        b64decode(audio_base64, validate=True),
        file_name=file_name,
        base_url=base_url,
        model=model,
        api_key=api_key,
    )
    prompt_features = text_features(
        gaia_question_prompt(question, "Extracted text from speech", transcript)
    )
    return {
        "dag_id": dag_id,
        "processed_content": transcript,
        "curr_task_feat": audio_feature,
        "succ_task_feat": {
            "task2_llm_process_qwen": {**prompt_features, "reason": 1},
            "task3_llm_process_deepseek": {**prompt_features, "reason": 0},
        },
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@workflow
def gaia_speech(
    dag_id: str,
    question: str,
    supplementary_path: str,
    transcription_base_url: str,
    transcription_model: str,
    transcription_api_key: str,
    qwen_base_url: str,
    qwen_model: str,
    qwen_api_key: str,
    deepseek_base_url: str,
    deepseek_model: str,
    deepseek_api_key: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
):
    prepared = task1_audio_process(
        dag_id=dag_id,
        question=question,
        supplementary_path=supplementary_path,
    )
    transcribed = task2_transcribe_audio(
        dag_id=prepared.dag_id,
        question=question,
        audio_base64=prepared.audio_base64,
        file_name=prepared.file_name,
        audio_feature=prepared.audio_feature,
        base_url=transcription_base_url,
        model=transcription_model,
        api_key=transcription_api_key,
    )
    qwen = task2_llm_process_qwen(
        dag_id=transcribed.dag_id,
        processed_content=transcribed.processed_content,
        question=question,
        base_url=qwen_base_url,
        model=qwen_model,
        api_key=qwen_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    deepseek = task3_llm_process_deepseek(
        dag_id=transcribed.dag_id,
        processed_content=transcribed.processed_content,
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


def create_template(
    *,
    server_url: str,
    transcription_base_url: str,
    transcription_model: str,
    transcription_api_key: str,
    qwen_base_url: str,
    qwen_model: str,
    qwen_api_key: str,
    deepseek_base_url: str,
    deepseek_model: str,
    deepseek_api_key: str,
):
    client = MaClient(server_url)
    return client.create_workflow_from(
        gaia_speech,
        inputs={
            "transcription_base_url": transcription_base_url,
            "transcription_model": transcription_model,
            "transcription_api_key": transcription_api_key,
            "qwen_base_url": qwen_base_url,
            "qwen_model": qwen_model,
            "qwen_api_key": qwen_api_key,
            "deepseek_base_url": deepseek_base_url,
            "deepseek_model": deepseek_model,
            "deepseek_api_key": deepseek_api_key,
        },
    )


__all__ = [
    "create_template",
    "gaia_speech",
    "task1_audio_process",
    "task2_transcribe_audio",
]
