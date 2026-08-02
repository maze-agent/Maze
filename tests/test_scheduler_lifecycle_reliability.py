import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from maze.core.scheduler import scheduler as scheduler_module
from maze.core.scheduler.llm_instance import LlmInstanceMessage
from maze.core.scheduler.scheduler import Scheduler


class _StopLoop(Exception):
    pass


class _CaptureSocket:
    def __init__(self, events=None):
        self.events = events
        self.messages = []

    def connect(self, _address):
        return None

    def send(self, payload):
        message = json.loads(payload.decode("utf-8"))
        self.messages.append(message)
        if self.events is not None:
            self.events.append(("send", message["type"]))


class _Context:
    def __init__(self, socket):
        self.socket_instance = socket

    def socket(self, _socket_type):
        return self.socket_instance


class _OneMessageQueue:
    def __init__(self, message, done):
        self.message = message
        self.done = done
        self.read = False

    def get(self, timeout=None):
        if self.read and self.done():
            raise _StopLoop()
        if not self.read:
            self.read = True
            return self.message
        raise queue.Empty()


class _Selection:
    def __init__(self, selected_node, lease_id, reason="selected"):
        self.selected_node = selected_node
        self.lease_id = lease_id
        self.decision = {"selected": selected_node is not None, "reason": reason}

    def __bool__(self):
        return self.selected_node is not None


class _RecordingEvent:
    def __init__(self, events, name):
        self.events = events
        self.name = name
        self.set_value = False

    def set(self):
        self.set_value = True
        self.events.append(self.name)

    def is_set(self):
        return self.set_value


def _llm_message(**overrides):
    data = {
        "instance_id": "instance-1",
        "model": "model-1",
        "backend": "vllm",
        "backend_args": {"max_model_len": 4096},
        "cpu_nums": 1,
        "memory": 1024,
        "gpu_nums": 1,
        "gpu_mem": 2048,
    }
    data.update(overrides)
    return LlmInstanceMessage("start_llm_instance", data)


def _bare_scheduler(socket=None):
    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.Lock()
    scheduler.port2 = 12345
    scheduler.owner_node_sender = None
    scheduler.last_llm_scaling_check = 0.0
    socket = socket or _CaptureSocket()
    scheduler.context = _Context(socket)
    return scheduler, socket


def _run_one_llm_message(scheduler, message):
    socket = scheduler.context.socket_instance
    scheduler.llm_instance_queue = _OneMessageQueue(
        message,
        done=lambda: bool(socket.messages),
    )
    try:
        with pytest.raises(_StopLoop):
            scheduler._llm_instance_thread(scheduler.port2)
    finally:
        scheduler._shutdown_llm_executors()


def _wait_for_maintenance(scheduler, predicate, *, now=10.0, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        scheduler._manage_llm_instance_scaling(now=now)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for LLM maintenance")


def test_scheduler_constructor_passes_owner_to_llm_manager():
    scheduler = Scheduler(
        12001,
        12002,
        12003,
        SimpleNamespace(put=lambda _message: None),
        owner_id="owner-1",
    )

    assert scheduler.owner_id == "owner-1"
    assert scheduler.llm_instance_manager.owner_id == "owner-1"
    assert scheduler.task_queues.queue_names() == ("gpu", "cpu", "io")


def test_llm_start_validates_then_reserves_and_passes_lease(monkeypatch):
    events = []
    socket = _CaptureSocket(events)
    scheduler, _ = _bare_scheduler(socket)
    selected_node = SimpleNamespace(node_id="node-1", node_ip="10.0.0.2", gpu_id=0)
    selection = _Selection(selected_node, "lease-1")

    class ResourceManager:
        def select_node(self, **kwargs):
            events.append(("select", kwargs))
            return selection

    class LlmManager:
        def record_owner_node(self, node_id, node_ip):
            events.append(("owner", node_id, node_ip))
            return True

        def start_llm_instance(self, **kwargs):
            events.append(("start", kwargs))
            return {
                "instance_id": kwargs["instance_id"],
                "model": kwargs["model"],
                "backend": kwargs["backend"],
                "host": kwargs["node_ip"],
                "port": "8000",
                "endpoint": "http://10.0.0.2:8000/v1",
                "status": "ready",
            }

    scheduler.resource_manager = ResourceManager()
    scheduler.llm_instance_manager = LlmManager()
    scheduler.owner_node_sender = SimpleNamespace(
        send=lambda placement: events.append(("owner_receipt", placement))
    )

    _run_one_llm_message(scheduler, _llm_message(request_id="request-1"))

    assert [event[0] for event in events] == [
        "select",
        "owner",
        "owner_receipt",
        "start",
        "send",
    ]
    selection_kwargs = events[0][1]
    assert selection_kwargs["reservation_kind"] == "instance"
    assert selection_kwargs["run_id"] == "instance-1"
    start_kwargs = events[3][1]
    assert start_kwargs["lease_id"] == "lease-1"
    assert start_kwargs["backend_args"] == {"max_model_len": 4096}
    assert start_kwargs["return_info"] is True
    assert socket.messages == [{
        "type": "finish_llm_instance_launch",
        "data": {
            "instance_id": "instance-1",
            "model": "model-1",
            "backend": "vllm",
            "host": "10.0.0.2",
            "port": "8000",
            "endpoint": "http://10.0.0.2:8000/v1",
            "status": "ready",
            "request_id": "request-1",
        },
    }]


def test_invalid_backend_fails_before_resource_selection():
    scheduler, socket = _bare_scheduler()
    scheduler.resource_manager = SimpleNamespace(
        select_node=lambda **_kwargs: pytest.fail(
            "invalid backend must fail before reserving resources"
        )
    )
    scheduler.llm_instance_manager = SimpleNamespace()

    _run_one_llm_message(scheduler, _llm_message(backend="unsupported"))

    assert socket.messages[0]["type"] == "fail_llm_instance_launch"
    assert "Unsupported model backend" in socket.messages[0]["data"]["error"]


def test_resource_shortage_fails_once_without_requeue():
    scheduler, socket = _bare_scheduler()
    selection_calls = []
    scheduler.resource_manager = SimpleNamespace(
        select_node=lambda **kwargs: (
            selection_calls.append(kwargs)
            or _Selection(None, None, "insufficient_gpu")
        )
    )
    scheduler.llm_instance_manager = SimpleNamespace()

    _run_one_llm_message(scheduler, _llm_message())

    assert len(selection_calls) == 1
    assert socket.messages == [{
        "type": "fail_llm_instance_launch",
        "data": {
            "instance_id": "instance-1",
            "backend": "vllm",
            "request_id": None,
            "error": "insufficient_gpu",
        },
    }]


def test_failed_launch_retains_lease_until_cleanup_is_confirmed():
    scheduler, socket = _bare_scheduler()
    selected_node = SimpleNamespace(node_id="node-1", node_ip="10.0.0.2", gpu_id=0)
    scheduler.resource_manager = SimpleNamespace(
        select_node=lambda **_kwargs: _Selection(selected_node, "lease-1"),
        release_lease=lambda _lease_id: pytest.fail(
            "cleanup_pending launch must retain its lease"
        ),
        release_instance_resource=lambda _detail: pytest.fail(
            "cleanup_pending launch must retain its resources"
        ),
    )

    class LlmManager:
        def record_owner_node(self, _node_id, _node_ip):
            return True

        def start_llm_instance(self, **_kwargs):
            raise RuntimeError("startup failed; cleanup is pending")

        def get_instance_state(self, _instance_id):
            return "cleanup_pending"

    scheduler.llm_instance_manager = LlmManager()

    _run_one_llm_message(scheduler, _llm_message())

    assert socket.messages[0]["type"] == "fail_llm_instance_launch"
    assert "cleanup is pending" in socket.messages[0]["data"]["error"]


def test_failed_launch_releases_lease_after_cleanup_is_confirmed():
    events = []
    socket = _CaptureSocket(events)
    scheduler, _ = _bare_scheduler(socket)
    selected_node = SimpleNamespace(node_id="node-1", node_ip="10.0.0.2", gpu_id=0)
    scheduler.resource_manager = SimpleNamespace(
        select_node=lambda **_kwargs: _Selection(selected_node, "lease-1"),
        release_instance_resource=lambda detail: events.append(
            ("release", detail["lease_id"])
        ),
    )

    class LlmManager:
        def record_owner_node(self, _node_id, _node_ip):
            return True

        def start_llm_instance(self, **_kwargs):
            raise TimeoutError("actor launch timed out")

        def get_instance_state(self, _instance_id):
            return "stopped"

        def get_instance_resource_detail(self, _instance_id):
            return {"lease_id": "lease-1", "backend": "vllm"}

        def finalize_stopped_instance(self, instance_id):
            events.append(("finalize", instance_id))

    scheduler.llm_instance_manager = LlmManager()

    _run_one_llm_message(scheduler, _llm_message())

    assert [event[0] for event in events] == ["release", "finalize", "send"]
    assert socket.messages[0]["type"] == "fail_llm_instance_launch"
    assert "actor launch timed out" in socket.messages[0]["data"]["error"]


def test_stop_ack_follows_stop_release_and_finalize_order():
    events = []
    socket = _CaptureSocket(events)
    scheduler, _ = _bare_scheduler(socket)

    class LlmManager:
        def stop_llm_instance(self, **kwargs):
            events.append(("stop", kwargs))
            return {"backend": "vllm", "lease_id": "lease-1"}

        def finalize_stopped_instance(self, instance_id):
            events.append(("finalize", instance_id))

    scheduler.llm_instance_manager = LlmManager()
    scheduler.resource_manager = SimpleNamespace(
        release_instance_resource=lambda detail: events.append(("release", detail))
    )

    scheduler._handle_llm_instance_stop(
        socket,
        {"instance_id": "instance-1", "request_id": "request-1"},
    )

    assert [event[0] for event in events] == [
        "stop",
        "release",
        "finalize",
        "send",
    ]
    assert events[0][1] == {"instance_id": "instance-1", "finalize": False}
    assert socket.messages[0]["type"] == "finish_llm_instance_stop"


def test_scaling_keeps_ready_instances_until_explicit_stop():
    events = []
    scheduler, _ = _bare_scheduler()

    class LlmManager:
        def lru_scale_in_candidates(self, now=None):
            events.append(("lru", now))
            return [{"instance_id": "instance-1"}]

        def claim_lru_scale_in(self, instance_id):
            events.append(("claim", instance_id))
            return True

        def stop_llm_instance(self, **kwargs):
            acquired = scheduler.lock.acquire(blocking=False)
            events.append(("stop", acquired, kwargs))
            if acquired:
                scheduler.lock.release()
            return {"backend": "vllm", "lease_id": "lease-1"}

        def finalize_stopped_instance(self, instance_id):
            events.append(("finalize", instance_id))

        def scale_out_recommendations(self):
            return []

    scheduler.llm_instance_manager = LlmManager()
    scheduler.resource_manager = SimpleNamespace(
        release_instance_resource=lambda detail: events.append(("release", detail))
    )

    scheduler._manage_llm_instance_scaling(now=10.0)

    assert events == []


def test_model_routed_task_does_not_reserve_the_model_gpu_twice():
    scheduler = object.__new__(Scheduler)
    task = SimpleNamespace(
        scheduler_resources={"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 2048},
        model_route={"endpoint": "http://model/v1"},
    )

    execution_resources = scheduler._task_execution_resources(task)

    assert execution_resources == {
        "cpu": 1,
        "cpu_mem": 0,
        "gpu": 0,
        "gpu_mem": 0,
    }
    assert task.scheduler_resources["gpu"] == 1
    assert task.scheduler_resources["gpu_mem"] == 2048


def test_runtime_failure_cleanup_releases_only_after_stop_confirmation():
    events = []
    scheduler, _ = _bare_scheduler()

    class LlmManager:
        def __init__(self):
            self.cleanup_attempts = 0

        def runtime_cleanup_candidates(self):
            return [{
                "instance_id": "instance-1",
                "state": "cleanup_pending" if self.cleanup_attempts else "unhealthy",
                "reason": "model process exited",
            }]

        def stop_llm_instance(self, **kwargs):
            self.cleanup_attempts += 1
            events.append(("stop", self.cleanup_attempts, kwargs))
            if self.cleanup_attempts == 1:
                raise RuntimeError("cleanup unavailable")
            return {"backend": "vllm", "lease_id": "lease-1"}

        def finalize_stopped_instance(self, instance_id):
            events.append(("finalize", instance_id))

        def lru_scale_in_candidates(self, now=None):
            return []

        def scale_out_recommendations(self):
            return []

    scheduler.llm_instance_manager = LlmManager()
    scheduler.resource_manager = SimpleNamespace(
        release_instance_resource=lambda detail: events.append(
            ("release", detail["lease_id"])
        )
    )

    scheduler._manage_llm_instance_scaling(now=10.0)
    _wait_for_maintenance(
        scheduler,
        lambda: scheduler.llm_instance_manager.cleanup_attempts == 1,
        now=10.0,
    )

    assert events == [
        ("stop", 1, {"instance_id": "instance-1", "finalize": False}),
    ]

    scheduler._manage_llm_instance_scaling(now=20.0)
    _wait_for_maintenance(
        scheduler,
        lambda: any(event[0] == "finalize" for event in events),
        now=20.0,
    )

    assert events == [
        ("stop", 1, {"instance_id": "instance-1", "finalize": False}),
        ("stop", 2, {"instance_id": "instance-1", "finalize": False}),
        ("release", "lease-1"),
        ("finalize", "instance-1"),
    ]


def test_slow_runtime_probe_does_not_block_supervisor_hot_path():
    scheduler, _ = _bare_scheduler()
    probe_started = threading.Event()
    release_probe = threading.Event()
    timeout_checked = threading.Event()

    class LlmManager:
        def runtime_cleanup_candidates(self):
            probe_started.set()
            assert release_probe.wait(2)
            return []

        def lru_scale_in_candidates(self, now=None):
            return []

        def scale_out_recommendations(self):
            return []

    scheduler.llm_instance_manager = LlmManager()
    scheduler._fail_timed_out_tasks = lambda _socket: timeout_checked.set()

    started_at = time.monotonic()
    scheduler._manage_llm_instance_scaling(now=10.0)
    assert time.monotonic() - started_at < 0.1
    assert probe_started.wait(1)

    scheduler._fail_timed_out_tasks(None)
    assert timeout_checked.is_set()
    assert not release_probe.is_set()

    release_probe.set()
    _wait_for_maintenance(
        scheduler,
        lambda: scheduler._llm_runtime_probe_future is None,
        now=10.0,
    )
    scheduler._shutdown_llm_executors()


def test_pending_start_can_be_cancelled_without_blocking_control_thread():
    scheduler, socket = _bare_scheduler()
    start_entered = threading.Event()
    cancellation_requested = threading.Event()
    released_leases = []
    selected_node = SimpleNamespace(
        node_id="node-1",
        node_ip="10.0.0.2",
        gpu_id=0,
    )
    scheduler.resource_manager = SimpleNamespace(
        select_node=lambda **_kwargs: _Selection(selected_node, "lease-1"),
        release_lease=lambda lease_id: released_leases.append(lease_id),
    )

    class LlmManager:
        def record_owner_node(self, _node_id, _node_ip):
            return False

        def start_llm_instance(self, **_kwargs):
            start_entered.set()
            assert cancellation_requested.wait(2)
            raise RuntimeError("startup was cancelled")

        def request_start_cancellation(self, _instance_id):
            cancellation_requested.set()

        def clear_start_cancellation(self, _instance_id):
            return None

        def get_instance_state(self, _instance_id):
            return None

    scheduler.llm_instance_manager = LlmManager()
    start_message = _llm_message(request_id="start-request").message_data

    scheduler._queue_llm_start(socket, start_message)
    assert start_entered.wait(1)
    scheduler._queue_llm_stop({
        "instance_id": "instance-1",
        "request_id": "stop-request",
        "start_request_id": "start-request",
    })

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(socket.messages) < 2:
        scheduler._drain_llm_control_futures(socket)
        time.sleep(0.01)

    assert [message["type"] for message in socket.messages] == [
        "fail_llm_instance_launch",
        "finish_llm_instance_stop",
    ]
    assert released_leases == ["lease-1"]
    scheduler._shutdown_llm_executors()


def test_stale_start_cancellation_does_not_stop_same_id_instance():
    scheduler, _ = _bare_scheduler()
    stop_calls = []
    scheduler.resource_manager = SimpleNamespace()
    scheduler.llm_instance_manager = SimpleNamespace(
        stop_llm_instance=lambda **kwargs: stop_calls.append(kwargs),
    )
    scheduler._ensure_llm_async_state()
    scheduler._llm_instance_start_request_ids["instance-1"] = "current-start"

    scheduler._queue_llm_stop({
        "instance_id": "instance-1",
        "start_request_id": "stale-start",
    })

    assert stop_calls == []
    assert scheduler._llm_control_stop_futures == {}
    assert scheduler._llm_instance_start_request_ids == {
        "instance-1": "current-start",
    }
    scheduler._shutdown_llm_executors()


def test_fatal_cleanup_orders_confirmed_stops_before_owner_sweep_and_ray(monkeypatch):
    events = []
    scheduler, _ = _bare_scheduler()
    scheduler._process_exit_lock = threading.Lock()
    scheduler.fatal_event = _RecordingEvent(events, "fatal")
    scheduler.owner_cleanup_complete_event = _RecordingEvent(events, "owner_event")
    scheduler.ray_cleanup_complete_event = _RecordingEvent(events, "ray_event")

    class LlmManager:
        def begin_shutdown(self):
            events.append("begin_shutdown")

        def stop_all_llm_instances(self):
            events.append("stop_all")
            return {"instance-1": {"lease_id": "lease-1"}}, {}

        def finalize_stopped_instance(self, instance_id):
            events.append(("finalize", instance_id))

        def stop_owned_llm_processes(self):
            events.append("owner_sweep")

    scheduler.llm_instance_manager = LlmManager()
    scheduler.resource_manager = SimpleNamespace(
        release_instance_resource=lambda detail: events.append(
            ("release", detail["lease_id"])
        )
    )
    monkeypatch.setattr(
        scheduler_module,
        "stop_ray_runtime",
        lambda **kwargs: (
            events.append(("ray_stop", kwargs))
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    def exit_process(code):
        events.append(("exit", code))
        raise SystemExit(code)

    monkeypatch.setattr(scheduler_module.os, "_exit", exit_process)

    with pytest.raises(SystemExit) as exc_info:
        scheduler._cleanup(exit_code=1)

    assert exc_info.value.code == 1
    assert events == [
        "fatal",
        "begin_shutdown",
        "stop_all",
        ("release", "lease-1"),
        ("finalize", "instance-1"),
        "owner_sweep",
        "owner_event",
        ("ray_stop", {"force": True}),
        "ray_event",
        ("exit", 1),
    ]


def test_graceful_cleanup_stops_producers_before_llm_executors(monkeypatch):
    events = []
    scheduler, _ = _bare_scheduler()
    scheduler._process_exit_lock = threading.Lock()
    scheduler.fatal_event = threading.Event()
    scheduler.owner_cleanup_complete_event = threading.Event()
    scheduler.ray_cleanup_complete_event = threading.Event()

    producer_started = threading.Event()

    def producer():
        producer_started.set()
        while not scheduler._shutdown_requested():
            time.sleep(0.001)
        events.append("producer_stopped")

    scheduler.monitor_thread = threading.Thread(
        name="test-supervisor",
        target=producer,
    )
    scheduler.monitor_thread.start()
    assert producer_started.wait(1)

    scheduler._stop_scheduler_event_sender = lambda: events.append("sender_stopped") or True

    def shutdown_executors():
        assert not scheduler.monitor_thread.is_alive()
        events.append("executors_stopped")
        return True

    scheduler._shutdown_llm_executors = shutdown_executors
    scheduler.llm_instance_manager = SimpleNamespace(
        begin_shutdown=lambda: events.append("begin_shutdown"),
        stop_all_llm_instances=lambda: ({}, {}),
        stop_owned_llm_processes=lambda: events.append("owner_sweep"),
    )
    scheduler.resource_manager = SimpleNamespace()
    monkeypatch.setattr(
        scheduler_module,
        "stop_ray_runtime",
        lambda **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        scheduler_module.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(SystemExit(code)),
    )

    with pytest.raises(SystemExit) as exc_info:
        scheduler._cleanup(exit_code=0)

    assert exc_info.value.code == 0
    assert not scheduler.monitor_thread.is_alive()
    assert events == [
        "producer_stopped",
        "sender_stopped",
        "begin_shutdown",
        "executors_stopped",
        "owner_sweep",
    ]
    assert not scheduler.fatal_event.is_set()
    assert scheduler.owner_cleanup_complete_event.is_set()
    assert scheduler.ray_cleanup_complete_event.is_set()


def test_cleanup_releases_only_confirmed_instances_and_preserves_ray(monkeypatch):
    events = []
    scheduler, _ = _bare_scheduler()
    scheduler._process_exit_lock = threading.Lock()
    scheduler.fatal_event = threading.Event()
    scheduler.owner_cleanup_complete_event = threading.Event()
    scheduler.ray_cleanup_complete_event = threading.Event()
    scheduler.llm_instance_manager = SimpleNamespace(
        begin_shutdown=lambda: events.append("begin_shutdown"),
        stop_all_llm_instances=lambda: (
            {"stopped": {"lease_id": "lease-stopped"}},
            {"pending": "cleanup pending"},
        ),
        finalize_stopped_instance=lambda instance_id: events.append(
            ("finalize", instance_id)
        ),
        stop_owned_llm_processes=lambda: events.append("owner_sweep"),
    )
    scheduler.resource_manager = SimpleNamespace(
        release_instance_resource=lambda detail: events.append(
            ("release", detail["lease_id"])
        )
    )
    monkeypatch.setattr(
        scheduler_module,
        "stop_ray_runtime",
        lambda **_kwargs: pytest.fail("Ray must remain for incomplete owner cleanup"),
    )
    monkeypatch.setattr(
        scheduler_module.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(SystemExit(code)),
    )

    with pytest.raises(SystemExit):
        scheduler._cleanup(exit_code=1)

    assert events == [
        "begin_shutdown",
        ("release", "lease-stopped"),
        ("finalize", "stopped"),
        "owner_sweep",
    ]
    assert not scheduler.owner_cleanup_complete_event.is_set()
    assert not scheduler.ray_cleanup_complete_event.is_set()


def test_pre_dispatch_rejection_has_explicit_non_attempt_identity():
    scheduler, socket = _bare_scheduler()

    scheduler._send_task_rejected(
        socket,
        {
            "workflow_id": "run-1",
            "task_id": "task-1",
            "task_kind": "gpu",
            "resources": {"gpu": 1},
        },
        ValueError("invalid task"),
    )

    data = socket.messages[0]["data"]
    assert data["pre_dispatch"] is True
    assert (data["attempt"], data["dispatch_id"], data["lease_id"]) == (
        0,
        None,
        None,
    )


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
