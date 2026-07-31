import asyncio
import json
import multiprocessing as mp
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from maze.core.path import path as path_module
from maze.core.path.path import MaPath, SchedulerUnavailableError


class _Process:
    def __init__(self):
        self.alive = True
        self.pid = 4321
        self.exitcode = None

    def is_alive(self):
        return self.alive


def _signal_fatal_and_hang(fatal_event):
    fatal_event.set()
    while True:
        time.sleep(1)


class _StaticRun:
    run_id = "run"
    workflow_id = "workflow"

    def __init__(self):
        self.status = "running"
        self.event_seq = 0

    def mark_interrupted(self, _reason):
        if self.status == "interrupted":
            return False
        self.status = "interrupted"
        return True

    def append_event(self, event):
        self.event_seq += 1
        return {**event, "seq": self.event_seq}

    def snapshot(self):
        return {"run_id": self.run_id, "status": self.status}


class _StaticStore:
    def __init__(self):
        self.events = []
        self.snapshots = []

    def append_event(self, run_id, event):
        self.events.append((run_id, event))

    def load_events(self, run_id, after=None):
        return [
            event
            for stored_run_id, event in self.events
            if stored_run_id == run_id
            and (after is None or int(event.get("seq", 0)) > after)
        ]

    def save_run(self, snapshot):
        self.snapshots.append(snapshot)


def _path(process):
    path = object.__new__(MaPath)
    path.scheduler_process = process
    path._scheduler_failure_handled = None
    path._scheduler_exit_progress = None
    path.static_runs = {}
    path.dynamic_runs = {}
    path.lock = asyncio.Lock()
    path.async_que = {}
    path.langgraph_task_requests = {}
    path.llm_instance_async_que = {}
    path.cluster_resource_requests = {}
    path.cluster_queue_requests = {}
    path.worker_registration_requests = {}
    path.task_attempts = {}
    path._stop_local_ray_best_effort = lambda: True
    return path


@pytest.mark.asyncio
async def test_langgraph_waiter_success_path_still_clears_its_queue():
    process = _Process()
    path = _path(process)

    class Task:
        def set_args(self, _args):
            pass

        def set_kwargs(self, _kwargs):
            pass

        def to_json(self):
            return {"workflow_id": "workflow", "task_id": "task"}

    path.workflows = {
        "workflow": SimpleNamespace(get_task=lambda _task_id: Task()),
    }

    def respond(_message):
        path.langgraph_task_requests["task"].put_nowait({
            "type": "finish_task",
            "data": {"result": {"status": "ok"}},
        })

    path._send_scheduler_message = respond

    result = await path.run_langgraph_task(
        "workflow",
        "task",
        "args",
        "kwargs",
    )

    assert result == {"status": "ok"}
    assert path.langgraph_task_requests == {}


@pytest.mark.asyncio
async def test_scheduler_exit_notifies_all_inflight_waiters_and_clears_queues():
    process = _Process()
    path = _path(process)
    sent = []
    all_sent = asyncio.Event()

    class Task:
        def set_args(self, _args):
            pass

        def set_kwargs(self, _kwargs):
            pass

        def to_json(self):
            return {"workflow_id": "workflow", "task_id": "task"}

    path.workflows = {
        "workflow": SimpleNamespace(get_task=lambda _task_id: Task()),
    }

    def send(message):
        sent.append(message)
        if len(sent) == 3:
            all_sent.set()

    path._send_scheduler_message = send
    waiters = [
        asyncio.create_task(
            path.run_langgraph_task("workflow", "task", "args", "kwargs")
        ),
        asyncio.create_task(
            path.start_llm_instance("instance", "model", 1, 1, 1024, 0)
        ),
        asyncio.create_task(path.stop_llm_instance("instance")),
    ]

    await asyncio.wait_for(all_sent.wait(), timeout=0.5)
    process.alive = False
    process.exitcode = 17
    await path._handle_scheduler_exit()

    results = await asyncio.wait_for(
        asyncio.gather(*waiters, return_exceptions=True),
        timeout=0.5,
    )
    assert all(isinstance(result, SchedulerUnavailableError) for result in results)
    assert all(result.detail()["code"] == "scheduler_unavailable" for result in results)
    assert all(result.detail()["scheduler_exitcode"] == 17 for result in results)
    assert path.langgraph_task_requests == {}
    assert path.llm_instance_async_que == {}


@pytest.mark.asyncio
async def test_fatal_signal_notifies_existing_waiters_before_scheduler_exits():
    process = _Process()
    path = _path(process)
    path._scheduler_fatal_event = threading.Event()
    all_sent = asyncio.Event()
    sent = []

    class Task:
        def set_args(self, _args):
            pass

        def set_kwargs(self, _kwargs):
            pass

        def to_json(self):
            return {"workflow_id": "workflow", "task_id": "task"}

    path.workflows = {
        "workflow": SimpleNamespace(get_task=lambda _task_id: Task()),
    }

    def send(message):
        sent.append(message)
        if len(sent) == 6:
            all_sent.set()

    path._send_scheduler_message = send
    waiters = [
        asyncio.create_task(
            path.run_langgraph_task(
                "workflow",
                "task",
                "args",
                "kwargs",
                timeout=0.5,
            )
        ),
        asyncio.create_task(
            path.start_llm_instance(
                "instance",
                "model",
                1,
                1,
                1024,
                0,
                timeout=0.5,
            )
        ),
        asyncio.create_task(path.stop_llm_instance("instance", timeout=0.5)),
        asyncio.create_task(path.get_cluster_resources(timeout=0.5)),
        asyncio.create_task(path.get_cluster_queues(timeout=0.5)),
        asyncio.create_task(
            path.start_worker(
                "127.0.0.2",
                "worker",
                {"cpu": 1, "cpu_mem": 1, "gpu_resource": {}},
                timeout=0.5,
            )
        ),
    ]

    await asyncio.wait_for(all_sent.wait(), timeout=0.5)
    path._scheduler_fatal_event.set()
    await path._handle_scheduler_exit()

    results = await asyncio.wait_for(
        asyncio.gather(*waiters, return_exceptions=True),
        timeout=0.5,
    )
    assert all(isinstance(result, SchedulerUnavailableError) for result in results)
    assert all(result.detail()["scheduler_exitcode"] is None for result in results)
    assert path.langgraph_task_requests == {}
    assert path.llm_instance_async_que == {}
    assert path.cluster_resource_requests == {}
    assert path.cluster_queue_requests == {}
    assert path.worker_registration_requests == {}


@pytest.mark.asyncio
async def test_fatal_child_is_terminated_after_grace_and_interrupts_run_waiter():
    context = mp.get_context("fork")
    fatal_event = context.Event()
    process = context.Process(target=_signal_fatal_and_hang, args=(fatal_event,))
    maintenance = None
    process.start()
    try:
        assert await asyncio.to_thread(fatal_event.wait, 1)
        path = _path(process)
        path._cleanup_started = False
        path._scheduler_fatal_event = fatal_event
        path._scheduler_fatal_exit_grace_seconds = 0.05
        path._scheduler_fatal_terminate_timeout_seconds = 0.5
        path._scheduler_fatal_kill_timeout_seconds = 0.5
        static_run = _StaticRun()
        path.static_runs = {static_run.run_id: static_run}
        path.static_run_store = _StaticStore()
        path.global_metrics = SimpleNamespace(
            on_run_status_change=lambda *_args: None,
        )
        path.async_que = {static_run.run_id: asyncio.Queue()}
        path._sweep_run_deadlines = lambda: asyncio.sleep(0)

        maintenance = asyncio.create_task(
            path.maintenance_coroutine(interval_seconds=0.1)
        )
        event = await asyncio.wait_for(
            path.async_que[static_run.run_id].get(),
            timeout=2,
        )

        assert event["type"] == "interrupt_workflow"
        assert static_run.status == "interrupted"
        assert not process.is_alive()
        assert process.exitcode not in (None, 0)
    finally:
        if maintenance is not None:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)


@pytest.mark.asyncio
async def test_fatal_child_escalates_to_kill_when_terminate_is_ignored():
    class Process:
        pid = 4321
        exitcode = None

        def __init__(self):
            self.alive = True
            self.terminate_calls = 0
            self.kill_calls = 0

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminate_calls += 1

        def kill(self):
            self.kill_calls += 1
            self.alive = False
            self.exitcode = -9

        def join(self, timeout=None):
            return None

    process = Process()
    path = _path(process)
    path._scheduler_fatal_event = threading.Event()
    path._scheduler_fatal_event.set()
    path._scheduler_fatal_exit_grace_seconds = 0
    path._scheduler_fatal_terminate_timeout_seconds = 0
    path._scheduler_fatal_kill_timeout_seconds = 0

    await path._handle_scheduler_exit()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert path._scheduler_failure_handled == (4321, -9)


@pytest.mark.asyncio
async def test_fatal_child_that_exits_during_grace_is_not_terminated():
    class Process:
        pid = 4321
        exitcode = None

        def __init__(self):
            self.alive = True
            self.terminate_calls = 0

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminate_calls += 1

    process = Process()
    path = _path(process)
    path._scheduler_fatal_event = threading.Event()
    path._scheduler_fatal_event.set()
    path._scheduler_fatal_exit_grace_seconds = 1

    await path._handle_scheduler_exit()
    process.alive = False
    process.exitcode = 1
    await path._handle_scheduler_exit()

    assert process.terminate_calls == 0
    assert path._scheduler_failure_handled == (4321, 1)


@pytest.mark.asyncio
async def test_fatal_signal_wins_over_a_late_success_response():
    process = _Process()
    path = _path(process)
    path._scheduler_fatal_event = threading.Event()
    path._scheduler_fatal_event.set()
    response_queue = asyncio.Queue()
    response_queue.put_nowait({
        "type": "finish_llm_instance_stop",
        "data": {"instance_id": "instance"},
    })

    with pytest.raises(SchedulerUnavailableError, match="fatal failure"):
        await path._wait_for_scheduler_response(
            response_queue,
            timeout=0.5,
            operation="stop the model instance",
        )


@pytest.mark.asyncio
async def test_scheduler_response_deadlines_clear_all_waiter_queues():
    process = _Process()
    path = _path(process)

    class Task:
        def set_args(self, _args):
            pass

        def set_kwargs(self, _kwargs):
            pass

        def to_json(self):
            return {"workflow_id": "workflow", "task_id": "task"}

    path.workflows = {
        "workflow": SimpleNamespace(get_task=lambda _task_id: Task()),
    }
    path._send_scheduler_message = lambda _message: None

    results = await asyncio.gather(
        path.run_langgraph_task(
            "workflow",
            "task",
            "args",
            "kwargs",
            timeout=0.01,
        ),
        path.start_llm_instance(
            "instance",
            "model",
            1,
            1,
            1024,
            0,
            timeout=0.01,
        ),
        path.stop_llm_instance("instance", timeout=0.01),
        return_exceptions=True,
    )

    assert all(isinstance(result, SchedulerUnavailableError) for result in results)
    assert all("Timed out" in str(result) for result in results)
    assert path.langgraph_task_requests == {}
    assert path.llm_instance_async_que == {}


@pytest.mark.asyncio
async def test_monitor_routes_langgraph_exception_to_the_dedicated_waiter():
    process = _Process()
    path = _path(process)

    class Task:
        def set_args(self, _args):
            pass

        def set_kwargs(self, _kwargs):
            pass

        def to_json(self):
            return {"workflow_id": "workflow", "task_id": "task"}

    class Socket:
        def __init__(self):
            self.messages = asyncio.Queue()

        async def recv_multipart(self):
            return await self.messages.get()

    path.workflows = {
        "workflow": SimpleNamespace(get_task=lambda _task_id: Task()),
    }
    path.socket_from_scheduler = Socket()
    response = {
        "type": "task_exception",
        "data": {
            "workflow_id": "workflow",
            "task_id": "task",
            "attempt": 1,
            "dispatch_id": "dispatch",
            "lease_id": "lease",
            "result": {"status": "failed"},
        },
    }

    def send(_message):
        path.socket_from_scheduler.messages.put_nowait(
            [b"scheduler", json.dumps(response).encode("utf-8")]
        )

    path._send_scheduler_message = send
    monitor = asyncio.create_task(path.monitor_coroutine())
    try:
        result = await asyncio.wait_for(
            path.run_langgraph_task(
                "workflow",
                "task",
                "args",
                "kwargs",
                timeout=0.5,
            ),
            timeout=0.5,
        )
    finally:
        monitor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await monitor

    assert result == {"status": "failed"}
    assert path.langgraph_task_requests == {}
    assert path.async_que == {}


@pytest.mark.asyncio
async def test_maintenance_checks_scheduler_exit_independently_of_monitor():
    path = object.__new__(MaPath)
    path._cleanup_started = False
    called = asyncio.Event()

    async def handle_exit():
        called.set()

    path._handle_scheduler_exit = handle_exit
    path._sweep_run_deadlines = lambda: asyncio.sleep(0)
    maintenance = asyncio.create_task(path.maintenance_coroutine(interval_seconds=0.1))
    try:
        await asyncio.wait_for(called.wait(), timeout=0.5)
    finally:
        maintenance.cancel()
        with pytest.raises(asyncio.CancelledError):
            await maintenance


@pytest.mark.asyncio
async def test_scheduler_exit_retries_cleanup_until_it_reports_success():
    process = _Process()
    process.alive = False
    process.exitcode = 17
    path = _path(process)
    outcomes = iter((False, True))
    path._stop_local_ray_best_effort = lambda: next(outcomes)

    await path._handle_scheduler_exit()

    assert path._scheduler_failure_handled is None
    assert path._scheduler_exit_progress["ray_cleanup_complete"] is False

    await path._handle_scheduler_exit()

    assert path._scheduler_failure_handled == (4321, 17)
    assert path._scheduler_exit_progress is None


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_ray_cleanup_failure_is_reported(monkeypatch, failure):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner"
    path._stop_owned_llm_processes_via_ray_best_effort = lambda: True
    path._stop_owned_llm_processes_locally_best_effort = lambda: True

    if failure == "nonzero":
        result = SimpleNamespace(returncode=1, stdout="", stderr="failed")
        monkeypatch.setattr(path_module, "stop_ray_runtime", lambda **_kwargs: result)
    else:
        def timeout(**_kwargs):
            raise subprocess.TimeoutExpired("ray stop", 1)

        monkeypatch.setattr(path_module, "stop_ray_runtime", timeout)

    assert path._stop_local_ray_best_effort() is False


@pytest.mark.parametrize("failed_step", ["cluster_owner", "local_owner"])
def test_owner_cleanup_failure_is_reported(monkeypatch, failed_step):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner"
    path._stop_owned_llm_processes_via_ray_best_effort = (
        lambda: failed_step != "cluster_owner"
    )
    path._stop_owned_llm_processes_locally_best_effort = (
        lambda: failed_step != "local_owner"
    )
    ray_stops = []

    def stop_ray(**_kwargs):
        ray_stops.append(True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(path_module, "stop_ray_runtime", stop_ray)

    assert path._stop_local_ray_best_effort() is False
    assert ray_stops == ([] if failed_step == "cluster_owner" else [True])


def test_cluster_owner_cleanup_exception_is_reported(monkeypatch):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner"
    monkeypatch.setattr(path_module.ray, "is_initialized", lambda: True)

    def fail(_owner_id):
        raise RuntimeError("cluster cleanup failed")

    monkeypatch.setattr(path_module, "stop_llm_owner_processes_on_cluster", fail)

    assert path._stop_owned_llm_processes_via_ray_best_effort() is False


def test_local_owner_cleanup_exception_is_reported(monkeypatch):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner"

    def fail(_owner_id):
        raise RuntimeError("local cleanup failed")

    monkeypatch.setattr(path_module, "stop_llm_owner_processes_locally", fail)

    assert path._stop_owned_llm_processes_locally_best_effort() is False


def test_cleanup_success_is_reported(monkeypatch):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner"
    path._stop_owned_llm_processes_via_ray_best_effort = lambda: True
    path._stop_owned_llm_processes_locally_best_effort = lambda: True
    monkeypatch.setattr(
        path_module,
        "stop_ray_runtime",
        lambda **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert path._stop_local_ray_best_effort() is True


def test_unverified_cluster_cleanup_is_not_reported_as_success(monkeypatch):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner"
    path._stop_owned_llm_processes_via_ray_best_effort = lambda: None
    path._stop_owned_llm_processes_locally_best_effort = lambda: True
    monkeypatch.setattr(
        path_module,
        "stop_ray_runtime",
        lambda **_kwargs: pytest.fail("Ray must remain available without owner proof"),
    )

    assert path._stop_local_ray_best_effort() is False


def test_cleanup_step_proofs_survive_a_later_local_retry(monkeypatch):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner"
    path._scheduler_owner_cleanup_complete_event = threading.Event()
    path._scheduler_ray_cleanup_complete_event = threading.Event()
    cluster_calls = []
    ray_calls = []
    local_outcomes = iter((False, True))
    path._stop_owned_llm_processes_via_ray_best_effort = lambda: (
        cluster_calls.append(True) or True
    )
    path._stop_owned_llm_processes_locally_best_effort = lambda: next(local_outcomes)
    monkeypatch.setattr(
        path_module,
        "stop_ray_runtime",
        lambda **_kwargs: (
            ray_calls.append(True)
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    assert path._stop_local_ray_best_effort() is False
    assert path._stop_local_ray_best_effort() is True
    assert cluster_calls == [True]
    assert ray_calls == [True]


def test_cleanup_result_remains_retriable_until_complete():
    path = object.__new__(MaPath)
    path._cleanup_started = False
    path._cleanup_complete = False
    path.scheduler_process = None
    path._send_scheduler_message = lambda _message: None
    path._close_scheduler_channels = lambda: None
    outcomes = iter((False, True))
    cleanup_calls = []
    path._stop_local_ray_best_effort = lambda: (
        cleanup_calls.append(True) or next(outcomes)
    )

    assert path.cleanup() is False
    assert path._cleanup_started is False
    assert path.cleanup() is True
    assert path.cleanup() is True
    assert cleanup_calls == [True, True]
