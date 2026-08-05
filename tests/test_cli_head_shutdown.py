import logging
import sys
import asyncio
from types import ModuleType

import pytest

from maze.cli import cli


class _Mapath:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.cleanup_calls = 0
        self.shutdown_requests = 0

    def request_scheduler_shutdown(self):
        self.shutdown_requests += 1

    def cleanup(self):
        self.cleanup_calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.mark.asyncio
async def test_head_cleanup_retries_in_worker_thread_until_complete(monkeypatch, caplog):
    mapath = _Mapath([RuntimeError("first failure"), False, True])
    to_thread_calls = []

    async def to_thread(function, *args, **kwargs):
        to_thread_calls.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(cli.asyncio, "to_thread", to_thread)
    caplog.set_level(logging.WARNING, logger=cli.__name__)

    assert await cli._cleanup_mapath_with_retries(mapath) is True
    assert mapath.cleanup_calls == 3
    assert mapath.shutdown_requests == 1
    assert to_thread_calls == [mapath.cleanup, mapath.cleanup, mapath.cleanup]
    assert "cleanup attempt 1/3 failed" in caplog.text
    assert "cleanup attempt 2/3 remained incomplete" in caplog.text


@pytest.mark.asyncio
async def test_head_cleanup_reports_incomplete_cleanup(monkeypatch, caplog):
    mapath = _Mapath([False, False, False])

    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(cli.asyncio, "to_thread", to_thread)
    caplog.set_level(logging.WARNING, logger=cli.__name__)

    assert await cli._cleanup_mapath_with_retries(mapath) is False
    assert mapath.cleanup_calls == 3
    assert mapath.shutdown_requests == 1
    assert "cleanup did not complete after 3 attempts" in caplog.text


def test_playground_process_uses_own_unix_session(monkeypatch, tmp_path):
    captured = {}
    process = object()

    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    monkeypatch.setattr(cli.sys, "platform", "linux")

    assert cli._start_playground_process(
        ["node", "server.js"],
        cwd=tmp_path,
        env={"PORT": "3001"},
    ) is process
    assert captured["kwargs"]["start_new_session"] is True
    assert "stdout" not in captured["kwargs"]
    assert "stderr" not in captured["kwargs"]


def test_stop_playground_terminates_only_child_process_group(monkeypatch):
    class Process:
        pid = 1234

        def poll(self):
            return None

        def wait(self, timeout):
            return 0

        def terminate(self):
            raise AssertionError("own-session child should be stopped by its group")

    signals = []
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(cli.os, "killpg", lambda pid, signum: signals.append((pid, signum)))

    cli.stop_playground([("backend", Process())])

    assert signals == [(1234, cli.signal.SIGTERM)]


@pytest.mark.asyncio
async def test_head_init_failure_still_runs_mapath_cleanup(monkeypatch):
    class Mapath:
        def __init__(self):
            self.init_calls = 0
            self.cleanup_calls = 0
            self.shutdown_requests = 0

        def init(self, **_kwargs):
            self.init_calls += 1
            raise RuntimeError("init failed")

        def request_scheduler_shutdown(self):
            self.shutdown_requests += 1

        def cleanup(self):
            self.cleanup_calls += 1
            return True

    mapath = Mapath()
    server_module = ModuleType("maze.core.server")
    server_module.app = object()
    server_module.mapath = mapath
    monkeypatch.setitem(sys.modules, "maze.core.server", server_module)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *_args: None)

    with pytest.raises(RuntimeError, match="init failed"):
        await cli._async_start_head(port=8000, ray_head_port=6379)

    assert mapath.init_calls == 1
    assert mapath.shutdown_requests == 1
    assert mapath.cleanup_calls == 1


@pytest.mark.asyncio
async def test_head_cleanup_exhaustion_fails_lifecycle(monkeypatch):
    class Mapath:
        def __init__(self):
            self.cleanup_calls = 0
            self.shutdown_requests = 0

        def init(self, **_kwargs):
            return None

        async def monitor_coroutine(self):
            return None

        async def maintenance_coroutine(self):
            return None

        def request_scheduler_shutdown(self):
            self.shutdown_requests += 1

        def cleanup(self):
            self.cleanup_calls += 1
            return False

    class Server:
        should_exit = False

        async def serve(self):
            return None

    mapath = Mapath()
    server = Server()
    server_module = ModuleType("maze.core.server")
    server_module.app = object()
    server_module.mapath = mapath
    monkeypatch.setitem(sys.modules, "maze.core.server", server_module)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *_args: None)
    monkeypatch.setattr(cli.uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli.uvicorn, "Server", lambda _config: server)

    with pytest.raises(RuntimeError, match="Maze head cleanup failed after 3 attempts"):
        await cli._async_start_head(port=8000, ray_head_port=6379)

    assert mapath.shutdown_requests == 1
    assert mapath.cleanup_calls == cli.HEAD_CLEANUP_MAX_ATTEMPTS
    assert server.should_exit is True


@pytest.mark.asyncio
async def test_head_server_exit_cancels_background_tasks_and_cleans_up(monkeypatch):
    cancelled = []

    class Mapath:
        cleanup_calls = 0
        shutdown_requests = 0

        def init(self, **_kwargs):
            return None

        async def monitor_coroutine(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append("monitor")

        async def maintenance_coroutine(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append("maintenance")

        def request_scheduler_shutdown(self):
            self.shutdown_requests += 1

        def cleanup(self):
            self.cleanup_calls += 1
            return True

    class Server:
        should_exit = False

        async def serve(self):
            return None

    mapath = Mapath()
    server = Server()
    server_module = ModuleType("maze.core.server")
    server_module.app = object()
    server_module.mapath = mapath
    monkeypatch.setitem(sys.modules, "maze.core.server", server_module)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *_args: None)
    monkeypatch.setattr(cli.uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli.uvicorn, "Server", lambda _config: server)

    await cli._async_start_head(port=8000, ray_head_port=6379)

    assert sorted(cancelled) == ["maintenance", "monitor"]
    assert mapath.shutdown_requests == 1
    assert mapath.cleanup_calls == 1
    assert server.should_exit is True


@pytest.mark.asyncio
async def test_head_cleanup_failure_chains_init_error(monkeypatch):
    class Mapath:
        def __init__(self):
            self.cleanup_calls = 0

        def init(self, **_kwargs):
            raise ValueError("init failed before readiness")

        def request_scheduler_shutdown(self):
            return None

        def cleanup(self):
            self.cleanup_calls += 1
            return False

    mapath = Mapath()
    server_module = ModuleType("maze.core.server")
    server_module.app = object()
    server_module.mapath = mapath
    monkeypatch.setitem(sys.modules, "maze.core.server", server_module)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *_args: None)

    with pytest.raises(RuntimeError, match="Maze head cleanup failed") as exc_info:
        await cli._async_start_head(port=8000, ray_head_port=6379)

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert str(exc_info.value.__cause__) == "init failed before readiness"
    assert mapath.cleanup_calls == cli.HEAD_CLEANUP_MAX_ATTEMPTS
