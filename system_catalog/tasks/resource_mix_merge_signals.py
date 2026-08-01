from maze import task


@task(task_kind="cpu", resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0})
def resource_mix_merge_signals(
    token_stats: dict = None,
    keyword_scores: dict = None,
    graph_stats: dict = None,
    ranked_sections: list = None,
    embedding_summary: dict = None,
    section_file_count: int = 0,
):
    """Merge CPU/GPU analysis and artifact counts into a compact demo scorecard."""
    token_stats = token_stats or {}
    keyword_scores = keyword_scores or {}
    graph_stats = graph_stats or {}
    embedding_summary = embedding_summary or {}
    ranked_sections = ranked_sections or []

    cpu_signal = token_stats.get("unique_tokens", 0) + graph_stats.get("edge_count", 0) * 3
    gpu_signal = 10 if embedding_summary.get("accelerator") == "cuda" else 4
    artifact_signal = int(section_file_count or 0) * 2
    total_score = cpu_signal + gpu_signal + artifact_signal

    signal_summary = [
        {"signal": "artifact_files", "description": "materialized section files", "value": artifact_signal},
        {"signal": "cpu_analysis", "description": "token statistics and section graph", "value": cpu_signal},
        {"signal": "gpu_probe", "description": "vector-style section embedding probe", "value": gpu_signal},
    ]
    signal_summary.sort(key=lambda item: item["value"], reverse=True)

    top_section = ranked_sections[0] if ranked_sections else {"section_id": None, "title": "n/a", "score": 0}
    findings = [
        f"Top section: {top_section.get('title')} ({top_section.get('section_id')})",
        f"Dominant demo signal: {signal_summary[0]['signal']}",
        f"Keyword mentions: CPU={keyword_scores.get('cpu', 0)}, GPU={keyword_scores.get('gpu', 0)}, artifact={keyword_scores.get('artifact', 0)}",
    ]

    return {
        "scorecard": {
            "total_score": total_score,
            "cpu_signal": cpu_signal,
            "gpu_signal": gpu_signal,
            "artifact_signal": artifact_signal,
            "accelerator": embedding_summary.get("accelerator", "unknown"),
        },
        "signal_summary": signal_summary,
        "findings": findings,
    }
