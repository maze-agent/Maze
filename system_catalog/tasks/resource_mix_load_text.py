from pathlib import Path

from maze import task


SAMPLE_TEXT = """
Maze resource mix demo

Question: How should a distributed workflow balance CPU parsing, GPU ranking, and file I/O?
Observation: CPU tasks are good at deterministic text statistics and graph scoring.
Observation: GPU tasks are useful for vector-style scoring even when this demo falls back to CPU math.
Observation: I/O tasks should turn intermediate state into artifacts that later tasks can inspect.

Question: What should the final report show?
Answer: It should show the source text, token signals, section graph, accelerator mode, and artifact paths.
Answer: It should be safe to run even when the uploaded .txt file is missing.
"""


def _pick_text_file(input_path: str) -> Path | None:
    configured = Path(input_path or "questions.txt")
    if configured.is_file():
        return configured

    txt_files = [
        path
        for path in sorted(Path(".").rglob("*.txt"))
        if path.is_file() and "resource_mix_demo" not in path.parts
    ]
    return txt_files[0] if txt_files else None


@task(task_kind="io", resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 1})
def resource_mix_load_text(input_path: str = "questions.txt", fallback_text: str = SAMPLE_TEXT):
    """Read an uploaded text file, or create a deterministic sample when none exists."""
    source = _pick_text_file(input_path)
    created_sample = False

    if source is None:
        source = Path(input_path or "questions.txt")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(fallback_text.strip() + "\n", encoding="utf-8")
        created_sample = True

    text = source.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    return {
        "corpus": text,
        "source_path": source.as_posix(),
        "line_count": len(lines),
        "byte_count": len(text.encode("utf-8")),
        "created_sample": created_sample,
    }
