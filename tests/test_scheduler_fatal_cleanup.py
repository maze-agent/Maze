import multiprocessing as mp
import threading
import time
from types import SimpleNamespace

from maze.core.scheduler import scheduler as scheduler_module
from maze.core.scheduler.scheduler import Scheduler


class _TaskQueue:
    def __init__(self, task):
        self.task = task

    def get(self):
        return self.task


class _FailingResourceManager:
    def __init__(self, selection_reached, finalize_reached):
        self.selection_reached = selection_reached
        self.finalize_reached = finalize_reached

    def select_node(self, **_kwargs):
        self.selection_reached.set()
        raise RuntimeError("injected selection failure")

    def release_instance_resource(self, _resource_detail):
        self.finalize_reached.set()
        return None


def _scheduler_with_model_cleanup(selection_reached, finalize_reached, fatal_event):
    scheduler = object.__new__(Scheduler)
    scheduler.context = SimpleNamespace(
        socket=lambda *_args, **_kwargs: SimpleNamespace(connect=lambda *_args: None)
    )
    scheduler.lock = threading.Lock()
    scheduler._process_exit_lock = threading.Lock()
    scheduler.fatal_event = fatal_event
    scheduler.ready_queue = SimpleNamespace(put=lambda _message: None)
    scheduler.stopped_workflow_ids = set()
    scheduler.resource_manager = _FailingResourceManager(
        selection_reached,
        finalize_reached,
    )
    scheduler.llm_instance_manager = SimpleNamespace(
        stop_all_llm_instances=lambda: ({"instance-1": {"lease_id": "lease-1"}}, {}),
        finalize_stopped_instance=lambda _instance_id: None,
        stop_owned_llm_processes=lambda: None,
    )
    scheduler_module.stop_ray_runtime = lambda **_kwargs: SimpleNamespace(
        returncode=0,
        stdout="",
        stderr="",
    )
    return scheduler


def _submit_failure_with_model_cleanup(
    selection_reached,
    finalize_reached,
    fatal_event,
):
    task = SimpleNamespace(
        workflow_id="run-1",
        task_id="task-1",
        priority=0,
        next_eligible_time=time.time() - 1,
        attempt=0,
        resources={"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0},
        set_task_status=lambda _status: None,
    )
    scheduler = _scheduler_with_model_cleanup(
        selection_reached,
        finalize_reached,
        fatal_event,
    )
    scheduler.task_queue = _TaskQueue(task)
    scheduler.workflow_manager = SimpleNamespace(add_task=lambda _task: True)

    scheduler._run_critical_thread(
        "submit",
        scheduler._submit_thread,
        12345,
    )


def _llm_selection_failure_with_model_cleanup(
    selection_reached,
    finalize_reached,
    fatal_event,
):
    scheduler = _scheduler_with_model_cleanup(
        selection_reached,
        finalize_reached,
        fatal_event,
    )
    scheduler.llm_instance_queue = _TaskQueue(SimpleNamespace(
        message_type="start_llm_instance",
        message_data={
            "instance_id": "new-instance",
            "model": "model",
            "backend": "vllm",
            "cpu_nums": 1,
            "memory": 0,
            "gpu_nums": 1,
            "gpu_mem": 0,
        },
    ))

    scheduler._run_critical_thread(
        "llm-instance",
        scheduler._llm_instance_thread,
        12345,
    )


def _assert_fatal_process_exits(target):
    context = mp.get_context("fork")
    selection_reached = context.Event()
    finalize_reached = context.Event()
    fatal_event = context.Event()
    process = context.Process(
        target=target,
        args=(selection_reached, finalize_reached, fatal_event),
    )
    process.start()
    try:
        process.join(timeout=2)
        assert not process.is_alive(), "fatal cleanup deadlocked on the Scheduler lock"
        assert process.exitcode == 1
        assert selection_reached.is_set(), "failure injection did not reach node selection"
        assert finalize_reached.is_set(), "fatal cleanup did not re-enter the Scheduler lock"
        assert fatal_event.is_set(), "fatal cleanup did not signal the parent process"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)


def test_submit_failure_releases_scheduler_lock_before_fatal_cleanup():
    _assert_fatal_process_exits(_submit_failure_with_model_cleanup)


def test_llm_selection_failure_releases_scheduler_lock_before_fatal_cleanup():
    _assert_fatal_process_exits(_llm_selection_failure_with_model_cleanup)
