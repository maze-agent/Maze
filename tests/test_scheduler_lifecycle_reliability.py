import queue
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from maze.core.path import path as path_module
from maze.core.path.path import MaPath
from maze.core.scheduler import scheduler as scheduler_module
from maze.core.scheduler.scheduler import Scheduler
from maze.core.workflow.static_run import StaticRun
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


class _EmptyReadyQueue:
    def get(self, timeout=None):
        raise queue.Empty


class _ExitedProcess:
    pid = 4321
    exitcode = 23

    def __init__(self, *args, **kwargs):
        self.started = False
        self.join_calls = []

    def start(self):
        self.started = True

    def is_alive(self):
        return False

    def join(self, timeout=None):
        self.join_calls.append(timeout)


class _Socket:
    def __init__(self):
        self.closed = False

    def connect(self, _address):
        return None

    def bind(self, _address):
        return None

    def close(self, linger=0):
        self.closed = True


class _Context:
    def socket(self, _kind):
        return _Socket()


def _workflow() -> Workflow:
    workflow = Workflow("workflow")
    task = CodeTask(workflow.id, "task", "task")
    task.save_task(
        task_input={"input_params": {}},
        task_output={"output_params": {}},
        code_str="def task(): return {}",
        code_ser="",
        resources={"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0},
    )
    workflow.add_task(task.task_id, task)
    return workflow


def test_mapath_init_detects_scheduler_exit_before_ready_and_cleans_runtime(monkeypatch):
    process = _ExitedProcess()
    monkeypatch.setattr(path_module.mp, "Queue", _EmptyReadyQueue)
    monkeypatch.setattr(path_module.mp, "Process", lambda **_kwargs: process)
    monkeypatch.setattr(path_module, "get_available_ports", lambda _count: [31001, 31002])
    monkeypatch.setattr(path_module.zmq, "Context", _Context)
    monkeypatch.setattr(path_module.zmq.asyncio, "Context", _Context)

    path = object.__new__(MaPath)
    ray_stops = []
    path._stop_local_ray_best_effort = lambda: ray_stops.append(True)

    with pytest.raises(RuntimeError, match="exited before becoming ready.*exitcode=23"):
        path.init(ray_head_port=32001, strategy="least-loaded")

    assert process.started is True
    assert process.join_calls
    assert ray_stops == [True]
    assert path.socket_to_scheduler.closed is True
    assert path.socket_from_scheduler.closed is True


def test_mapath_scheduler_ready_wait_has_a_deadline():
    path = object.__new__(MaPath)
    path.ready_queue = _EmptyReadyQueue()
    path.scheduler_process = SimpleNamespace(is_alive=lambda: True)

    with pytest.raises(TimeoutError, match="within 0.01 seconds"):
        path._wait_for_scheduler_ready(timeout=0.01)


def test_scheduler_critical_failure_runs_cleanup_with_nonzero_exit():
    scheduler = object.__new__(Scheduler)
    ready_messages = []
    cleanup_codes = []
    scheduler.ready_queue = SimpleNamespace(put=ready_messages.append)

    def cleanup(exit_code=0):
        cleanup_codes.append(exit_code)
        raise SystemExit(exit_code)

    scheduler._cleanup = cleanup

    with pytest.raises(SystemExit) as exc_info:
        scheduler._run_critical_thread(
            "supervisor",
            lambda: (_ for _ in ()).throw(RuntimeError("failed")),
        )

    assert exc_info.value.code == 1
    assert cleanup_codes == [1]
    assert ready_messages == [{
        "status": "error",
        "error": "Critical scheduler thread supervisor failed: failed",
    }]


def test_scheduler_fatal_signal_rejects_submissions_before_cleanup_returns():
    fatal_event = threading.Event()
    cleanup_started = threading.Event()
    finish_cleanup = threading.Event()
    scheduler = object.__new__(Scheduler)
    scheduler.fatal_event = fatal_event
    scheduler.ready_queue = SimpleNamespace(put=lambda _message: None)

    def cleanup(exit_code=0):
        assert exit_code == 1
        assert fatal_event.is_set()
        cleanup_started.set()
        finish_cleanup.wait(timeout=2)

    scheduler._cleanup = cleanup
    thread = threading.Thread(
        target=scheduler._run_critical_thread,
        args=("submit", lambda: (_ for _ in ()).throw(RuntimeError("failed"))),
    )
    thread.start()
    try:
        assert cleanup_started.wait(timeout=1)
        path = object.__new__(MaPath)
        path.scheduler_process = SimpleNamespace(
            pid=4321,
            exitcode=None,
            is_alive=lambda: True,
        )
        path._scheduler_fatal_event = fatal_event

        with pytest.raises(path_module.SchedulerUnavailableError, match="fatal failure"):
            path._require_scheduler_available()
    finally:
        finish_cleanup.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_scheduler_normal_cleanup_does_not_set_fatal_signal(monkeypatch):
    fatal_event = threading.Event()
    owner_cleanup_event = threading.Event()
    ray_cleanup_event = threading.Event()
    scheduler = object.__new__(Scheduler)
    scheduler.fatal_event = fatal_event
    scheduler.owner_cleanup_complete_event = owner_cleanup_event
    scheduler.ray_cleanup_complete_event = ray_cleanup_event
    scheduler._process_exit_lock = threading.Lock()
    scheduler.llm_instance_manager = SimpleNamespace(
        stop_all_llm_instances=lambda: ({}, {}),
        stop_owned_llm_processes=lambda: {},
    )
    monkeypatch.setattr(
        scheduler_module,
        "stop_ray_runtime",
        lambda **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(scheduler_module.os, "_exit", lambda _code: None)

    scheduler._cleanup(exit_code=0)

    assert not fatal_event.is_set()
    assert owner_cleanup_event.is_set()
    assert ray_cleanup_event.is_set()


def test_scheduler_preserves_ray_when_owner_cleanup_is_incomplete(monkeypatch):
    owner_cleanup_event = threading.Event()
    ray_cleanup_event = threading.Event()
    scheduler = object.__new__(Scheduler)
    scheduler.fatal_event = threading.Event()
    scheduler._process_exit_lock = threading.Lock()
    scheduler.owner_cleanup_complete_event = owner_cleanup_event
    scheduler.ray_cleanup_complete_event = ray_cleanup_event
    scheduler.llm_instance_manager = SimpleNamespace(
        begin_shutdown=lambda: None,
        stop_all_llm_instances=lambda: ({}, {}),
        stop_owned_llm_processes=lambda: (_ for _ in ()).throw(
            RuntimeError("owner cleanup failed")
        ),
    )
    monkeypatch.setattr(
        scheduler_module,
        "stop_ray_runtime",
        lambda **_kwargs: pytest.fail("Ray must remain available for owner cleanup retry"),
    )
    monkeypatch.setattr(scheduler_module.os, "_exit", lambda _code: None)

    scheduler._cleanup(exit_code=1)

    assert not owner_cleanup_event.is_set()
    assert not ray_cleanup_event.is_set()


def test_parent_cleanup_orders_cluster_models_before_ray_and_local_fallback(monkeypatch):
    events = []
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner-1"
    path.ray_head_port = 33001

    monkeypatch.setattr(path_module.ray, "is_initialized", lambda: False)
    monkeypatch.setattr(
        path_module.socket,
        "create_connection",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        path_module.ray,
        "init",
        lambda **kwargs: events.append(("ray_init", kwargs)),
    )
    monkeypatch.setattr(path_module.ray, "shutdown", lambda: events.append(("ray_shutdown", {})))
    monkeypatch.setattr(
        path_module,
        "stop_llm_owner_processes_on_cluster",
        lambda owner_id: events.append(("cluster_models", owner_id)),
    )
    monkeypatch.setattr(
        path_module,
        "stop_ray_runtime",
        lambda **kwargs: (
            events.append(("ray_stop", kwargs))
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(
        path_module,
        "stop_llm_owner_processes_locally",
        lambda owner_id: events.append(("local_models", owner_id)),
    )

    path._stop_local_ray_best_effort()

    assert [event[0] for event in events] == [
        "ray_init",
        "cluster_models",
        "ray_shutdown",
        "ray_stop",
        "local_models",
    ]
    assert events[0][1]["address"] == "127.0.0.1:33001"
    assert events[1][1] == events[4][1] == "owner-1"


def test_parent_cleanup_skips_ray_init_after_scheduler_stopped_gcs(monkeypatch):
    events = []
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner-1"
    path.ray_head_port = 33001
    path._scheduler_owner_cleanup_complete_event = threading.Event()
    path._scheduler_owner_cleanup_complete_event.set()
    path._scheduler_ray_cleanup_complete_event = threading.Event()

    monkeypatch.setattr(path_module.ray, "is_initialized", lambda: False)

    def unavailable(*_args, **_kwargs):
        raise ConnectionRefusedError

    monkeypatch.setattr(path_module.socket, "create_connection", unavailable)
    monkeypatch.setattr(
        path_module.ray,
        "init",
        lambda **_kwargs: pytest.fail("ray.init must not run after GCS shutdown"),
    )
    monkeypatch.setattr(
        path_module,
        "stop_llm_owner_processes_on_cluster",
        lambda _owner_id: pytest.fail("cluster cleanup requires a live GCS"),
    )
    monkeypatch.setattr(
        path_module,
        "stop_ray_runtime",
        lambda **kwargs: (
            events.append(("ray_stop", kwargs))
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr(
        path_module,
        "stop_llm_owner_processes_locally",
        lambda owner_id: events.append(("local_models", owner_id)),
    )

    path._stop_local_ray_best_effort()

    assert events == [
        ("ray_stop", {"force": True}),
        ("local_models", "owner-1"),
    ]


def test_parent_local_model_fallback_runs_when_ray_stop_fails(monkeypatch):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner-1"
    path._stop_owned_llm_processes_via_ray_best_effort = lambda: None
    local_cleanups = []

    def fail_ray_stop(**_kwargs):
        raise path_module.subprocess.TimeoutExpired("ray stop", 1)

    monkeypatch.setattr(path_module, "stop_ray_runtime", fail_ray_stop)
    monkeypatch.setattr(
        path_module,
        "stop_llm_owner_processes_locally",
        lambda owner_id: local_cleanups.append(owner_id),
    )

    path._stop_local_ray_best_effort()

    assert local_cleanups == ["owner-1"]


def test_ray_head_start_retries_once_after_stale_runtime_cleanup(monkeypatch):
    calls = []
    start_attempts = 0

    def run(command, **kwargs):
        nonlocal start_attempts
        calls.append((list(command), kwargs))
        if command[1] == "start":
            start_attempts += 1
            if start_attempts == 1:
                raise scheduler_module.subprocess.CalledProcessError(
                    1,
                    command,
                    stderr="Ray is already running",
                )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scheduler_module.subprocess, "run", run)
    scheduler = object.__new__(Scheduler)
    scheduler.ray_head_port = 33001

    scheduler._launch_ray_head()

    assert [command[1] for command, _kwargs in calls] == ["start", "stop", "start"]
    assert calls[1][0][-1] == "--force"


def test_static_retry_snapshot_clears_old_active_attempt_identity():
    run = StaticRun("run", "workflow", _workflow())
    run.mark_task_started("task", {
        "node_id": "worker-old",
        "node_ip": "10.0.0.2",
        "gpu_id": None,
        "attempt": 1,
        "dispatch_id": "dispatch-old",
        "lease_id": "lease-old",
        "schedule_decision": {
            "lease_id": "lease-old",
            "selected_node": {"node_id": "worker-old"},
        },
    })
    run.append_event({
        "type": "task_retry",
        "data": {
            "attempt": 1,
            "dispatch_id": "dispatch-old",
            "lease_id": "lease-old",
        },
    })

    run.mark_task_retry("task", {"error_type": "node_lost"}, attempt=1)

    task = run.snapshot()["task_nodes"]["task"]
    assert task["status"] == "queued"
    assert task["attempt"] == 1
    assert task["dispatch_id"] is None
    assert task["lease_id"] is None
    assert task["selected_node"] is None
    assert task["schedule_decision"] is None
    assert task["started_time"] is None
    assert run.get_events()[0]["data"] == {
        "attempt": 1,
        "dispatch_id": "dispatch-old",
        "lease_id": "lease-old",
        "run_id": "run",
        "workflow_id": "workflow",
        "run_status": "running",
    }
