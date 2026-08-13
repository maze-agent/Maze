"""Read one artifact-backed GAIA image for the Playground."""

from __future__ import annotations

from maze import task
import workflows.gaia.vision as gaia_vision


@task(
    task_kind="cpu",
    resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
    max_retries=1,
    retry_on=["node_lost", "resource_unavailable", "artifact_error"],
)
def gaia_prepare_image(
    dag_id: str,
    question: str,
    supplementary_path: str,
) -> dict[str, object]:
    result = gaia_vision.task1_image_process(dag_id, question, supplementary_path)
    return {
        "dag_id": result["dag_id"],
        "question": question,
        "image_base64": result["image_base64"],
        "mime_type": result["mime_type"],
        "image_feature": result["image_feature"],
    }
