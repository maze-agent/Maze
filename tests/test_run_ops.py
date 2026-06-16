import pytest

from maze.core.runs.context_store import ContextStore
from maze.core.runs.ops import (
    ResumeError,
    aggregate_agent_metrics,
    build_resume_spec,
    summarize_run_errors,
)


def _dag_spec():
    return {
        "schema": "maze.workflow/v1",
        "name": "demo",
        "nodes": [
            {"id": "a", "task_name": "a", "code_str": "def a():\n    return {'x': 1}", "outputs": [{"name": "x", "data_type": "any"}], "resources": {"cpu": 1}, "inputs": {}},
            {"id": "b", "task_name": "b", "code_str": "def b(x):\n    return {'y': x}", "outputs": [{"name": "y", "data_type": "any"}], "resources": {"cpu": 1}, "inputs": {}},
        ],
        "edges": [{"source_task_id": "a", "source_output": "x", "target_task_id": "b", "target_input": "x"}],
        "run": {"artifact_mode": True},
    }


# --- C5 resume ---

def test_resume_skips_succeeded_and_injects_output():
    task_nodes = {
        "a": {"task_id": "a", "status": "succeeded", "result_summary": {"x": 42}},
        "b": {"task_id": "b", "status": "failed"},
    }
    spec = build_resume_spec(_dag_spec(), task_nodes)
    assert [n["id"] for n in spec["nodes"]] == ["b"]
    # the edge a->b becomes a literal input from a's persisted output
    assert spec["nodes"][0]["inputs"]["x"] == {"value": 42}
    assert spec["edges"] == []


def test_resume_keeps_edge_when_both_rerun():
    task_nodes = {
        "a": {"task_id": "a", "status": "failed"},
        "b": {"task_id": "b", "status": "pending"},
    }
    spec = build_resume_spec(_dag_spec(), task_nodes)
    assert {n["id"] for n in spec["nodes"]} == {"a", "b"}
    assert spec["edges"] == [{"from": "a.x", "to": "b.x"}]


def test_resume_all_succeeded_raises():
    task_nodes = {
        "a": {"task_id": "a", "status": "succeeded", "result_summary": {"x": 1}},
        "b": {"task_id": "b", "status": "succeeded", "result_summary": {"y": 1}},
    }
    with pytest.raises(ResumeError, match="all tasks already succeeded"):
        build_resume_spec(_dag_spec(), task_nodes)


def test_resume_missing_output_raises():
    task_nodes = {
        "a": {"task_id": "a", "status": "succeeded", "result_summary": {}},  # no 'x'
        "b": {"task_id": "b", "status": "failed"},
    }
    with pytest.raises(ResumeError, match="missing persisted output"):
        build_resume_spec(_dag_spec(), task_nodes)


# --- C7 agent metrics ---

def test_aggregate_agent_metrics():
    snaps = [
        {"task_nodes": {
            "a": {"task_name": "infer", "status": "succeeded", "duration_seconds": 2.0, "metrics": {"tokens_in": 10, "tokens_out": 5}},
        }},
        {"task_nodes": {
            "a": {"task_name": "infer", "status": "failed", "duration_seconds": 4.0, "degraded": True, "metrics": {"tokens_in": 6}},
        }},
    ]
    out = aggregate_agent_metrics(snaps)
    agent = out["agents"][0]
    assert agent["task_name"] == "infer"
    assert agent["runs"] == 2
    assert agent["succeeded"] == 1
    assert agent["failed"] == 1
    assert agent["degraded"] == 1
    assert agent["success_rate"] == 0.5
    assert agent["tokens_in"] == 16
    assert agent["max_duration_seconds"] == 4.0


# --- C11 error summary ---

def test_summarize_run_errors_infers_stage():
    snapshot = {
        "run_id": "r1",
        "status": "failed",
        "task_nodes": {
            "t": {
                "task_id": "t", "task_name": "t", "status": "failed", "attempt": 1,
                "error": {"error_type": "user_code", "message": "boom", "origin": "runner"},
            },
        },
    }
    out = summarize_run_errors(snapshot)
    assert out["failure_count"] == 1
    f = out["failures"][0]
    assert f["error_type"] == "user_code"
    assert f["message"] == "boom"
    assert f["stage"] == "execution"


# --- C6 context store ---

def test_context_store_roundtrip(tmp_path):
    store = ContextStore(workspace_dir=tmp_path)
    store.set("agent-1", "memory", {"step": 1})
    rec = store.get("agent-1", "memory")
    assert rec["value"] == {"step": 1}
    items = store.list("agent-1")
    assert len(items) == 1
    assert store.delete("agent-1", "memory") is True
    assert store.get("agent-1", "memory") is None


def test_context_store_rejects_bad_names(tmp_path):
    store = ContextStore(workspace_dir=tmp_path)
    with pytest.raises(ValueError):
        store.set("bad/ns", "k", 1)
    with pytest.raises(ValueError):
        store.set("ns", "../escape", 1)
