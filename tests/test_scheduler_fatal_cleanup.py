import threading
import time
from types import SimpleNamespace

import pytest

from maze.core.scheduler.llm_instance import LlmInstanceMessage
from maze.core.scheduler.scheduler import Scheduler


class _Socket:
    def connect(self, _address):
        return None

    def bind(self, _address):
        return None

    def recv_multipart(self):
        raise RuntimeError("injected receive failure")


class _Context:
    def socket(self, _socket_type):
        return _Socket()


class _TaskQueues:
    def __init__(self, task):
        self.task = task

    def wait_for_task(self, timeout=None):
        return True

    def queue_names(self):
        return ("cpu", "gpu", "io")

    def peek(self, queue_name, now):
        return self.task if queue_name == "cpu" else None

    def pop_head(self, queue_name, task):
        return task


class _OneMessageQueue:
    def __init__(self, message):
        self.message = message

    def get(self, timeout=None):
        return self.message


class _FailingSelectionManager:
    def select_node(self, **_kwargs):
        raise RuntimeError("injected selection failure")


def _scheduler_with_lock_check():
    scheduler = object.__new__(Scheduler)
    scheduler.context = _Context()
    scheduler.port2 = 12345
    scheduler.lock = threading.Lock()
    scheduler.fatal_event = threading.Event()
    scheduler.ready_messages = []
    scheduler.ready_queue = SimpleNamespace(put=scheduler.ready_messages.append)
    scheduler.stopped_workflow_ids = set()
    scheduler.cleanup_lock_was_free = False

    def cleanup(*, exit_code=0):
        acquired = scheduler.lock.acquire(blocking=False)
        scheduler.cleanup_lock_was_free = acquired
        if acquired:
            scheduler.lock.release()
        if not acquired:
            raise AssertionError("fatal cleanup was entered with Scheduler.lock held")
        raise SystemExit(exit_code)

    scheduler._cleanup = cleanup
    return scheduler


def _run_and_assert_fatal(scheduler, name, target, *args):
    with pytest.raises(SystemExit) as exc_info:
        scheduler._run_critical_thread(name, target, *args)

    assert exc_info.value.code == 1
    assert scheduler.cleanup_lock_was_free is True
    assert scheduler.fatal_event.is_set()
    assert scheduler.ready_messages == [{
        "status": "error",
        "error": f"Critical scheduler thread {name} failed: injected selection failure"
        if name != "receive"
        else "Critical scheduler thread receive failed: injected receive failure",
    }]


def test_submit_failure_releases_scheduler_lock_before_fatal_cleanup():
    scheduler = _scheduler_with_lock_check()
    task = SimpleNamespace(
        workflow_id="run-1",
        task_id="task-1",
        attempt=0,
        next_eligible_time=time.time() - 1,
        scheduler_resources={"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0},
        set_task_status=lambda _status: None,
    )
    scheduler.task_queues = _TaskQueues(task)
    scheduler.workflow_manager = SimpleNamespace(add_task=lambda _task: True)
    scheduler.resource_manager = _FailingSelectionManager()

    _run_and_assert_fatal(
        scheduler,
        "submit",
        scheduler._submit_thread,
        scheduler.port2,
    )


def test_llm_selection_failure_releases_scheduler_lock_before_fatal_cleanup():
    scheduler = _scheduler_with_lock_check()
    scheduler.resource_manager = _FailingSelectionManager()
    scheduler.llm_instance_manager = SimpleNamespace(
        has_instance=lambda _instance_id: False
    )
    scheduler.llm_instance_queue = _OneMessageQueue(LlmInstanceMessage(
        "start_llm_instance",
        {
            "instance_id": "instance-1",
            "model": "model",
            "backend": "vllm",
            "cpu_nums": 1,
            "memory": 0,
            "gpu_nums": 1,
            "gpu_mem": 0,
        },
    ))

    _run_and_assert_fatal(
        scheduler,
        "llm-instance",
        scheduler._llm_instance_thread,
        scheduler.port2,
    )


def test_supervisor_failure_releases_scheduler_lock_before_fatal_cleanup():
    scheduler = _scheduler_with_lock_check()
    scheduler._manage_llm_instance_scaling = lambda: None
    scheduler.resource_manager = SimpleNamespace(
        check_dead_node=lambda: (_ for _ in ()).throw(
            RuntimeError("injected selection failure")
        )
    )

    _run_and_assert_fatal(
        scheduler,
        "supervisor",
        scheduler._supervisor_thread,
        scheduler.port2,
    )


def test_receive_failure_uses_the_same_fatal_wrapper():
    scheduler = _scheduler_with_lock_check()

    _run_and_assert_fatal(
        scheduler,
        "receive",
        scheduler._receive_thread,
        12344,
    )


def test_expected_thread_rejection_during_shutdown_is_not_fatal():
    scheduler = _scheduler_with_lock_check()
    scheduler._request_shutdown()

    def fail_during_shutdown():
        raise RuntimeError("cannot schedule new futures after shutdown")

    scheduler._run_critical_thread("supervisor", fail_during_shutdown)

    assert scheduler.fatal_event.is_set() is False
    assert scheduler.ready_messages == []
    assert scheduler.cleanup_lock_was_free is False
