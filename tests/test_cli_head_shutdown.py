import logging

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
async def test_head_cleanup_retries_in_a_worker_thread_until_complete(monkeypatch, caplog):
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
    assert "did not complete after" not in caplog.text


@pytest.mark.asyncio
async def test_head_cleanup_logs_an_explicit_error_after_three_incomplete_attempts(
    monkeypatch,
    caplog,
):
    mapath = _Mapath([False, False, False])

    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(cli.asyncio, "to_thread", to_thread)
    caplog.set_level(logging.WARNING, logger=cli.__name__)

    assert await cli._cleanup_mapath_with_retries(mapath) is False
    assert mapath.cleanup_calls == 3
    assert mapath.shutdown_requests == 1
    assert "cleanup did not complete after 3 attempts" in caplog.text
    assert "resources may require manual cleanup" in caplog.text
