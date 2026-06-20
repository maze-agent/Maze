from maze import task


@task(resources={"cpu": 1, "cpu_mem": 256, "gpu": 0, "gpu_mem": 0})
def resource_mix_merge_signals(
    token_stats: dict = None,
    keyword_scores: dict = None,
    graph_stats: dict = None,
    ranked_sections: list = None,
    embedding_summary: dict = None,
    section_file_count: int = 0,
):
    """Merge CPU, GPU, and I/O signals into a compact scorecard."""
    token_stats = token_stats or {}
    keyword_scores = keyword_scores or {}
    graph_stats = graph_stats or {}
    embedding_summary = embedding_summary or {}
    ranked_sections = ranked_sections or []

    cpu_signal = token_stats.get("unique_tokens", 0) + graph_stats.get("edge_count", 0) * 3
    gpu_signal = 10 if embedding_summary.get("accelerator") == "cuda" else 4
    io_signal = int(section_file_count or 0) * 2
    total_score = cpu_signal + gpu_signal + io_signal

    routing_plan = [
        {"lane": "I/O", "reason": "load text, materialize section files, and write final artifacts", "weight": io_signal},
        {"lane": "CPU", "reason": "token statistics, section graph, and deterministic quality gates", "weight": cpu_signal},
        {"lane": "GPU", "reason": "vector-style section embedding probe with CPU fallback", "weight": gpu_signal},
    ]
    routing_plan.sort(key=lambda item: item["weight"], reverse=True)

    top_section = ranked_sections[0] if ranked_sections else {"section_id": None, "title": "n/a", "score": 0}
    findings = [
        f"Top section: {top_section.get('title')} ({top_section.get('section_id')})",
        f"Top lane: {routing_plan[0]['lane']}",
        f"Keyword balance: CPU={keyword_scores.get('cpu', 0)}, GPU={keyword_scores.get('gpu', 0)}, IO={keyword_scores.get('io', 0)}",
    ]

    return {
        "scorecard": {
            "total_score": total_score,
            "cpu_signal": cpu_signal,
            "gpu_signal": gpu_signal,
            "io_signal": io_signal,
            "accelerator": embedding_summary.get("accelerator", "unknown"),
        },
        "routing_plan": routing_plan,
        "findings": findings,
    }
