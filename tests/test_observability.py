import json

from maze.core.runs.global_metrics import GlobalMetrics
from maze.core.runs.static_snapshot import (
    RunMetrics,
    StaticRunSnapshot,
    TaskSnapshot,
    merge_metrics,
)
from maze.core.runs.static_store import StaticRunStore


def test_merge_metrics_sums_numeric_and_merges_models():
    merged = merge_metrics(
        {
            "tokens_in": 10,
            "tokens_out": 4,
            "cost_usd": 0.1,
            "by_model": {"gpt-a": {"tokens_in": 10, "calls": 1}},
        },
        {
            "tokens_in": 7,
            "tokens_out": 3,
            "cost_usd": 0.2,
            "by_model": {
                "gpt-a": {"tokens_out": 3, "calls": 2},
                "gpt-b": {"tokens_in": 5, "tokens_out": 1, "calls": 1},
            },
        },
    )

    assert merged["tokens_in"] == 17
    assert merged["tokens_out"] == 7
    assert merged["cost_usd"] == 0.30000000000000004
    assert merged["by_model"]["gpt-a"]["tokens_in"] == 10
    assert merged["by_model"]["gpt-a"]["tokens_out"] == 3
    assert merged["by_model"]["gpt-a"]["calls"] == 3
    assert merged["by_model"]["gpt-b"]["tokens_in"] == 5


def test_run_metrics_uses_nested_by_model_without_double_counting():
    metrics = RunMetrics()
    metrics.absorb(
        {
            "model": "top-level-ignored",
            "tokens_in": 10,
            "tokens_out": 5,
            "cost_usd": 0.25,
            "by_model": {
                "gpt-a": {
                    "tokens_in": 10,
                    "tokens_out": 5,
                    "cost_usd": 0.25,
                    "calls": 1,
                }
            },
        }
    )

    payload = metrics.to_json()
    assert payload["tokens_in"] == 10
    assert payload["tokens_out"] == 5
    assert payload["cost_usd"] == 0.25
    assert payload["by_model"] == {
        "gpt-a": {
            "tokens_in": 10,
            "tokens_out": 5,
            "cost_usd": 0.25,
            "calls": 1,
        }
    }


def test_global_metrics_tracks_run_task_and_token_counters():
    metrics = GlobalMetrics()

    metrics.on_workflow_created("wf-1")
    metrics.on_run_submitted("run-1")
    metrics.on_run_status_change("run-1", "submitted", "running")
    metrics.on_task_started("run-1", "task-1")
    metrics.on_task_finished(
        "run-1",
        "task-1",
        "succeeded",
        {
            "tokens_in": 4,
            "tokens_out": 6,
            "cost_usd": 0.12,
            "model": "gpt-a",
        },
    )
    metrics.on_run_status_change("run-1", "running", "succeeded")

    snapshot = metrics.snapshot(workflows_in_memory=2, runs_in_memory=1)
    assert snapshot["workflows"]["created_total"] == 1
    assert snapshot["workflows"]["in_memory_not_submitted"] == 2
    assert snapshot["static_runs"]["total"] == 1
    assert snapshot["static_runs"]["by_status"]["succeeded"] == 1
    assert snapshot["tasks"]["total_finished"] == 1
    assert snapshot["tasks"]["by_status"]["succeeded"] == 1
    assert snapshot["tokens"]["in"] == 4
    assert snapshot["tokens"]["out"] == 6
    assert snapshot["tokens"]["cost_usd"] == 0.12
    assert snapshot["tokens"]["by_model"]["gpt-a"]["calls"] == 1


def test_static_run_snapshot_counts_and_roundtrip():
    snap = StaticRunSnapshot(
        run_id="run-1",
        workflow_id="wf-1",
        status="running",
        task_total=3,
        tasks={
            "a": TaskSnapshot(task_id="a", task_name="A", status="succeeded"),
            "b": TaskSnapshot(task_id="b", task_name="B", status="running", started_at=1.0),
            "c": TaskSnapshot(task_id="c", task_name="C", status="pending"),
        },
    )

    payload = snap.to_json()
    assert payload["task_counts"] == {
        "total": 3,
        "done": 1,
        "running": 1,
        "pending": 1,
        "succeeded": 1,
        "failed": 0,
    }

    restored = StaticRunSnapshot.from_json(payload)
    assert restored.run_id == "run-1"
    assert restored.task_done == 1
    assert restored.task_running == 1
    assert restored.task_pending == 1


def test_static_run_task_finish_records_metrics_and_timing():
    from maze.core.workflow.static_run import StaticRun
    from maze.core.workflow.task import CodeTask
    from maze.core.workflow.workflow import Workflow

    workflow = Workflow("wf-1")
    task = CodeTask("wf-1", "task-1", "Task 1")
    workflow.add_task("task-1", task)
    run = StaticRun("run-1", "wf-1", workflow)

    run.mark_task_started(
        "task-1",
        {"node_id": "node-a", "node_ip": "127.0.0.1", "attempt": 1},
    )
    run.mark_task_finished(
        "task-1",
        result={"answer": "ok"},
        metrics={"tokens_in": 3, "tokens_out": 2, "model": "test-model"},
        started_at=10.0,
        finished_at=10.5,
        duration_ms=500,
        node_id="node-a",
    )

    task_node = run.snapshot()["task_nodes"]["task-1"]
    assert task_node["status"] == "succeeded"
    assert task_node["started_time"] == 10.0
    assert task_node["finished_time"] == 10.5
    assert task_node["duration_ms"] == 500
    assert task_node["metrics"] == {
        "tokens_in": 3,
        "tokens_out": 2,
        "model": "test-model",
    }
    assert task_node["selected_node"]["node_id"] == "node-a"


def test_static_run_store_persists_run_events_and_recovers_interrupted(tmp_path):
    store = StaticRunStore(workspace_dir=tmp_path)
    snap = StaticRunSnapshot(
        run_id="run-1",
        workflow_id="wf-1",
        status="running",
        task_total=1,
        tasks={"a": TaskSnapshot(task_id="a", task_name="A", status="running")},
    )

    store.save_run(snap)
    store.append_event("run-1", {"seq": 1, "type": "start_task", "data": {"task_id": "a"}})

    loaded = store.load_run("run-1")
    events = store.load_events("run-1")
    assert loaded.status == "running"
    assert events == [{"schema_version": 1, "seq": 1, "type": "start_task", "data": {"task_id": "a"}}]

    recovered = store.recover_interrupted_runs()
    assert [item.run_id for item in recovered] == ["run-1"]
    interrupted = store.load_run("run-1")
    assert interrupted.status == "interrupted"
    assert interrupted.failure_reason == "Head process restarted before run completed"

    raw_events = [
        json.loads(line)
        for line in store.events_path("run-1").read_text(encoding="utf-8").splitlines()
    ]
    assert raw_events[-1]["type"] == "run_interrupted"
