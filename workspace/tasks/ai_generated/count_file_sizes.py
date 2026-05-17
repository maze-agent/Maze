from maze import task
from pathlib import Path
from typing import List
import os

@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def count_file_sizes(file_names: List[str] = "file_names"):
    file_sizes = []
    total_size = 0
    for file_name in file_names:
        file_path = Path(file_name)
        if file_path.exists():
            size = os.path.getsize(file_path)
            file_sizes.append({"file_name": file_name, "size_bytes": size})
            total_size += size
    report_path = "reports/file_sizes.json"
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(str(file_sizes), encoding="utf-8")
    return {"file_sizes": file_sizes, "total_size_bytes": total_size, "report_path": report_path}