import asyncio

from maze.core.path.path import MaPath
from maze.core.workflow.dynamic import DynamicRun, DynamicTaskSpec
from maze.core.workflow.dynamic_store import DynamicRunStore


def test_finish_notification_observes_persisted_terminal_task(tmp_path):
    run_id = "dynamic-run"
    run = DynamicRun(run_id)
    task, _ = run.append_task(
        DynamicTaskSpec(
            task_spec_id="fast-task",
            task_name="fast_task",
            code_str="def fast_task(): return {'status': 'ok'}",
            code_ser=None,
        )
    )
    run.mark_started(task.task_id)

    path = object.__new__(MaPath)
    path.dynamic_runs = {run_id: run}
    path.dynamic_run_store = DynamicRunStore(tmp_path)
    path.task_attempts = {}
    path._submit_dynamic_task = lambda ready_task: None

    observed = {}

    class CompletionNotificationQueue:
        async def put(self, message):
            snapshot = path.dynamic_run_store.load_run(run_id)
            observed.update({
                "message": message,
                "completed": task.task_id in snapshot["tasks"]["completed"],
                "running": task.task_id in snapshot["tasks"]["running"],
                "can_finalize": run.can_finalize(),
            })

    path.async_que = {run_id: CompletionNotificationQueue()}

    asyncio.run(path._handle_dynamic_scheduler_event({
        "type": "finish_task",
        "data": {
            "workflow_id": run_id,
            "task_id": task.task_id,
            "result": {"status": "ok"},
        },
    }))

    assert observed == {
        "message": {"type": "dynamic_event"},
        "completed": True,
        "running": False,
        "can_finalize": True,
    }

    run.finalize({"status": "done"})
    assert run.status == "finalized"
