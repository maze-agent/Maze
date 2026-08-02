import pytest

from maze.core.path.path import MaPath, SCHEDULER_START_ABORT_CLEANUP_ATTEMPTS


class _UnstoppableProcess:
    pid = 4321

    def __init__(self):
        self.join_timeouts = []
        self.terminate_calls = 0
        self.kill_calls = 0

    def join(self, timeout):
        self.join_timeouts.append(timeout)

    def is_alive(self):
        return True

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1


def _abort_path(process=None, cleanup_outcomes=()):
    path = object.__new__(MaPath)
    path.scheduler_process = process
    outcomes = iter(cleanup_outcomes)
    path.cleanup_attempts = 0
    path.channels_closed = 0

    def stop_local_ray():
        path.cleanup_attempts += 1
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    path._stop_local_ray_best_effort = stop_local_ray
    path._close_scheduler_channels = lambda: setattr(
        path,
        "channels_closed",
        path.channels_closed + 1,
    )
    return path


def test_abort_scheduler_start_fails_if_child_survives_kill():
    process = _UnstoppableProcess()
    path = _abort_path(process, [True])

    with pytest.raises(RuntimeError, match="remained alive after terminate/kill"):
        path._abort_scheduler_start()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.join_timeouts == [1, 5, 5]
    assert path.cleanup_attempts == 1
    assert path.channels_closed == 0


def test_abort_scheduler_start_retries_incomplete_ray_cleanup():
    path = _abort_path(cleanup_outcomes=[False, True])

    path._abort_scheduler_start()

    assert path.cleanup_attempts == 2
    assert path.channels_closed == 1


def test_abort_scheduler_start_reports_exhausted_ray_cleanup():
    path = _abort_path(
        cleanup_outcomes=[False] * SCHEDULER_START_ABORT_CLEANUP_ATTEMPTS
    )

    with pytest.raises(RuntimeError, match="cleanup did not complete after 3 attempts"):
        path._abort_scheduler_start()

    assert path.cleanup_attempts == SCHEDULER_START_ABORT_CLEANUP_ATTEMPTS
    assert path.channels_closed == 1


def test_cleanup_retains_lease_and_channels_if_scheduler_survives_kill():
    process = _UnstoppableProcess()
    path = object.__new__(MaPath)
    path.scheduler_process = process
    path._cleanup_complete = False
    path._cleanup_started = False
    path._scheduler_shutdown_requested = False
    path.shutdown_requests = 0
    path.channels_closed = 0
    path.lease_releases = 0
    path.request_scheduler_shutdown = lambda: setattr(
        path,
        "shutdown_requests",
        path.shutdown_requests + 1,
    )
    path._stop_local_ray_best_effort = lambda: True
    path._close_scheduler_channels = lambda: setattr(
        path,
        "channels_closed",
        path.channels_closed + 1,
    )
    path._release_core_process_lease = lambda: setattr(
        path,
        "lease_releases",
        path.lease_releases + 1,
    )

    assert path.cleanup() is False

    assert path._cleanup_complete is False
    assert path._cleanup_started is False
    assert path.shutdown_requests == 1
    assert path.channels_closed == 0
    assert path.lease_releases == 0
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.join_timeouts == [75, 5, 5]
