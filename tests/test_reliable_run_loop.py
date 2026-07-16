import json
import time

from maze.core.workflow.dynamic import DynamicRun
from maze.core.workflow.dynamic_store import DynamicRunStore
from maze.core.workflow.static_run import StaticRun
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


def make_workflow() -> Workflow:
    workflow = Workflow("workflow")
    task = CodeTask("workflow", "task", "task")
    task.save_task(
        task_input={"input_params": {}},
        task_output={"output_params": {"1": {"key": "result", "data_type": "str"}}},
        code_str="def task():\n    return {'result': 'ok'}",
        code_ser=None,
        resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
        task_kind="cpu",
    )
    workflow.add_task("task", task)
    return workflow


def test_static_run_deadline_terminalizes_active_tasks():
    run = StaticRun("run", "workflow", make_workflow(), timeout_seconds=1)
    run.submitted_time = time.time() - 2

    assert run.mark_timed_out_if_needed() is True
    assert run.status == "timed_out"
    assert run.task_nodes["task"]["status"] == "timed_out"
    assert run.snapshot()["task_counts"]["timed_out"] == 1


def test_dynamic_run_deadline_handles_pending_task_mapping():
    run = DynamicRun("run", timeout_seconds=1)
    task = make_workflow().tasks["task"]
    run.tasks[task.task_id] = task
    run.pending_tasks[task.task_id] = task
    run.created_time = time.time() - 2

    assert run.mark_timed_out_if_needed() is True
    assert run.status == "timed_out"
    assert run.pending_tasks == {}
    assert run.failed_tasks == {task.task_id}
    assert run.snapshot()["task_nodes"][task.task_id]["status"] == "failed"


def test_dynamic_store_mirrors_external_workspace_to_canonical_store(tmp_path):
    store = DynamicRunStore(tmp_path / "default")
    external_workspace = tmp_path / "external"
    snapshot = {
        "run_id": "run",
        "status": "interrupted",
        "created_time": time.time(),
        "file_context": {
            "enabled": True,
            "workspace_dir": str(external_workspace),
        },
    }
    event = {"type": "interrupt_dynamic_run", "data": {"run_id": "run"}, "seq": 1}

    store.append_event("run", event, snapshot=snapshot)
    store.save_run(snapshot)

    canonical_run = store.run_json_path("run")
    mirrored_run = external_workspace / "runs" / "run" / "dynamic_run.json"
    canonical_events = store.events_path("run")
    mirrored_events = external_workspace / "runs" / "run" / "dynamic_events.jsonl"
    assert json.loads(canonical_run.read_text(encoding="utf-8"))["status"] == "interrupted"
    assert json.loads(mirrored_run.read_text(encoding="utf-8"))["status"] == "interrupted"
    assert json.loads(canonical_events.read_text(encoding="utf-8"))["type"] == event["type"]
    assert json.loads(mirrored_events.read_text(encoding="utf-8"))["type"] == event["type"]
