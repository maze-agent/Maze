"""GAIA document workflow implemented with Maze's public API."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import time

from maze import MaClient, task, workflow

from workflows.gaia._common import (
    empty_time_record,
    gaia_deepseek_prompt,
    gaia_fusion_prompt,
    gaia_question_prompt,
    text_feature_for_answer,
    text_features,
)


def _resolve_input_file(supplementary_path: str) -> Path:
    input_dir_value = os.environ.get("MAZE_INPUT_DIR")
    if not input_dir_value:
        raise RuntimeError("MAZE_INPUT_DIR is not set")
    if not isinstance(supplementary_path, str) or not supplementary_path.strip():
        raise ValueError("supplementary_path must be a non-empty relative path")

    normalized = supplementary_path.replace("\\", "/")
    relative = PurePosixPath(normalized)
    windows_path = PureWindowsPath(supplementary_path)
    if (
        relative.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in relative.parts
    ):
        raise ValueError("supplementary_path must stay within MAZE_INPUT_DIR")

    input_dir = Path(input_dir_value).resolve()
    candidate = (input_dir / Path(*relative.parts)).resolve(strict=True)
    try:
        candidate.relative_to(input_dir)
    except ValueError as exc:
        raise ValueError(
            "supplementary_path must stay within MAZE_INPUT_DIR"
        ) from exc
    if not candidate.is_file():
        raise ValueError("supplementary_path must reference a file")
    return candidate


def _decode_text(content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1")


def _process_document_file(supplementary_path: str) -> dict[str, object]:
    path = _resolve_input_file(supplementary_path)
    content = path.read_bytes()
    extension = path.suffix.lower()

    if extension in {".txt", ".md"}:
        processed_content = _decode_text(content)
    elif extension == ".pdf":
        from PyPDF2 import PdfReader

        pages = []
        for index, page in enumerate(PdfReader(BytesIO(content)).pages, start=1):
            page_text = page.extract_text() or ""
            if page_text.strip():
                pages.append(f"--- Page {index} ---\n{page_text}")
        processed_content = "\n\n".join(pages)
        if not processed_content:
            raise ValueError(f"PDF contains no extractable text: {path.name}")
    else:
        raise ValueError(
            f"Unsupported supplementary file type {extension or '<none>'}; "
            "supported types are .txt, .md, and .pdf"
        )

    return {
        "file_name": path.name,
        "file_extension": extension,
        "content_sha256": sha256(content).hexdigest(),
        "size_bytes": len(content),
        "processed_content": processed_content,
    }


@task(
    resources={"cpu": 1},
    max_retries=1,
    retry_on=["node_lost", "resource_unavailable", "artifact_error"],
)
def task1_file_process(
    dag_id: str,
    question: str,
    supplementary_path: str,
) -> dict[str, object]:
    start_time = time.time()
    if not question:
        raise ValueError(f"task {dag_id} missing Question")
    file_info = _process_document_file(supplementary_path)
    processed_content = str(file_info["processed_content"])
    prompt = gaia_question_prompt(
        question,
        "Extracted text from file",
        processed_content,
    )
    features = text_features(prompt)
    return {
        "processed_content": processed_content,
        "file_name": file_info["file_name"],
        "content_sha256": file_info["content_sha256"],
        "dag_id": dag_id,
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
        "curr_task_feat": None,
        "start_time": start_time,
        "end_time": time.time(),
        "time_record": empty_time_record(),
    }


@task(resources={"cpu": 1}, max_retries=0)
def task2_llm_process_qwen(
    dag_id: str,
    processed_content: str,
    question: str,
    base_url: str,
    model: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, object]:
    from workflows._inference import chat

    start_time = time.time()
    prompt = gaia_question_prompt(
        question,
        "Extracted text from file",
        processed_content,
    )
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
    processed_content: str,
    question: str,
    base_url: str,
    model: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, object]:
    from workflows._inference import chat

    start_time = time.time()
    prompt = gaia_deepseek_prompt(
        question,
        "Extracted text from file",
        processed_content,
    )
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
def gaia_file(
    dag_id: str,
    question: str,
    supplementary_path: str,
    qwen_base_url: str,
    qwen_model: str,
    qwen_api_key: str,
    deepseek_base_url: str,
    deepseek_model: str,
    deepseek_api_key: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
):
    prepared = task1_file_process(
        dag_id=dag_id,
        question=question,
        supplementary_path=supplementary_path,
    )
    qwen = task2_llm_process_qwen(
        dag_id=prepared.dag_id,
        processed_content=prepared.processed_content,
        question=question,
        base_url=qwen_base_url,
        model=qwen_model,
        api_key=qwen_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    deepseek = task3_llm_process_deepseek(
        dag_id=prepared.dag_id,
        processed_content=prepared.processed_content,
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
    qwen_base_url: str,
    qwen_model: str,
    qwen_api_key: str,
    deepseek_base_url: str,
    deepseek_model: str,
    deepseek_api_key: str,
):
    client = MaClient(server_url)
    return client.create_workflow_from(
        gaia_file,
        inputs={
            "qwen_base_url": qwen_base_url,
            "qwen_model": qwen_model,
            "qwen_api_key": qwen_api_key,
            "deepseek_base_url": deepseek_base_url,
            "deepseek_model": deepseek_model,
            "deepseek_api_key": deepseek_api_key,
        },
    )


def submit(
    *,
    server_url: str,
    workspace_dir: str,
    dag_id: str,
    question: str,
    supplementary_path: str,
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
    """Submit one artifact-backed GAIA file Run to a Maze server."""

    maze_workflow = create_template(
        server_url=server_url,
        qwen_base_url=qwen_base_url,
        qwen_model=qwen_model,
        qwen_api_key=qwen_api_key,
        deepseek_base_url=deepseek_base_url,
        deepseek_model=deepseek_model,
        deepseek_api_key=deepseek_api_key,
    )
    run_id = maze_workflow.run(
        workspace_dir=workspace_dir,
        artifact_mode=True,
        timeout_seconds=timeout_seconds,
        inputs={
            "dag_id": dag_id,
            "question": question,
            "supplementary_path": supplementary_path,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
    )
    return maze_workflow, run_id


__all__ = [
    "create_template",
    "gaia_file",
    "submit",
    "task1_file_process",
    "task2_llm_process_qwen",
    "task3_llm_process_deepseek",
    "task4_llm_fuse_answer",
]
