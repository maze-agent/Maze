import json
from pathlib import Path

from maze import task


@task(task_kind="cpu", resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0})
def resource_mix_write_final_report(
    source_path: str = "",
    token_stats: dict = None,
    graph_stats: dict = None,
    embedding_summary: dict = None,
    scorecard: dict = None,
    signal_summary: list = None,
    quality_gate: dict = None,
    quality_notes: list = None,
    section_manifest_path: str = "",
    output_path: str = "resource_mix_demo/final_report.md",
    summary_path: str = "resource_mix_demo/summary.json",
):
    """Write the final Markdown and JSON artifacts for the resource mix demo."""
    token_stats = token_stats or {}
    graph_stats = graph_stats or {}
    embedding_summary = embedding_summary or {}
    scorecard = scorecard or {}
    signal_summary = signal_summary or []
    quality_gate = quality_gate or {}
    quality_notes = quality_notes or []

    manifest = {}
    manifest_file = Path(section_manifest_path) if section_manifest_path else None
    if manifest_file and manifest_file.exists():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = Path(summary_path)
    summary.parent.mkdir(parents=True, exist_ok=True)

    report_lines = [
        "# Maze Resource Mix Demo Report",
        "",
        f"- Source text: `{source_path}`",
        f"- Quality gate: `{quality_gate.get('decision', 'unknown')}` ({quality_gate.get('passed', 0)}/{quality_gate.get('total', 0)})",
        f"- Accelerator: `{embedding_summary.get('accelerator', 'unknown')}` on `{embedding_summary.get('device_name', 'unknown')}`",
        f"- Total score: `{scorecard.get('total_score', 0)}`",
        "",
        "## Demo Content Signals",
        "",
    ]
    for item in signal_summary:
        report_lines.append(f"- **{item.get('signal')}**: {item.get('description')} (value={item.get('value')})")

    report_lines.extend([
        "",
        "## Signals",
        "",
        f"- Tokens: {token_stats.get('total_tokens', 0)} total, {token_stats.get('unique_tokens', 0)} unique",
        f"- Graph: {graph_stats.get('node_count', 0)} nodes, {graph_stats.get('edge_count', 0)} edges",
        f"- Section files: {manifest.get('section_count', 0)}",
        "",
        "## Notes",
        "",
    ])
    report_lines.extend(f"- {note}" for note in quality_notes)

    payload = {
        "source_path": source_path,
        "token_stats": token_stats,
        "graph_stats": graph_stats,
        "embedding_summary": embedding_summary,
        "scorecard": scorecard,
        "signal_summary": signal_summary,
        "quality_gate": quality_gate,
        "section_manifest": manifest,
        "artifacts": [output.as_posix(), summary.as_posix(), section_manifest_path],
    }

    report = "\n".join(report_lines) + "\n"
    output.write_text(report, encoding="utf-8")
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "report_path": output.as_posix(),
        "summary_json_path": summary.as_posix(),
        "report_preview": report[:900],
        "artifact_paths": [output.as_posix(), summary.as_posix(), section_manifest_path],
    }
