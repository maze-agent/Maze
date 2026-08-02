from types import SimpleNamespace

import pytest

from maze.cli import cli as cli_module
from maze.core.worker import worker as worker_module
from maze.core.worker.worker import Worker


def test_stop_worker_forces_ray_shutdown_with_a_fixed_timeout(monkeypatch):
    calls = []
    result = SimpleNamespace(returncode=0, stdout="stopped", stderr="")
    monkeypatch.setattr(
        worker_module.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or result,
    )
    Worker._last_registration_payload = {"node_id": "worker-1"}

    assert Worker.stop_worker() is result

    command, kwargs = calls[0]
    assert command == worker_module.build_ray_command("stop", "--force")
    assert kwargs == {
        "check": False,
        "text": True,
        "capture_output": True,
        "timeout": worker_module.WORKER_STOP_TIMEOUT_SECONDS,
    }
    assert Worker._last_registration_payload is None


def test_stop_worker_surfaces_ray_cleanup_failure(monkeypatch):
    monkeypatch.setattr(
        worker_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="raylet remained alive",
        ),
    )

    with pytest.raises(RuntimeError, match="raylet remained alive"):
        Worker.stop_worker()


def test_agent_sigint_always_stops_worker_and_preserves_interrupt(monkeypatch):
    calls = []

    def interrupted_start(*_args, **_kwargs):
        calls.append("start")
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_module.Worker, "start_worker", interrupted_start)
    monkeypatch.setattr(
        cli_module.Worker,
        "stop_worker",
        lambda: calls.append("stop"),
    )

    with pytest.raises(KeyboardInterrupt):
        cli_module.start_worker("10.0.0.1:8000", agent=True)

    assert calls == ["start", "stop"]


def test_agent_cleanup_failure_does_not_mask_sigint(monkeypatch, caplog):
    monkeypatch.setattr(
        cli_module.Worker,
        "start_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        cli_module.Worker,
        "stop_worker",
        lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    with pytest.raises(KeyboardInterrupt):
        cli_module.start_worker("10.0.0.1:8000", agent=True)

    assert "cleanup failed" in caplog.text


def test_non_agent_registration_does_not_stop_the_ray_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli_module.Worker,
        "start_worker",
        lambda *_args, **_kwargs: calls.append("start"),
    )
    monkeypatch.setattr(
        cli_module.Worker,
        "stop_worker",
        lambda: calls.append("stop"),
    )

    cli_module.start_worker("10.0.0.1:8000", agent=False)

    assert calls == ["start"]
