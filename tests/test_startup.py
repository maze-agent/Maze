"""Startup ownership guards and deterministic rollback faults."""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import ray

from maze.cli import serve, start
from maze.spec import ApiConfig, EngineConfig


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    monkeypatch.setenv("MAZE_API_TOKEN", "startup-test-only")
    monkeypatch.delenv("RAY_ADDRESS", raising=False)
    monkeypatch.setattr(ray, "is_initialized", lambda: False)
    monkeypatch.setattr("maze.cli.subprocess.check_output", lambda *a, **k: "")
    init, shutdown = Mock(), Mock()
    monkeypatch.setattr(ray, "init", init)
    monkeypatch.setattr(ray, "shutdown", shutdown)
    return init, shutdown


@pytest.mark.parametrize(
    "reason", ["token", "attached", "address", "process", "inspection"]
)
def test_preflight_never_initializes_or_stops_ray(
    boundary: tuple[Mock, Mock], monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    init, shutdown = boundary
    if reason == "token":
        monkeypatch.delenv("MAZE_API_TOKEN")
    elif reason == "attached":
        monkeypatch.setattr(ray, "is_initialized", lambda: True)
    elif reason == "address":
        monkeypatch.setenv("RAY_ADDRESS", "127.0.0.1:6379")
    elif reason == "process":
        monkeypatch.setattr(
            "maze.cli.subprocess.check_output", lambda *a, **k: "/existing/raylet\n"
        )
    else:
        monkeypatch.setattr(
            "maze.cli.subprocess.check_output",
            Mock(side_effect=PermissionError("ps denied")),
        )
    with pytest.raises((ValueError, RuntimeError, PermissionError)):
        start(EngineConfig())
    init.assert_not_called()
    shutdown.assert_not_called()


def test_port_conflict_does_not_touch_existing_listener_or_ray(
    boundary: tuple[Mock, Mock],
) -> None:
    init, shutdown = boundary
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        with pytest.raises(OSError):
            start(EngineConfig(api=ApiConfig(port=port)))
        with socket.create_connection(("127.0.0.1", port)):
            connection, _ = occupied.accept()
            connection.close()
    init.assert_not_called()
    shutdown.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError("init failed"), KeyboardInterrupt()])
def test_partial_init_rolls_back_and_releases_port(
    boundary: tuple[Mock, Mock], failure: BaseException
) -> None:
    init, shutdown = boundary
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    init.side_effect = failure
    with pytest.raises(type(failure)):
        start(EngineConfig(api=ApiConfig(port=port)))
    shutdown.assert_called_once()
    with socket.socket() as retry:
        retry.bind(("127.0.0.1", port))


def test_shutdown_failure_still_releases_listener(boundary: tuple[Mock, Mock]) -> None:
    init, shutdown = boundary
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    init.side_effect = RuntimeError("partial init")
    shutdown.side_effect = RuntimeError("cleanup failed")
    with pytest.raises(RuntimeError, match="cleanup failed") as error:
        start(EngineConfig(api=ApiConfig(port=port)))
    assert str(error.value.__context__) == "partial init"
    with socket.socket() as retry:
        retry.bind(("127.0.0.1", port))


@pytest.mark.parametrize("failure_site", ["app", "server", "serve"])
def test_engine_closes_even_when_http_setup_fails(
    monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    closed = []

    async def close(self) -> None:
        closed.append(self)

    def fail(*args, **kwargs):
        raise RuntimeError("injected HTTP failure")

    async def fail_serve(*args, **kwargs):
        fail()

    monkeypatch.setattr("maze.engine.Engine.close", close)
    if failure_site == "app":
        monkeypatch.setattr("maze.api.create_app", fail)
    elif failure_site == "server":
        monkeypatch.setattr("uvicorn.Server", fail)
    else:
        monkeypatch.setattr("uvicorn.Server.serve", fail_serve)
    with socket.socket() as listener, pytest.raises(RuntimeError, match="injected"):
        asyncio.run(serve(EngineConfig(), "test-only", listener))
    assert len(closed) == 1


@pytest.mark.parametrize("phase", ["init", "http", "interrupt"])
def test_real_ray_rollback_in_owned_subprocess(phase: str) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    script = """
import sys
import ray
import maze.api
from maze.cli import start
from maze.spec import ApiConfig, EngineConfig
phase, port = sys.argv[1], int(sys.argv[2])
original = ray.init
injected = False

def fault(*args, **kwargs):
    global injected
    original(*args, **kwargs)
    injected = True
    if phase == 'interrupt':
        raise KeyboardInterrupt()
    raise RuntimeError('injected after real Ray startup')

def http_fault(*args, **kwargs):
    global injected
    assert ray.is_initialized()
    injected = True
    raise RuntimeError('injected HTTP construction failure')

if phase == 'http':
    maze.api.create_app = http_fault
else:
    ray.init = fault
try:
    start(EngineConfig(api=ApiConfig(port=port)))
except (RuntimeError, KeyboardInterrupt):
    assert injected
    assert not ray.is_initialized()
else:
    raise AssertionError('failure was not injected')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, phase, str(port)],
        env={**os.environ, "MAZE_API_TOKEN": "startup-test-only"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with socket.socket() as retry:
        retry.bind(("127.0.0.1", port))
    processes = subprocess.check_output(["ps", "-axo", "comm="], text=True)
    assert not any(
        Path(p.strip()).name in ("raylet", "gcs_server") for p in processes.splitlines()
    )
