import json
import threading
import time
from types import SimpleNamespace

from maze.core.scheduler import runtime as runtime_module
from maze.core.scheduler import scheduler as scheduler_module
from maze.core.scheduler.queues import HeterogeneousTaskQueues
from maze.core.scheduler.resource import Node, ResourceManager
from maze.core.scheduler.runtime import SelectedNode, TaskRuntime, WorkflowRuntimeManager
from maze.core.scheduler.scheduler import Scheduler
from maze.core.scheduler.strategy import create_scheduling_strategy


PUBLIC_TASK_RESOURCES = {
    "cpu_num": 1,
    "gpu_mem": 0,
    "io_num": 0,
}
NODE_RESOURCES = {
    "cpu": 4,
    "cpu_mem": 1024,
    "gpu_resource": {},
}


def _task(**overrides):
    kwargs = {
        "workflow_id": "run-1",
        "task_id": "task-1",
        "task_input": {"input_params": {}},
        "task_output": {},
        "resources": dict(PUBLIC_TASK_RESOURCES),
        "code_str": "def task(): return {}",
    }
    kwargs.update(overrides)
    return TaskRuntime(**kwargs)


def _two_node_manager():
    manager = ResourceManager()
    for node_id, node_ip in (("worker-old", "10.0.0.2"), ("worker-new", "10.0.0.3")):
        manager.nodes[node_id] = Node(
            node_id,
            node_ip,
            NODE_RESOURCES,
            NODE_RESOURCES,
        )
        manager.running_task_counts[node_id] = 0
    manager._ray_node_index = lambda: {
        "worker-old": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
        "worker-new": {"Alive": True, "NodeManagerAddress": "10.0.0.3"},
    }
    return manager


def test_node_loss_retry_releases_old_lease_and_reselects_another_worker():
    manager = _two_node_manager()
    task = _task(max_retries=1)
    first = manager.select_node(
        task.scheduler_resources,
        run_id=task.workflow_id,
        task_id=task.task_id,
        attempt=1,
        dispatch_id="dispatch-1",
    )
    assert first.node_id == "worker-old"
    task.begin_attempt("dispatch-1", first.lease_id)
    task.status = "running"
    task.object_ref = "object-ref"
    task.selected_node = first.selected_node
    task.last_schedule_decision = first.decision

    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.Lock()
    scheduler.stopped_workflow_ids = set()
    scheduler.scheduling_strategy = create_scheduling_strategy("FCFS")
    scheduler.task_queues = HeterogeneousTaskQueues(scheduler.scheduling_strategy)
    scheduler.workflow_manager = SimpleNamespace(
        clear_task_ref=lambda _task: None,
        cancel_workflow=lambda _workflow_id: (_ for _ in ()).throw(
            AssertionError("retry cancelled its workflow")
        ),
    )
    scheduler.resource_manager = manager
    sent = []

    scheduler._retry_or_fail_task(
        SimpleNamespace(send=sent.append),
        task,
        {
            "error_type": "node_lost",
            "message": "worker exited",
            "retryable": True,
            "node_id": "worker-old",
        },
    )

    retry_event = json.loads(sent[0])
    assert retry_event["type"] == "task_retry"
    assert retry_event["data"]["attempt"] == 1
    assert retry_event["data"]["dispatch_id"] == "dispatch-1"
    assert retry_event["data"]["lease_id"] == first.lease_id
    assert first.lease_id not in manager.active_leases
    assert manager.nodes["worker-old"].available_resources["cpu"] == 4
    assert scheduler.stopped_workflow_ids == set()
    assert task.dispatch_id is None
    assert task.lease_id is None
    assert task.object_ref is None
    assert task.selected_node is None
    assert task.resources["avoid_node_ids"] == ["worker-old"]
    assert scheduler.task_queues.snapshot() == [task]

    second = manager.select_node(
        task.scheduler_resources,
        run_id=task.workflow_id,
        task_id=task.task_id,
        attempt=2,
        dispatch_id="dispatch-2",
    )
    assert second.node_id == "worker-new"
    assert manager.active_leases[second.lease_id]["attempt"] == 2
    assert manager.active_leases[second.lease_id]["dispatch_id"] == "dispatch-2"


def test_timeout_terminal_event_keeps_attempt_identity_and_releases_lease(monkeypatch):
    task = _task(max_retries=0, timeout_seconds=0)
    task.begin_attempt("dispatch-timeout", "lease-timeout")
    task.status = "running"
    task.started_time = time.time() - 1
    task.object_ref = "object-ref"
    task.selected_node = SelectedNode("worker-1", "10.0.0.2")

    released = []
    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.Lock()
    scheduler.stopped_workflow_ids = set()
    scheduler.workflow_manager = SimpleNamespace(
        get_running_tasks=lambda: [task],
        clear_task_ref=lambda _task: None,
        cancel_workflow=lambda _workflow_id: [task],
        clear_workflow=lambda _workflow_id: None,
    )
    scheduler.resource_manager = SimpleNamespace(
        release_task_resource=lambda tasks: released.extend(tasks),
        release_dag_context=lambda _workflow_id: True,
    )
    cancelled = []
    monkeypatch.setattr(
        scheduler_module.ray,
        "cancel",
        lambda object_ref, force: cancelled.append((object_ref, force)),
    )
    sent = []

    scheduler._fail_timed_out_tasks(SimpleNamespace(send=sent.append))

    assert cancelled == [("object-ref", True)]
    assert released == [task]
    assert scheduler.stopped_workflow_ids == {"run-1"}
    event = json.loads(sent[0])
    assert event["type"] == "task_exception"
    assert event["data"]["error"]["error_type"] == "timeout"
    assert event["data"]["attempt"] == 1
    assert event["data"]["dispatch_id"] == "dispatch-timeout"
    assert event["data"]["lease_id"] == "lease-timeout"


def test_terminal_failure_releases_model_routes_for_cancelled_siblings():
    failing = _task(task_id="task-failing", max_retries=0)
    sibling = _task(task_id="task-sibling", max_retries=0)
    for index, task in enumerate((failing, sibling), 1):
        task.begin_attempt(f"dispatch-{index}", f"lease-{index}")
        task.status = "running"
        task.model_route = {
            "instance_id": "model-instance",
            "request_id": index,
        }

    inflight_requests = {"model-instance": 2}

    def release_model_route(route):
        instance_id = route["instance_id"]
        inflight_requests[instance_id] -= 1

    released_tasks = []
    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.Lock()
    scheduler.stopped_workflow_ids = set()
    scheduler.llm_instance_manager = SimpleNamespace(
        release_model_route=release_model_route,
    )
    scheduler.workflow_manager = SimpleNamespace(
        clear_task_ref=lambda _task: None,
        cancel_workflow=lambda _workflow_id: [failing, sibling],
        clear_workflow=lambda _workflow_id: None,
    )
    scheduler.resource_manager = SimpleNamespace(
        release_task_resource=lambda tasks: released_tasks.extend(tasks),
        release_dag_context=lambda _workflow_id: True,
    )
    sent = []

    scheduler._retry_or_fail_task(
        SimpleNamespace(send=sent.append),
        failing,
        {
            "error_type": "user_code",
            "message": "terminal failure",
            "retryable": False,
        },
    )

    assert inflight_requests == {"model-instance": 0}
    assert failing.model_route is None
    assert sibling.model_route is None
    assert released_tasks == [failing, sibling]
    assert json.loads(sent[0])["type"] == "task_exception"


def test_cancel_returns_running_attempt_for_one_idempotent_lease_release(monkeypatch):
    manager = ResourceManager()
    manager.nodes["worker"] = Node(
        "worker",
        "10.0.0.2",
        NODE_RESOURCES,
        NODE_RESOURCES,
    )
    manager.running_task_counts["worker"] = 0
    manager._ray_node_index = lambda: {
        "worker": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
    }
    selection = manager.select_node(
        {
            "cpu": 1,
            "cpu_mem": 128,
            "gpu": 0,
            "gpu_mem": 0,
        },
        run_id="run-1",
        task_id="task-1",
        attempt=1,
        dispatch_id="dispatch-cancel",
    )
    task = _task(max_retries=0)
    task.begin_attempt("dispatch-cancel", selection.lease_id)

    runtime = WorkflowRuntimeManager()
    runtime.add_task(task)
    runtime.workflows[task.workflow_id].add_runtime_info(
        task.task_id,
        "object-ref",
        selection.selected_node,
    )
    cancelled_refs = []
    monkeypatch.setattr(
        runtime_module.ray,
        "cancel",
        lambda ref, force: cancelled_refs.append((ref, force)),
    )

    cancelled_tasks = runtime.cancel_workflow(task.workflow_id)
    assert cancelled_tasks == [task]
    assert cancelled_refs == [("object-ref", True)]
    assert task.dispatch_id == "dispatch-cancel"
    assert task.lease_id == selection.lease_id

    manager.release_task_resource(cancelled_tasks)
    manager.release_task_resource(cancelled_tasks)
    assert manager.active_leases == {}
    assert manager.nodes["worker"].available_resources["cpu"] == 4
    assert manager.running_task_counts["worker"] == 0


def test_ray_membership_unavailable_does_not_release_or_replace_active_dispatch(monkeypatch):
    manager = _two_node_manager()
    task = _task()
    selection = manager.select_node(
        task.scheduler_resources,
        run_id=task.workflow_id,
        task_id=task.task_id,
        attempt=1,
        dispatch_id="dispatch-1",
    )
    task.begin_attempt("dispatch-1", selection.lease_id)
    active_lease = dict(manager.active_leases[selection.lease_id])

    monkeypatch.setattr(
        manager,
        "_ray_node_index",
        lambda: (_ for _ in ()).throw(
            resource_query_error("gcs unavailable")
        ),
    )

    assert manager.check_dead_node() is False
    pending = manager.select_node(
        task.scheduler_resources,
        run_id=task.workflow_id,
        task_id=task.task_id,
        attempt=2,
        dispatch_id="dispatch-2",
    )
    assert not pending
    assert pending.decision["reason"] == "ray_cluster_unavailable"
    assert manager.active_leases == {selection.lease_id: active_lease}


def resource_query_error(message):
    from maze.core.scheduler.resource import RayNodeQueryError

    return RayNodeQueryError(message)
