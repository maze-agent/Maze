"""Read one artifact-backed GAIA audio file for the Playground."""

from __future__ import annotations

from maze import task
import workflows.gaia.speech as gaia_speech


@task(
    task_kind="cpu",
    resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
    max_retries=1,
    retry_on=["node_lost", "resource_unavailable", "artifact_error"],
)
def gaia_prepare_audio(
    dag_id: str,
    question: str,
    supplementary_path: str,
) -> dict[str, object]:
    result = gaia_speech.task1_audio_process(dag_id, question, supplementary_path)
    return {
        "dag_id": result["dag_id"],
        "question": question,
        "audio_base64": result["audio_base64"],
        "file_name": result["file_name"],
        "audio_feature": result["audio_feature"],
    }
