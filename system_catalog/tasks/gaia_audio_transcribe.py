"""Transcribe artifact-backed GAIA audio through a routed model service."""

from __future__ import annotations

from base64 import b64decode
import os

from maze import task


GPU_RESOURCES = {"cpu_num": 1, "gpu_mem": 8192, "io_num": 0}


@task(task_kind="gpu", resources=GPU_RESOURCES, max_retries=0)
def gaia_transcribe_audio(
    dag_id: str,
    question: str,
    audio_base64: str,
    file_name: str,
) -> dict[str, object]:
    from workflows._inference import transcribe

    base_url = os.environ.get("MAZE_MODEL_ENDPOINT", "").strip()
    model = os.environ.get("MAZE_MODEL_NAME", "").strip()
    if not base_url or not model:
        raise RuntimeError("GAIA transcription task requires a Scheduler model route")
    transcript = transcribe(
        b64decode(audio_base64, validate=True),
        file_name=file_name,
        base_url=base_url,
        model=model,
        api_key="",
    )
    return {
        "dag_id": dag_id,
        "question": question,
        "context_label": "Extracted text from speech",
        "context": transcript,
    }
