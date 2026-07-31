import json
import multiprocessing as mp
import threading
from types import SimpleNamespace

import pytest

from maze.core.scheduler import scheduler as scheduler_module
from maze.core.scheduler.resource import Node, ResourceManager
from maze.core.scheduler.runtime import SelectedNode, TaskRuntime
from maze.core.scheduler.scheduler import PriorityQueue, Scheduler
from maze.core.workflow.task import TaskType


TASK_RESOURCES = {
    "cpu": 1,
    "cpu_mem": 128,
    "gpu": 0,
    "gpu_mem": 0,
}


def _failing_supervisor_process():
    scheduler = object.__new__(Scheduler)
    scheduler.context = SimpleNamespace(
        socket=lambda *_args, **_kwargs: SimpleNamespace(connect=lambda *_args: None)
    )
    scheduler.lock = threading.Lock()
    scheduler.resource_manager = SimpleNamespace(
        check_dead_node=lambda: True,
        show_all_node_resource=lambda: None,
    )
    scheduler.ready_queue = SimpleNamespace(put=lambda _message: None)
    scheduler.llm_instance_manager = SimpleNamespace(
        stop_all_llm_instances=lambda: ({}, {}),
    )
    scheduler_module.stop_ray_runtime = lambda **_kwargs: None
    scheduler._fail_timed_out_tasks = lambda _socket: None
    scheduler.workflow_manager = SimpleNamespace(
        get_running_task_refs=lambda: ["object-ref"],
    )
    scheduler_module.ray.wait = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("supervisor failed")
    )
    scheduler._run_critical_thread(
        "supervisor",
        scheduler._supervisor_thread,
        12345,
    )


def _task_message(workflow_id: str, task_id: str):
    return {
        "workflow_id": workflow_id,
        "task_id": task_id,
        "task_type": TaskType.CODE.value,
        "task_input": {"input_params": {}},
        "task_output": {},
        "resources": dict(TASK_RESOURCES),
        "code_str": "def task(): return {}",
    }


def test_critical_supervisor_failure_exits_scheduler_process_nonzero():
    context = mp.get_context("fork")
    process = context.Process(target=_failing_supervisor_process)
    process.start()
    try:
        process.join(timeout=5)
        assert not process.is_alive()
        assert process.exitcode == 1
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)


def test_submit_rechecks_stopped_workflow_after_acquiring_scheduler_lock():
    first_check = threading.Event()
    allow_submit_to_continue = threading.Event()
    second_get = threading.Event()
    task = SimpleNamespace(
        workflow_id="run-1",
        task_id="task-1",
        priority=0,
        next_eligible_time=0,
    )

    class Queue:
        def __init__(self):
            self.calls = 0

        def get(self):
            self.calls += 1
            if self.calls == 1:
                return task
            second_get.set()
            threading.Event().wait()

        def put(self, *_args, **_kwargs):
            pytest.fail("stopped task was requeued")

    scheduler = object.__new__(Scheduler)
    scheduler.context = SimpleNamespace(
        socket=lambda *_args, **_kwargs: SimpleNamespace(connect=lambda *_args: None)
    )
    scheduler.lock = threading.Lock()
    scheduler.stopped_workflow_ids = set()
    scheduler.task_queue = Queue()
    scheduler.workflow_manager = SimpleNamespace(
        add_task=lambda _task: pytest.fail("stopped task was added to a workflow")
    )
    scheduler.resource_manager = SimpleNamespace(
        select_node=lambda **_kwargs: pytest.fail("stopped task reserved resources")
    )
    checks = 0

    def stopped_workflows():
        nonlocal checks
        checks += 1
        if checks == 1:
            first_check.set()
            assert allow_submit_to_continue.wait(timeout=2)
            return set()
        return scheduler.stopped_workflow_ids

    scheduler._stopped_workflows = stopped_workflows
    submit_thread = threading.Thread(
        target=scheduler._submit_thread,
        args=(12345,),
        daemon=True,
    )
    submit_thread.start()

    assert first_check.wait(timeout=2)
    with scheduler.lock:
        scheduler.stopped_workflow_ids.add("run-1")
    allow_submit_to_continue.set()

    assert second_get.wait(timeout=2)
    assert scheduler.cur_ready_task is None
    assert checks >= 2


def test_retry_keeps_workflow_open_clears_active_identity_and_avoids_failed_node():
    task = TaskRuntime(
        "run-1",
        "task-1",
        task_input={"input_params": {}},
        task_output={},
        resources=dict(TASK_RESOURCES),
        code_str="def task(): return {}",
    )
    task.begin_attempt("dispatch-1", "lease-1")
    task.status = "running"
    task.object_ref = "object-ref"
    task.selected_node = SelectedNode("worker-1", "10.0.0.2")
    task.last_schedule_decision = {
        "lease_id": "lease-1",
        "selected_node": {"node_id": "worker-1"},
    }

    released = []
    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.Lock()
    scheduler.stopped_workflow_ids = set()
    scheduler.task_queue = PriorityQueue()
    scheduler.workflow_manager = SimpleNamespace(
        clear_task_ref=lambda current: None,
        cancel_workflow=lambda _workflow_id: pytest.fail("retry cancelled its workflow"),
    )
    scheduler.resource_manager = SimpleNamespace(
        release_task_resource=lambda tasks: released.extend(tasks),
    )
    sent = []
    socket = SimpleNamespace(send=sent.append)

    scheduler._retry_or_fail_task(
        socket,
        task,
        {
            "error_type": "node_lost",
            "message": "worker exited",
            "retryable": True,
            "node_id": "worker-1",
        },
    )

    assert scheduler.stopped_workflow_ids == set()
    assert released == [task]
    retry_event = json.loads(sent[0])
    assert retry_event["type"] == "task_retry"
    assert retry_event["data"]["attempt"] == 1
    assert retry_event["data"]["dispatch_id"] == "dispatch-1"
    assert retry_event["data"]["lease_id"] == "lease-1"
    assert task.dispatch_id is None
    assert task.lease_id is None
    assert task.object_ref is None
    assert task.selected_node is None
    assert task.last_schedule_decision is None
    assert task.resources["avoid_node_ids"] == ["worker-1"]
    assert scheduler._task_queue_snapshot_item(task, 0)["lease_id"] is None
    assert scheduler._task_queue_snapshot_item(task, 0)["schedule_decision"] is None

    assert scheduler._enqueue_task_message(_task_message("run-1", "child")) is True
    queued_task_ids = {queued.task_id for queued in scheduler.task_queue.snapshot()}
    assert queued_task_ids == {"task-1", "child"}


def test_queue_snapshot_aggregates_active_lease_inventory_without_lease_records():
    scheduler = object.__new__(Scheduler)
    scheduler.stopped_workflow_ids = set()
    scheduler.task_queue = PriorityQueue()
    scheduler.workflow_manager = SimpleNamespace(get_running_tasks=lambda: [])
    scheduler.resource_manager = SimpleNamespace(
        scheduling_policy="least-loaded",
        active_leases={
            "task-lease": {"reservation_kind": "task", "dispatch_id": "private-task"},
            "instance-lease": {"reservation_kind": "instance", "instance_id": "private-model"},
            "unknown-lease": {},
        },
    )

    snapshot = scheduler.get_queue_snapshot()

    assert snapshot["active_lease_count"] == 3
    assert snapshot["active_lease_counts_by_kind"] == {
        "instance": 1,
        "task": 1,
        "unknown": 1,
    }
    serialized = json.dumps(snapshot)
    assert "task-lease" not in serialized
    assert "instance-lease" not in serialized
    assert "private-task" not in serialized
    assert "private-model" not in serialized


def test_queue_snapshot_tracks_real_task_and_instance_lease_lifecycle():
    manager = ResourceManager()
    capacity = {
        "cpu": 4,
        "cpu_mem": 1024,
        "gpu_resource": {},
    }
    manager.nodes["node-1"] = Node(
        "node-1",
        "127.0.0.1",
        capacity,
        capacity,
    )
    manager.running_task_counts["node-1"] = 0
    manager._ray_node_index = lambda: {}
    manager._is_node_alive = lambda *_args: True

    task_lease = manager.select_node(
        TASK_RESOURCES,
        reservation_kind="task",
        run_id="run-1",
        task_id="task-1",
        attempt=1,
        dispatch_id="dispatch-1",
    )
    instance_lease = manager.select_node(
        TASK_RESOURCES,
        reservation_kind="instance",
        run_id="instance-1",
    )
    assert task_lease and instance_lease

    scheduler = object.__new__(Scheduler)
    scheduler.stopped_workflow_ids = set()
    scheduler.task_queue = PriorityQueue()
    scheduler.workflow_manager = SimpleNamespace(get_running_tasks=lambda: [])
    scheduler.resource_manager = manager

    allocated = scheduler.get_queue_snapshot()
    assert allocated["active_lease_count"] == 2
    assert allocated["active_lease_counts_by_kind"] == {"instance": 1, "task": 1}

    task = TaskRuntime(
        "run-1",
        "task-1",
        task_input={"input_params": {}},
        task_output={},
        resources=dict(TASK_RESOURCES),
    )
    task.begin_attempt("dispatch-1", task_lease.lease_id)
    manager.release_task_resource([task])
    task_released = scheduler.get_queue_snapshot()
    assert task_released["active_lease_count"] == 1
    assert task_released["active_lease_counts_by_kind"] == {"instance": 1}

    manager.release_instance_resource({"lease_id": instance_lease.lease_id})
    released = scheduler.get_queue_snapshot()
    assert released["active_lease_count"] == 0
    assert released["active_lease_counts_by_kind"] == {}


def test_priority_queue_discards_all_tasks_for_terminal_workflow():
    task_queue = PriorityQueue()
    task_queue.put(SimpleNamespace(workflow_id="run-1", task_id="a"), 0)
    task_queue.put(SimpleNamespace(workflow_id="run-2", task_id="b"), 0)
    task_queue.put(SimpleNamespace(workflow_id="run-1", task_id="c"), 1)

    assert task_queue.discard_workflow("run-1") == 2
    assert [task.task_id for task in task_queue.snapshot()] == ["b"]
