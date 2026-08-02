import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from maze.client.maze import client as client_module
from maze.client.maze import models as models_module
from maze.client.maze.client import MaClient
from maze.client.maze.workflow import MaWorkflow


class _Response:
    status_code = 200
    text = "ok"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return dict(self._payload)


@pytest.fixture
def blackhole_http_server():
    release = threading.Event()
    accepted = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            accepted.set()
            release.wait(timeout=5)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", accepted
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_request_timeout_propagates_to_created_and_loaded_workflows(monkeypatch):
    calls = []

    def post(url, json=None, **kwargs):
        calls.append((url, json, kwargs))
        if url.endswith("/create_workflow"):
            return _Response({"status": "success", "workflow_id": "workflow-1"})
        return _Response({"status": "success", "run_id": "run-1"})

    monkeypatch.setattr(client_module.requests, "post", post)
    client = MaClient("http://maze.test", request_timeout=1.25)

    created = client.create_workflow()
    loaded = client.get_workflow("workflow-2")

    assert created.request_timeout == 1.25
    assert loaded.request_timeout == 1.25
    assert calls == [
        ("http://maze.test/create_workflow", None, {"timeout": 1.25}),
    ]

    assert created.run() == "run-1"
    assert calls[-1] == (
        "http://maze.test/run_workflow",
        {"workflow_id": "workflow-1"},
        {"timeout": 1.25},
    )


def test_default_request_timeout_keeps_requests_call_shape(monkeypatch):
    def post(url):
        assert url == "http://maze.test/create_workflow"
        return _Response({"status": "success", "workflow_id": "workflow-1"})

    monkeypatch.setattr(client_module.requests, "post", post)

    workflow = MaClient("http://maze.test").create_workflow()

    assert workflow.request_timeout is None


def test_request_timeout_propagates_to_legacy_task_save_and_delete(monkeypatch):
    calls = []

    def post(url, json=None, **kwargs):
        calls.append((url, json, kwargs))
        if url.endswith("/add_task"):
            return _Response({"status": "success", "task_id": "task-1"})
        return _Response({"status": "success"})

    monkeypatch.setattr(models_module.requests, "post", post)
    workflow = MaWorkflow(
        "workflow-1",
        "http://maze.test",
        request_timeout=1.25,
    )

    task = workflow.add_task(task_type="code", task_name="legacy")
    task.save(
        "def legacy():\n    return {'value': 1}\n",
        {"input_params": {}},
        {"output_params": {}},
        {"cpu": 1, "cpu_mem": 1, "gpu": 0, "gpu_mem": 0},
    )
    task.delete()

    assert task.request_timeout == 1.25
    assert [kwargs for _, _, kwargs in calls] == [
        {"timeout": 1.25},
        {"timeout": 1.25},
        {"timeout": 1.25},
    ]


def test_wait_run_bounds_each_poll_by_remaining_deadline(monkeypatch):
    observed_timeouts = []

    def get(_url, **kwargs):
        observed_timeouts.append(kwargs["timeout"])
        return _Response({"status": "success", "run": {"status": "running"}})

    monkeypatch.setattr(client_module.requests, "get", get)
    client = MaClient("http://maze.test", request_timeout=10)

    with pytest.raises(TimeoutError):
        client.wait_run("run-1", timeout=0.05, poll_interval=0.01)

    assert observed_timeouts
    assert all(0 < value <= 0.05 for value in observed_timeouts)


def test_wait_run_times_out_against_real_blackhole_http_server(
    blackhole_http_server,
):
    server_url, accepted = blackhole_http_server
    client = MaClient(server_url)
    started = time.monotonic()

    with pytest.raises(TimeoutError) as exc_info:
        client.wait_run("run-1", timeout=0.2, poll_interval=0.01)

    elapsed = time.monotonic() - started
    assert accepted.is_set()
    assert not isinstance(exc_info.value, requests.exceptions.Timeout)
    assert elapsed < 1.0
