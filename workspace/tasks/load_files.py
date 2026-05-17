from maze import task
from pathlib import Path

@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def load_files(input_dir: str = "."):
    root = Path(input_dir)
    files = [
        entry.relative_to(root).as_posix()
        for entry in root.rglob("*")
        if entry.is_file()
    ]
    return {"file_names": sorted(files)}
