"""Read one artifact-backed GAIA document for the Playground."""

from __future__ import annotations

from maze import task
import workflows.gaia.file as gaia_file


@task(
    task_kind="cpu",
    resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
    max_retries=1,
    retry_on=["node_lost", "resource_unavailable", "artifact_error"],
)
def gaia_prepare_document(
    dag_id: str,
    question: str,
    supplementary_path: str,
) -> dict[str, object]:
    result = gaia_file.task1_file_process(dag_id, question, supplementary_path)
    return {
        "dag_id": result["dag_id"],
        "question": question,
        "context_label": "Extracted text from file",
        "context": result["processed_content"],
        "file_name": result["file_name"],
        "content_sha256": result["content_sha256"],
    }
