import fcntl
import json
import multiprocessing as mp
import os
from pathlib import Path
import threading

import pytest

from maze.core.scheduler import scheduler as scheduler_module


class _ReadyQueue:
    def __init__(self):
        self.messages = []

    def put(self, message):
        self.messages.append(message)


def _clear_scheduler_identity_environment(monkeypatch):
    for name in (
        scheduler_module.SCHEDULER_PROCESS_MARKER_ENV,
        scheduler_module.SCHEDULER_OWNER_ID_ENV,
    ):
        monkeypatch.setenv(name, "scheduler-identity-test-sentinel")
        monkeypatch.delenv(name)


def _run_scheduler_process(owner_id, ready_queue, fatal_event=None):
    return scheduler_module.scheduler_process(
        41001,
        41002,
        "FCFS",
        41003,
        ready_queue,
        fatal_event=fatal_event,
        owner_id=owner_id,
    )


def test_scheduler_process_marks_owner_before_scheduler_construction(monkeypatch):
    owner_id = "ABCDEF0123456789ABCDEF0123456789"
    expected_owner_id = owner_id.lower()
    observations = []

    class SchedulerProbe:
        def __init__(self, *_args, **kwargs):
            observations.append({
                "process_marker": os.environ.get(
                    scheduler_module.SCHEDULER_PROCESS_MARKER_ENV
                ),
                "environment_owner": os.environ.get(
                    scheduler_module.SCHEDULER_OWNER_ID_ENV
                ),
                "scheduler_owner": kwargs.get("owner_id"),
            })

        def start(self):
            return None

    original_environment = {
        name: os.environ.get(name)
        for name in (
            scheduler_module.SCHEDULER_PROCESS_MARKER_ENV,
            scheduler_module.SCHEDULER_OWNER_ID_ENV,
        )
    }
    with monkeypatch.context() as isolated:
        _clear_scheduler_identity_environment(isolated)
        isolated.setattr(scheduler_module, "Scheduler", SchedulerProbe)
        isolated.setattr(
            scheduler_module,
            "_create_scheduler_identity_memfd",
            lambda _owner_id: None,
        )

        ready_queue = _ReadyQueue()
        _run_scheduler_process(owner_id, ready_queue)

        assert observations == [{
            "process_marker": "1",
            "environment_owner": expected_owner_id,
            "scheduler_owner": expected_owner_id,
        }]
        assert ready_queue.messages == []

    assert {
        name: os.environ.get(name)
        for name in original_environment
    } == original_environment


def test_scheduler_process_generates_and_reuses_missing_owner(monkeypatch):
    generated_owner_id = "0123456789abcdef0123456789abcdef"
    observations = []

    class SchedulerProbe:
        def __init__(self, *_args, **kwargs):
            observations.append((
                os.environ.get(scheduler_module.SCHEDULER_OWNER_ID_ENV),
                kwargs.get("owner_id"),
            ))

        def start(self):
            return None

    with monkeypatch.context() as isolated:
        _clear_scheduler_identity_environment(isolated)
        isolated.setattr(scheduler_module, "Scheduler", SchedulerProbe)
        isolated.setattr(
            scheduler_module,
            "_create_scheduler_identity_memfd",
            lambda _owner_id: None,
        )
        isolated.setattr(
            scheduler_module.uuid,
            "uuid4",
            lambda: type("GeneratedUuid", (), {"hex": generated_owner_id})(),
        )

        _run_scheduler_process(None, _ReadyQueue())

        assert observations == [(generated_owner_id, generated_owner_id)]
        assert os.environ[scheduler_module.SCHEDULER_PROCESS_MARKER_ENV] == "1"


def test_scheduler_process_continues_without_unsealed_identity_receipt(monkeypatch):
    owner_id = "0123456789abcdef0123456789abcdef"
    started = []

    class SchedulerProbe:
        def __init__(self, *_args, **_kwargs):
            return None

        def start(self):
            started.append(True)

    def fail_memfd_create(*_args, **_kwargs):
        raise OSError("injected memfd failure")

    with monkeypatch.context() as isolated:
        _clear_scheduler_identity_environment(isolated)
        isolated.setattr(scheduler_module, "Scheduler", SchedulerProbe)
        isolated.setattr(scheduler_module.os, "memfd_create", fail_memfd_create)
        isolated.setattr(scheduler_module, "_SCHEDULER_IDENTITY_FD", None)

        ready_queue = _ReadyQueue()
        _run_scheduler_process(owner_id, ready_queue)

        assert started == [True]
        assert ready_queue.messages == []
        assert scheduler_module._SCHEDULER_IDENTITY_FD is None
        assert os.environ[scheduler_module.SCHEDULER_PROCESS_MARKER_ENV] == "1"
        assert os.environ[scheduler_module.SCHEDULER_OWNER_ID_ENV] == owner_id


@pytest.mark.parametrize(
    "owner_id",
    [
        "",
        "0" * 31,
        "0" * 33,
        "g" * 32,
        "01234567-89ab-cdef-0123-456789abcdef",
        123,
    ],
)
def test_scheduler_process_rejects_invalid_owner_before_marking_or_construction(
    monkeypatch,
    owner_id,
):
    ready_queue = _ReadyQueue()
    fatal_event = threading.Event()

    class SchedulerMustNotBeConstructed:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("Scheduler was constructed for an invalid owner")

    with monkeypatch.context() as isolated:
        isolated.setenv(scheduler_module.SCHEDULER_PROCESS_MARKER_ENV, "parent")
        isolated.setenv(scheduler_module.SCHEDULER_OWNER_ID_ENV, "parent-owner")
        isolated.setattr(
            scheduler_module,
            "Scheduler",
            SchedulerMustNotBeConstructed,
        )

        with pytest.raises(
            ValueError,
            match="Scheduler owner_id must be 32 hexadecimal characters",
        ):
            _run_scheduler_process(owner_id, ready_queue, fatal_event)

        assert os.environ[scheduler_module.SCHEDULER_PROCESS_MARKER_ENV] == "parent"
        assert os.environ[scheduler_module.SCHEDULER_OWNER_ID_ENV] == "parent-owner"

    assert fatal_event.is_set()
    assert ready_queue.messages == [{
        "status": "error",
        "error": "Scheduler owner_id must be 32 hexadecimal characters",
    }]


def test_scheduler_identity_memfd_is_proc_readable_sealed_and_ephemeral(
    monkeypatch,
):
    context = mp.get_context("fork")
    stop_event = context.Event()
    ready_queue = context.Queue()
    owner_id = "0123456789abcdef0123456789abcdef"
    session_id = "fedcba9876543210fedcba9876543210"

    class BlockingScheduler:
        def __init__(self, *_args, **_kwargs):
            return None

        def start(self):
            ready_queue.put(os.getpid())
            stop_event.wait(10)

    process = None
    identity_path = None
    with monkeypatch.context() as isolated:
        isolated.setenv("MAZE_PHASE2_ACCEPTANCE_SESSION", session_id)
        isolated.setattr(scheduler_module, "Scheduler", BlockingScheduler)
        process = context.Process(
            target=scheduler_module.scheduler_process,
            args=(41001, 41002, "FCFS", 41003, ready_queue),
            kwargs={"owner_id": owner_id},
        )
        process.start()
        try:
            child_pid = ready_queue.get(timeout=5)
            assert child_pid == process.pid
            descriptor_paths = []
            for candidate in Path(f"/proc/{child_pid}/fd").iterdir():
                try:
                    target = os.readlink(candidate)
                except FileNotFoundError:
                    continue
                if scheduler_module.SCHEDULER_IDENTITY_MEMFD_NAME in target:
                    descriptor_paths.append(candidate)
            assert len(descriptor_paths) == 1
            identity_path = descriptor_paths[0]

            with identity_path.open("rb") as handle:
                encoded = handle.read()
                seals = fcntl.fcntl(handle.fileno(), fcntl.F_GET_SEALS)
            receipt = json.loads(encoded)
            external_stat = Path(f"/proc/{child_pid}/stat").read_text(
                encoding="ascii"
            )
            external_start_ticks = int(
                external_stat[external_stat.rfind(")") + 2 :].split()[19]
            )
            required_seals = (
                fcntl.F_SEAL_WRITE
                | fcntl.F_SEAL_GROW
                | fcntl.F_SEAL_SHRINK
                | fcntl.F_SEAL_SEAL
            )
            assert encoded == json.dumps(
                receipt,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            assert receipt == {
                "owner_id": owner_id,
                "pid": child_pid,
                "ppid": os.getpid(),
                "process": "scheduler",
                "schema": scheduler_module.SCHEDULER_IDENTITY_SCHEMA,
                "session_id": session_id,
                "start_ticks": external_start_ticks,
            }
            assert seals & required_seals == required_seals
            fd_info = (Path(f"/proc/{child_pid}/fdinfo") / identity_path.name)
            flags_line = next(
                line
                for line in fd_info.read_text(encoding="ascii").splitlines()
                if line.startswith("flags:")
            )
            assert int(flags_line.split()[1], 8) & os.O_CLOEXEC
        finally:
            stop_event.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)

    assert process is not None and process.exitcode == 0
    assert identity_path is not None and not identity_path.exists()
