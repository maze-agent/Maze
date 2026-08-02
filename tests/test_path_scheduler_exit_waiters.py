import asyncio
import json
import multiprocessing as mp
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


class _Task:
    def set_args(self, _args):
        pass

    def set_kwargs(self, _kwargs):
        pass

    def to_json(self):
        return {"workflow_id": "workflow", "task_id": "task"}


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


class _SchedulerSocket:
    def __init__(self):
        self.messages = asyncio.Queue()

    async def recv_multipart(self):
        return await self.messages.get()

    def respond(self, message):
        self.messages.put_nowait(
            [b"scheduler", json.dumps(message).encode("utf-8")]
        )


def _signal_fatal_and_hang(fatal_event):
    fatal_event.set()
    while True:
        time.sleep(1)


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
    path.cluster_control_requests = {}
    path.task_attempts = {}
    path._stop_local_ray_best_effort = lambda: True
    path.workflows = {
        "workflow": SimpleNamespace(get_task=lambda _task_id: _Task()),
    }
    return path


@pytest.mark.asyncio
async def test_llm_start_and_stop_route_success_and_failure_by_request_id():
    path = _path(_Process())
    path.socket_from_scheduler = _SchedulerSocket()
    sent = []
    all_sent = asyncio.Event()

    def send(message):
        sent.append(message)
        if len(sent) == 4:
            all_sent.set()

    path._send_scheduler_message = send
    monitor = asyncio.create_task(path.monitor_coroutine())
    waiters = [
        asyncio.create_task(
            path.start_llm_instance(
                "shared-instance", "model-ok", 1, 1, 1024, 0, timeout=1
            )
        ),
        asyncio.create_task(
            path.start_llm_instance(
                "shared-instance", "model-fail", 1, 1, 1024, 0, timeout=1
            )
        ),
        asyncio.create_task(path.stop_llm_instance("stop-ok", timeout=1)),
        asyncio.create_task(path.stop_llm_instance("stop-fail", timeout=1)),
    ]
    try:
        await asyncio.wait_for(all_sent.wait(), timeout=0.5)
        requests = {
            (
                message["type"],
                message["data"].get("model")
                or message["data"].get("instance_id"),
            ): message["data"]["request_id"]
            for message in sent
        }
        assert len(set(requests.values())) == 4
        assert set(path.llm_instance_async_que) == set(requests.values())

        path.socket_from_scheduler.respond({
            "type": "fail_llm_instance_stop",
            "data": {
                "request_id": requests[("stop_llm_instance", "stop-fail")],
                "instance_id": "stop-fail",
                "error": "stop failed",
            },
        })
        path.socket_from_scheduler.respond({
            "type": "finish_llm_instance_launch",
            "data": {
                "request_id": requests[("start_llm_instance", "model-ok")],
                "instance_id": "shared-instance",
                "backend": "vllm",
                "host": "127.0.0.1",
                "port": "8001",
            },
        })
        path.socket_from_scheduler.respond({
            "type": "fail_llm_instance_launch",
            "data": {
                "request_id": requests[("start_llm_instance", "model-fail")],
                "instance_id": "shared-instance",
                "error": "launch failed",
            },
        })
        path.socket_from_scheduler.respond({
            "type": "finish_llm_instance_stop",
            "data": {
                "request_id": requests[("stop_llm_instance", "stop-ok")],
                "instance_id": "stop-ok",
                "backend": "vllm",
            },
        })

        results = await asyncio.wait_for(
            asyncio.gather(*waiters, return_exceptions=True),
            timeout=1,
        )
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)

    assert results[0]["host"] == "127.0.0.1"
    assert isinstance(results[1], RuntimeError)
    assert str(results[1]) == "launch failed"
    assert results[2]["instance_id"] == "stop-ok"
    assert isinstance(results[3], RuntimeError)
    assert str(results[3]) == "stop failed"
    assert path.llm_instance_async_que == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["fatal", "exit"])
async def test_scheduler_unavailable_notifies_every_request_waiter(failure_mode):
    process = _Process()
    path = _path(process)
    sent = []
    all_sent = asyncio.Event()

    def send(message):
        sent.append(message)
        if len(sent) == 7:
            all_sent.set()

    path._send_scheduler_message = send
    waiters = [
        asyncio.create_task(
            path.run_langgraph_task(
                "workflow", "task", "args", "kwargs", timeout=1
            )
        ),
        asyncio.create_task(
            path.start_llm_instance(
                "instance", "model", 1, 1, 1024, 0, timeout=1
            )
        ),
        asyncio.create_task(path.stop_llm_instance("instance", timeout=1)),
        asyncio.create_task(path.get_cluster_resources(timeout=1)),
        asyncio.create_task(path.get_cluster_queues(timeout=1)),
        asyncio.create_task(
            path.start_worker(
                "127.0.0.2",
                "worker",
                {"cpu": 1, "cpu_mem": 1, "gpu_resource": {}},
                timeout=1,
            )
        ),
        asyncio.create_task(
            path.set_cluster_node_disabled("worker", True, timeout=1)
        ),
    ]

    await asyncio.wait_for(all_sent.wait(), timeout=0.5)
    request_ids = [
        message["data"]["request_id"]
        for message in sent
        if "request_id" in message["data"]
    ]
    assert len(request_ids) == 6
    assert len(set(request_ids)) == 6

    if failure_mode == "fatal":
        path._scheduler_fatal_event = threading.Event()
        path._scheduler_fatal_event.set()
    else:
        process.alive = False
        process.exitcode = 17
    await path._handle_scheduler_exit()

    results = await asyncio.wait_for(
        asyncio.gather(*waiters, return_exceptions=True),
        timeout=0.5,
    )
    assert all(isinstance(result, SchedulerUnavailableError) for result in results)
    assert all(result.pid == 4321 for result in results)
    expected_exitcode = None if failure_mode == "fatal" else 17
    assert all(result.exitcode == expected_exitcode for result in results)
    assert path.langgraph_task_requests == {}
    assert path.llm_instance_async_que == {}
    assert path.cluster_resource_requests == {}
    assert path.cluster_queue_requests == {}
    assert path.worker_registration_requests == {}
    assert path.cluster_control_requests == {}


@pytest.mark.asyncio
async def test_same_langgraph_task_uses_independent_invocation_runtime_ids():
    path = _path(_Process())
    path.socket_from_scheduler = _SchedulerSocket()
    sent = []
    all_sent = asyncio.Event()

    def send(message):
        sent.append(message)
        if len(sent) == 2:
            all_sent.set()

    path._send_scheduler_message = send
    monitor = asyncio.create_task(path.monitor_coroutine())
    waiters = [
        asyncio.create_task(
            path.run_langgraph_task(
                "workflow", "task", f"args-{index}", "kwargs", timeout=1
            )
        )
        for index in range(2)
    ]

    await asyncio.wait_for(all_sent.wait(), timeout=0.5)
    invocation_ids = [message["data"]["workflow_id"] for message in sent]
    assert len(set(invocation_ids)) == 2
    assert set(path.langgraph_task_requests) == set(invocation_ids)
    assert all(
        message["data"]["template_workflow_id"] == "workflow"
        for message in sent
    )
    assert all(
        message["data"]["invocation_id"] == message["data"]["workflow_id"]
        for message in sent
    )
    assert {message["data"]["args"] for message in sent} == {
        "args-0",
        "args-1",
    }
    assert {message["data"]["kwargs"] for message in sent} == {"kwargs"}

    try:
        for invocation_id in reversed(invocation_ids):
            identity = {
                "workflow_id": invocation_id,
                "task_id": "task",
                "attempt": 1,
                "dispatch_id": f"dispatch-{invocation_id}",
                "lease_id": f"lease-{invocation_id}",
            }
            path.socket_from_scheduler.respond({
                "type": "task_pending",
                "data": {
                    "workflow_id": invocation_id,
                    "task_id": "task",
                    "attempt": 0,
                },
            })
            path.socket_from_scheduler.respond({
                "type": "start_task",
                "data": dict(identity),
            })
            path.socket_from_scheduler.respond({
                "type": "finish_task",
                "data": {
                    **identity,
                    "result": {"invocation_id": invocation_id},
                },
            })

        results = await asyncio.wait_for(asyncio.gather(*waiters), timeout=0.5)
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)

    assert {result["invocation_id"] for result in results} == set(invocation_ids)
    assert path.langgraph_task_requests == {}
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_abandoned_langgraph_invocation_stops_only_its_runtime():
    path = _path(_Process())
    sent = []
    path._send_scheduler_message = sent.append

    with pytest.raises(SchedulerUnavailableError, match="Timed out after"):
        await path.run_langgraph_task(
            "workflow", "task", "args", "kwargs", timeout=0.01
        )

    assert [message["type"] for message in sent] == [
        "run_task",
        "stop_workflow",
    ]
    invocation_id = sent[0]["data"]["workflow_id"]
    assert sent[1]["data"]["workflow_id"] == invocation_id
    assert invocation_id != "workflow"
    assert path.langgraph_task_requests == {}


@pytest.mark.asyncio
async def test_abandoned_llm_start_cancels_only_its_start_request():
    path = _path(_Process())
    sent = []
    path._send_scheduler_message = sent.append

    with pytest.raises(SchedulerUnavailableError, match="Timed out after"):
        await path.start_llm_instance(
            "instance", "model", 1, 1, 1024, 1024, timeout=0.01
        )

    assert [message["type"] for message in sent] == [
        "start_llm_instance",
        "stop_llm_instance",
    ]
    start_request_id = sent[0]["data"]["request_id"]
    assert sent[1]["data"] == {
        "instance_id": "instance",
        "start_request_id": start_request_id,
    }
    assert path.llm_instance_async_que == {}


@pytest.mark.asyncio
async def test_fatal_signal_wins_over_late_scheduler_success():
    path = _path(_Process())
    path._scheduler_fatal_event = threading.Event()
    path._scheduler_fatal_event.set()
    response_queue = asyncio.Queue()
    response_queue.put_nowait({
        "type": "finish_llm_instance_stop",
        "data": {"instance_id": "instance", "request_id": "request"},
    })

    with pytest.raises(SchedulerUnavailableError, match="fatal failure"):
        await path._wait_for_scheduler_response(
            response_queue,
            timeout=0.5,
            operation="stop the model instance",
        )


def test_scheduler_exit_notification_supersedes_full_waiter_and_continues():
    process = _Process()
    process.alive = False
    process.exitcode = 17
    path = _path(process)
    full = asyncio.Queue(maxsize=1)
    full.put_nowait({"type": "late_success", "data": {}})
    later = asyncio.Queue(maxsize=1)
    path.llm_instance_async_que = {"full": full, "later": later}

    path._notify_scheduler_exit_waiters("scheduler failed", process)

    assert full.get_nowait()["type"] == "scheduler_unavailable"
    assert later.get_nowait()["type"] == "scheduler_unavailable"
    assert path.llm_instance_async_que == {}


@pytest.mark.asyncio
async def test_live_fatal_scheduler_is_terminated_and_interrupts_active_run():
    context = mp.get_context("spawn")
    fatal_event = context.Event()
    process = context.Process(target=_signal_fatal_and_hang, args=(fatal_event,))
    maintenance = None
    process.start()
    try:
        assert await asyncio.to_thread(fatal_event.wait, 20)
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
async def test_fatal_scheduler_escalates_to_kill_after_terminate_timeout():
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
async def test_scheduler_exit_retries_cleanup_until_owner_cleanup_is_confirmed():
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


def test_unverified_owner_cleanup_preserves_ray_runtime(monkeypatch):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner"
    path._stop_owned_llm_processes_via_ray_best_effort = lambda: False
    local_cleanup_calls = []
    path._stop_owned_llm_processes_locally_best_effort = lambda: (
        local_cleanup_calls.append(True) or True
    )
    monkeypatch.setattr(
        path_module,
        "stop_ray_runtime",
        lambda **_kwargs: pytest.fail(
            "Ray must remain available until owner cleanup is confirmed"
        ),
    )

    assert path._stop_local_ray_best_effort() is False
    assert local_cleanup_calls == [True]


def test_owner_cleanup_proof_allows_ray_shutdown(monkeypatch):
    path = object.__new__(MaPath)
    path._scheduler_owner_id = "owner"
    path._scheduler_owner_cleanup_complete_event = threading.Event()
    path._scheduler_owner_cleanup_complete_event.set()
    path._scheduler_ray_cleanup_complete_event = threading.Event()
    path._stop_owned_llm_processes_via_ray_best_effort = lambda: pytest.fail(
        "Owner cleanup must not be repeated after the Scheduler proves it"
    )
    path._stop_owned_llm_processes_locally_best_effort = lambda: True
    ray_stops = []
    monkeypatch.setattr(
        path_module,
        "stop_ray_runtime",
        lambda **_kwargs: (
            ray_stops.append(True)
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    assert path._stop_local_ray_best_effort() is True
    assert ray_stops == [True]
    assert path._scheduler_ray_cleanup_complete_event.is_set()
