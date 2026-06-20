from maze import task


@task(resources={"cpu": 1, "cpu_mem": 192, "gpu": 0, "gpu_mem": 0})
def resource_mix_quality_gate(scorecard: dict = None, findings: list = None, graph_stats: dict = None, embedding_summary: dict = None):
    """Apply a deterministic quality gate before the report task runs."""
    scorecard = scorecard or {}
    findings = findings or []
    graph_stats = graph_stats or {}
    embedding_summary = embedding_summary or {}

    checks = [
        {"name": "has_tokens", "passed": scorecard.get("cpu_signal", 0) > 0},
        {"name": "has_artifacts", "passed": scorecard.get("io_signal", 0) > 0},
        {"name": "has_embedding_checksum", "passed": bool(embedding_summary.get("checksum"))},
        {"name": "graph_built", "passed": graph_stats.get("node_count", 0) > 0},
    ]
    passed = sum(1 for check in checks if check["passed"])
    decision = "pass" if passed == len(checks) else "review"
    notes = list(findings)
    if decision != "pass":
        notes.append("One or more demo checks require review.")

    return {
        "quality_gate": {
            "decision": decision,
            "passed": passed,
            "total": len(checks),
            "checks": checks,
        },
        "quality_notes": notes,
    }
