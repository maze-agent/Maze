from maze import task
from typing import List

@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def count_files(file_names: List[str]) -> dict:
    return {"file_count": len(file_names)}