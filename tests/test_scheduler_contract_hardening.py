import json
import threading
import time

import pytest
import zmq

from maze.core.scheduler.scheduler import (
    SCHEDULER_EVENT_QUEUE_MAXSIZE,
    Scheduler,
    SchedulerMessageSendTimeout,
)


class _SenderContext:
    def __init__(self, socket):
        self.socket_instance = socket

    def socket(self, socket_type):
        assert socket_type == zmq.DEALER
        return self.socket_instance


class _ControlledSocket:
    def __init__(self, *, block_first=False, writable=True):
        self.block_first = block_first
        self.writable = writable
        self.first_send_started = threading.Event()
        self.release_first_send = threading.Event()
        self.sent = []
        self.options = []
        self.closed = False
        self.active_sends = 0
        self.max_active_sends = 0
        self._lock = threading.Lock()

    def setsockopt(self, option, value):
        self.options.append((option, value))

    def connect(self, _address):
        return None

    def poll(self, _timeout_ms, flags):
        assert flags == zmq.POLLOUT
        return zmq.POLLOUT if self.writable else 0

    def send(self, payload, flags=0):
        assert flags == zmq.NOBLOCK
        message = json.loads(payload.decode("utf-8"))
        with self._lock:
            self.active_sends += 1
            self.max_active_sends = max(
                self.max_active_sends,
                self.active_sends,
            )
        try:
            if self.block_first and not self.sent:
                self.first_send_started.set()
                if not self.release_first_send.wait(1):
                    raise AssertionError("test did not release the first send")
            self.sent.append(message)
        finally:
            with self._lock:
                self.active_sends -= 1

    def close(self, linger=0):
        assert linger == 0
        self.closed = True


def _sender_scheduler(socket, *, timeout=0.5):
    scheduler = object.__new__(Scheduler)
    scheduler.context = _SenderContext(socket)
    scheduler.scheduler_message_send_timeout_seconds = timeout
    scheduler._scheduler_event_send_lock = threading.Lock()
    scheduler.sender_failures = []

    def run_sender(_name, target, *args):
        try:
            target(*args)
        except BaseException as exc:
            scheduler.sender_failures.append(exc)

    scheduler._run_critical_thread = run_sender
    scheduler._start_scheduler_event_sender(12345)
    return scheduler


def _send_in_thread(scheduler, message, errors, socket=None):
    try:
        scheduler._send_scheduler_event(socket, message)
    except BaseException as exc:
        errors.append(exc)


def test_single_sender_orders_start_before_finish_from_concurrent_producers():
    socket = _ControlledSocket(block_first=True)
    scheduler = _sender_scheduler(socket)
    errors = []
    start_thread = threading.Thread(
        target=_send_in_thread,
        args=(scheduler, {"type": "start_task"}, errors),
    )
    finish_thread = threading.Thread(
        target=_send_in_thread,
        args=(scheduler, {"type": "finish_task"}, errors),
    )

    start_thread.start()
    assert socket.first_send_started.wait(0.5)
    finish_thread.start()
    time.sleep(0.02)
    assert socket.max_active_sends == 1

    socket.release_first_send.set()
    start_thread.join(1)
    finish_thread.join(1)

    assert not start_thread.is_alive()
    assert not finish_thread.is_alive()
    assert errors == []
    assert [message["type"] for message in socket.sent] == [
        "start_task",
        "finish_task",
    ]
    assert socket.max_active_sends == 1
    assert scheduler._scheduler_event_queue.maxsize == SCHEDULER_EVENT_QUEUE_MAXSIZE
    assert (zmq.SNDHWM, SCHEDULER_EVENT_QUEUE_MAXSIZE) in socket.options
    assert (zmq.IMMEDIATE, 1) in socket.options

    assert scheduler._stop_scheduler_event_sender() is True
    assert not scheduler.scheduler_event_thread.is_alive()
    assert socket.closed is True


def test_sender_backpressure_fails_waiter_and_exits():
    socket = _ControlledSocket(writable=False)
    scheduler = _sender_scheduler(socket, timeout=0.02)

    with pytest.raises(SchedulerMessageSendTimeout):
        scheduler._send_scheduler_event(None, {"type": "start_task"})

    scheduler.scheduler_event_thread.join(1)
    assert not scheduler.scheduler_event_thread.is_alive()
    assert len(scheduler.sender_failures) == 1
    assert isinstance(scheduler.sender_failures[0], SchedulerMessageSendTimeout)
    assert scheduler._scheduler_event_sender_stopped.is_set()
    assert socket.closed is True


def test_direct_send_lock_serializes_shared_test_socket_without_state_lock():
    socket = _ControlledSocket(block_first=True)
    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.Lock()
    scheduler.scheduler_message_send_timeout_seconds = 0.5
    errors = []

    first = threading.Thread(
        target=_send_in_thread,
        args=(scheduler, {"type": "first"}, errors, socket),
    )
    second = threading.Thread(
        target=_send_in_thread,
        args=(scheduler, {"type": "second"}, errors, socket),
    )
    first.start()
    assert socket.first_send_started.wait(0.5)
    second.start()
    time.sleep(0.02)

    assert scheduler.lock.acquire(blocking=False)
    scheduler.lock.release()
    assert socket.max_active_sends == 1
    socket.release_first_send.set()
    first.join(1)
    second.join(1)
    assert errors == []
    assert [message["type"] for message in socket.sent] == ["first", "second"]
    assert socket.max_active_sends == 1


def test_stop_workflow_ack_is_created_only_after_cleanup_completes():
    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.RLock()
    calls = []
    canceled_task = object()

    class WorkflowManager:
        def cancel_workflow(self, workflow_id):
            assert workflow_id in scheduler._stopped_workflows()
            calls.append(("cancel", workflow_id))
            return [canceled_task]

        def clear_workflow(self, workflow_id):
            calls.append(("clear", workflow_id))

    class ResourceManager:
        def release_task_resource(self, tasks):
            calls.append(("release_tasks", tasks))

        def release_dag_context(self, workflow_id):
            calls.append(("release_dag", workflow_id))

    scheduler.workflow_manager = WorkflowManager()
    scheduler.resource_manager = ResourceManager()
    scheduler._release_model_route = lambda task: calls.append(
        ("release_model", task)
    )

    response = scheduler._stop_workflow_request({
        "workflow_id": "run-1",
        "request_id": "request-1",
    })

    assert calls == [
        ("cancel", "run-1"),
        ("release_model", canceled_task),
        ("release_tasks", [canceled_task]),
        ("clear", "run-1"),
        ("release_dag", "run-1"),
    ]
    assert response == {
        "type": "workflow_stopped",
        "data": {
            "request_id": "request-1",
            "workflow_id": "run-1",
            "ok": True,
        },
    }


def test_stop_workflow_cleanup_failure_returns_negative_ack():
    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.RLock()
    calls = []
    release_attempts = 0

    class WorkflowManager:
        def cancel_workflow(self, workflow_id):
            calls.append(("cancel", workflow_id))
            return [object()]

        def clear_workflow(self, workflow_id):
            calls.append(("clear", workflow_id))

    class ResourceManager:
        def release_task_resource(self, tasks):
            nonlocal release_attempts
            release_attempts += 1
            calls.append(("release_tasks", release_attempts))
            if release_attempts == 1:
                raise OSError("injected release failure")

        def release_dag_context(self, workflow_id):
            calls.append(("release_dag", workflow_id))

    scheduler.workflow_manager = WorkflowManager()
    scheduler.resource_manager = ResourceManager()
    scheduler._release_model_route = lambda _task: None

    response = scheduler._stop_workflow_request({
        "workflow_id": "run-1",
        "request_id": "request-1",
    })

    assert response["type"] == "workflow_stopped"
    assert response["data"] == {
        "request_id": "request-1",
        "workflow_id": "run-1",
        "ok": False,
        "error": "injected release failure",
    }
    assert "run-1" in scheduler._stopped_workflows()

    retry_response = scheduler._stop_workflow_request({
        "workflow_id": "run-1",
        "request_id": "request-1",
    })

    assert retry_response["data"]["ok"] is True
    assert [call for call in calls if call[0] == "cancel"] == [
        ("cancel", "run-1")
    ]
    assert [call for call in calls if call[0] == "release_tasks"] == [
        ("release_tasks", 1),
        ("release_tasks", 2),
    ]
    assert ("clear", "run-1") in calls
    assert ("release_dag", "run-1") in calls
    assert scheduler._workflow_cleanup_states == {}
