from maze.core.runs.analysis import build_dag_design_digest, build_run_digest
from maze.core.workflow.dag_spec import dag_spec_from_payload


def _snapshot():
    return {
        "run_id": "run-1",
        "status": "succeeded",
        "duration_seconds": 12.5,
        "task_counts": {"total": 3, "succeeded": 2, "failed": 1},
        "error_summary": None,
        "metrics": {"tokens_in": 100, "tokens_out": 40},
        "task_nodes": {
            "a": {
                "task_id": "a",
                "task_name": "load",
                "status": "succeeded",
                "duration_seconds": 1.0,
                "resources": {"cpu": 1},
                "metrics": {"tokens_in": 10},
            },
            "b": {
                "task_id": "b",
                "task_name": "infer",
                "status": "succeeded",
                "duration_seconds": 9.0,
                "variant": "fallback",
                "degraded": True,
                "resources": {"cpu": 4},
            },
            "c": {
                "task_id": "c",
                "task_name": "save",
                "status": "failed",
                "duration_seconds": 2.0,
                "error": "boom",
                "pending_reason": "insufficient_gpu",
            },
        },
    }


def test_build_run_digest_extracts_signals():
    digest = build_run_digest(_snapshot())

    assert digest["run_id"] == "run-1"
    assert digest["task_total"] == 3
    # slowest first
    assert digest["slowest_tasks"][0]["task"] == "infer"
    assert digest["degraded_tasks"] == ["infer"]
    assert digest["failed_tasks"] == [{"task": "save", "error": "boom"}]
    assert digest["pending_tasks"] == [{"task": "save", "reason": "insufficient_gpu"}]


def test_build_run_digest_truncates_long_error():
    snap = _snapshot()
    snap["task_nodes"]["c"]["error"] = "x" * 1000
    digest = build_run_digest(snap)
    failed_error = digest["failed_tasks"][0]["error"]
    assert failed_error.endswith("...")
    assert len(failed_error) <= 403


def test_build_dag_design_digest_structure():
    spec = dag_spec_from_payload({
        "name": "demo",
        "nodes": [
            {"id": "a", "code": "def a():\n    return {'x': 1}", "outputs": ["x"], "resources": {"gpu": 1},
             "fallback": {"code": "def a():\n    return {'x': 1}", "resources": {"cpu": 1}}},
            {"id": "b", "code": "def b(x):\n    return {'y': x}", "inputs": {"x": {"from": "a.x"}}, "outputs": ["y"]},
            {"id": "c", "code": "def c():\n    return {'z': 1}", "outputs": ["z"]},
        ],
        "edges": [{"from": "a.x", "to": "b.x"}],
    })
    digest = build_dag_design_digest(spec)

    assert digest["node_count"] == 3
    assert digest["edge_count"] == 1
    assert set(digest["roots"]) == {"a", "c"}
    assert "b" in digest["leaves"]
    assert digest["isolated"] == ["c"]
    assert digest["max_depth"] == 2  # a -> b
    assert digest["gpu_nodes"] == 1
    assert digest["fallback_nodes"] == 1
