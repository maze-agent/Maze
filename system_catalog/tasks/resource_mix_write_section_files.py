import json
from pathlib import Path

from maze import task


@task(task_kind="io", resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 1})
def resource_mix_write_section_files(sections: list = None, output_dir: str = "resource_mix_demo/sections"):
    """Write section text files and a manifest artifact."""
    sections = sections or []
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)

    files = []
    total_bytes = 0
    for index, section in enumerate(sections, start=1):
        path = root / f"{index:02d}_{section.get('id', f's{index:02d}')}.txt"
        text = section.get("text", "")
        path.write_text(text, encoding="utf-8")
        size = len(text.encode("utf-8"))
        total_bytes += size
        files.append({
            "path": path.as_posix(),
            "title": section.get("title", path.stem),
            "bytes": size,
        })

    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps({
        "section_count": len(files),
        "total_bytes": total_bytes,
        "files": files,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "section_paths": [item["path"] for item in files],
        "section_manifest_path": manifest_path.as_posix(),
        "section_file_count": len(files),
        "section_total_bytes": total_bytes,
    }
