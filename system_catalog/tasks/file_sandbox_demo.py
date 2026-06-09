from pathlib import Path
from maze import task


@task(resources={"cpu": 1, "cpu_mem": 128, "gpu": 0, "gpu_mem": 0})
def file_sandbox_report(
    input_path: str = "demo/input.txt",
    report_path: str = "reports/file_sandbox_report.md",
):
    """Read a staged workspace file and write a small report artifact."""
    source = Path(input_path)
    if source.exists():
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        source.parent.mkdir(parents=True, exist_ok=True)
        text = (
            "Maze file sandbox demo\n"
            "This sample file was created because no input file existed yet.\n"
            "Upload files into Workspace Files and point input_path at them."
        )
        source.write_text(text, encoding="utf-8")

    lines = text.splitlines()
    words = text.split()
    report = "\n".join([
        "# File Sandbox Report",
        "",
        f"- Input path: `{input_path}`",
        f"- Characters: {len(text)}",
        f"- Lines: {len(lines)}",
        f"- Words: {len(words)}",
        "",
        "## Preview",
        "",
        text[:800],
        "",
    ])

    output = Path(report_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")

    return {
        "input_path": input_path,
        "report_path": report_path,
        "characters": len(text),
        "lines": len(lines),
        "words": len(words),
    }
