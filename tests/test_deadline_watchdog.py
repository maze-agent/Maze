import asyncio
import copy
import importlib
import sys
import time
from types import SimpleNamespace

import pytest

from maze.core.path.path import MaPath, SchedulerUnavailableError
from maze.core.workflow.dynamic import DynamicRun
from maze.core.workflow.dynamic_store import DynamicRunStore
from maze.core.workflow.static_run import StaticRun, StaticRunStore
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow


TASK_RESOURCES = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}


def _workflow(*task_ids: str) -> Workflow:
    workflow = Workflow("workflow")
    for task_id in task_ids:
        task = CodeTask(workflow.id, task_id, task_id)
        task.save_task(
            task_input={"input_params": {}},
            task_output={
                "output_params": {
                    "1": {"key": "result", "data_type": "str"},
                }
            },
            code_str="def task():\n    return {'result': 'ok'}",
            code_ser="",
            resources=TASK_RESOURCES,
        )
        workflow.add_task(task_id, task)
    return workflow


class _Process:
    def __init__(self, alive: bool, pid: int = 321, exitcode: int | None = None):
        self.alive = alive
        self.pid = pid
        self.exitcode = exitcode

    def is_alive(self):
        return self.alive


class _StaticStore:
    def __init__(self):
        self.events = []
        self.snapshots = []

    def append_event(self, run_id, event):
        self.events.append((run_id, copy.deepcopy(event)))

    def load_events(self, run_id, after=None):
        return [
            copy.deepcopy(event)
            for stored_run_id, event in self.events
            if stored_run_id == run_id
            and (after is None or int(event.get("seq", 0)) > after)
        ]

    def save_run(self, snapshot):
        self.snapshots.append(copy.deepcopy(snapshot))


class _DynamicStore:
    def __init__(self):
        self.events = []
        self.snapshots = []

    def append_event(self, run_id, event, snapshot=None):
        self.events.append((run_id, copy.deepcopy(event)))

    def load_events(self, run_id, after=None):
        return [
            copy.deepcopy(event)
            for stored_run_id, event in self.events
            if stored_run_id == run_id
            and (after is None or int(event.get("seq", 0)) > after)
        ]

    def save_run(self, snapshot):
        self.snapshots.append(copy.deepcopy(snapshot))


class _FailingStaticStore(_StaticStore):
    def __init__(self, *, fail_append_before=False, fail_append_after=False):
        super().__init__()
        self.fail_append_before = fail_append_before
        self.fail_append_after = fail_append_after

    def append_event(self, run_id, event):
        if self.fail_append_before:
            self.fail_append_before = False
            raise OSError("injected static event persistence failure")
        super().append_event(run_id, event)
        if self.fail_append_after:
            self.fail_append_after = False
            raise OSError("injected static event post-write failure")


class _FailingDynamicStore(_DynamicStore):
    def __init__(self, *, fail_save=False):
        super().__init__()
        self.fail_save = fail_save

    def save_run(self, snapshot):
        if self.fail_save:
            self.fail_save = False
            raise OSError("injected dynamic snapshot persistence failure")
        super().save_run(snapshot)


class _Metrics:
    def __init__(self):
        self.changes = []

    def on_run_status_change(self, run_id, old, new):
        self.changes.append((run_id, old, new))


def _dynamic_run(run_id: str, *states: str) -> DynamicRun:
    run = DynamicRun(run_id, timeout_seconds=1)
    for index, state in enumerate(states):
        task_id = f"task-{index}"
        task = CodeTask(run_id, task_id, task_id)
        run.tasks[task_id] = task
        if state == "pending":
            run.pending_tasks[task_id] = task
        elif state == "submitted":
            run.submitted_tasks.add(task_id)
        elif state == "running":
            run.running_tasks.add(task_id)
        else:
            raise AssertionError(f"Unknown test task state: {state}")
    return run


def _maintenance_path(static_run: StaticRun, dynamic_run: DynamicRun, process: _Process):
    path = object.__new__(MaPath)
    path.scheduler_process = process
    path._scheduler_failure_handled = None
    path._cleanup_started = False
    path.static_runs = {static_run.run_id: static_run}
    path.dynamic_runs = {dynamic_run.run_id: dynamic_run}
    path.async_que = {
        static_run.run_id: asyncio.Queue(),
        dynamic_run.run_id: asyncio.Queue(),
    }
    path.static_run_store = _StaticStore()
    path.dynamic_run_store = _DynamicStore()
    path.global_metrics = _Metrics()
    path.ray_cleanup_calls = 0

    def stop_ray():
        path.ray_cleanup_calls += 1
        return True

    path._stop_local_ray_best_effort = stop_ray
    return path


def test_static_deadline_terminalizes_only_active_tasks():
    run = StaticRun(
        "static-run",
        "workflow",
        _workflow("done", "running", "queued"),
        timeout_seconds=1,
    )
    run.task_nodes["done"]["status"] = "succeeded"
    run.task_nodes["running"]["status"] = "running"
    run.submitted_time = time.time() - 2

    assert run.mark_timed_out_if_needed() is True
    assert run.status == "timed_out"
    assert run.task_nodes["done"]["status"] == "succeeded"
    assert run.task_nodes["running"]["status"] == "timed_out"
    assert run.task_nodes["queued"]["status"] == "timed_out"
    assert run.snapshot()["task_counts"] == {
        "total": 3,
        "pending": 0,
        "queued": 0,
        "running": 0,
        "succeeded": 1,
        "failed": 0,
        "cancelled": 0,
        "timed_out": 2,
    }
    assert run.mark_timed_out_if_needed() is False


def test_dynamic_deadline_clears_and_terminalizes_every_active_map():
    run = _dynamic_run("dynamic-run", "pending", "submitted", "running")
    run.created_time = time.time() - 2

    assert run.mark_timed_out_if_needed() is True
    assert run.status == "timed_out"
    assert run.pending_tasks == {}
    assert run.submitted_tasks == set()
    assert run.running_tasks == set()
    assert run.failed_tasks == set(run.tasks)
    assert set(run.task_errors) == set(run.tasks)
    assert {
        node["status"] for node in run.snapshot()["task_nodes"].values()
    } == {"failed"}


def test_deadline_sweep_persists_events_and_stops_both_runtimes():
    static_run = StaticRun(
        "static-run",
        "workflow",
        _workflow("task"),
        timeout_seconds=1,
    )
    static_run.submitted_time = time.time() - 2
    dynamic_run = _dynamic_run("dynamic-run", "running")
    dynamic_run.created_time = time.time() - 2
    path = _maintenance_path(static_run, dynamic_run, _Process(True))
    sent = []
    path._send_scheduler_message = sent.append

    asyncio.run(path._sweep_run_deadlines())

    assert [event[1]["type"] for event in path.static_run_store.events] == [
        "timeout_workflow"
    ]
    assert [event[1]["type"] for event in path.dynamic_run_store.events] == [
        "timeout_dynamic_run"
    ]
    assert path.static_run_store.snapshots[-1]["status"] == "timed_out"
    assert path.dynamic_run_store.snapshots[-1]["status"] == "timed_out"
    assert {message["data"]["workflow_id"] for message in sent} == {
        "static-run",
        "dynamic-run",
    }


def test_scheduler_exit_interrupts_active_runs_exactly_once():
    static_run = StaticRun("static-run", "workflow", _workflow("task"))
    dynamic_run = _dynamic_run("dynamic-run", "running")
    process = _Process(False, pid=321, exitcode=17)
    path = _maintenance_path(static_run, dynamic_run, process)

    asyncio.run(path._handle_scheduler_exit())
    asyncio.run(path._handle_scheduler_exit())

    assert static_run.status == "interrupted"
    assert static_run.task_nodes["task"]["status"] == "cancelled"
    assert dynamic_run.status == "interrupted"
    assert dynamic_run.running_tasks == set()
    assert dynamic_run.failed_tasks == {"task-0"}
    assert [event[1]["type"] for event in path.static_run_store.events] == [
        "interrupt_workflow"
    ]
    assert [event[1]["type"] for event in path.dynamic_run_store.events] == [
        "interrupt_dynamic_run"
    ]
    assert path._scheduler_failure_handled == (321, 17)
    assert path.ray_cleanup_calls == 1


def test_scheduler_exit_retries_each_run_after_event_persistence_failure():
    static_run = StaticRun("static-run", "workflow", _workflow("task"))
    dynamic_run = _dynamic_run("dynamic-run", "running")
    path = _maintenance_path(
        static_run,
        dynamic_run,
        _Process(False, pid=321, exitcode=17),
    )
    path.static_run_store = _FailingStaticStore(fail_append_before=True)

    asyncio.run(path._handle_scheduler_exit())

    assert static_run.status == "interrupted"
    assert len(static_run.event_log) == 1
    assert path.static_run_store.events == []
    assert path.static_run_store.snapshots == []
    assert [event[1]["type"] for event in path.dynamic_run_store.events] == [
        "interrupt_dynamic_run"
    ]
    assert path._scheduler_failure_handled is None
    assert path.ray_cleanup_calls == 1

    asyncio.run(path._handle_scheduler_exit())

    assert [event[1]["type"] for event in path.static_run_store.events] == [
        "interrupt_workflow"
    ]
    assert path.static_run_store.snapshots[-1]["status"] == "interrupted"
    assert len(static_run.event_log) == 1
    assert len(path.dynamic_run_store.events) == 1
    assert path.global_metrics.changes == [
        ("static-run", "submitted", "interrupted")
    ]
    assert path.async_que["static-run"].qsize() == 1
    assert path.async_que["dynamic-run"].qsize() == 1
    assert path._scheduler_failure_handled == (321, 17)
    assert path.ray_cleanup_calls == 1


def test_scheduler_exit_deduplicates_event_after_ambiguous_append_failure():
    static_run = StaticRun("static-run", "workflow", _workflow("task"))
    dynamic_run = _dynamic_run("dynamic-run")
    dynamic_run.status = "interrupted"
    path = _maintenance_path(
        static_run,
        dynamic_run,
        _Process(False, pid=321, exitcode=17),
    )
    path.static_run_store = _FailingStaticStore(fail_append_after=True)

    asyncio.run(path._handle_scheduler_exit())
    asyncio.run(path._handle_scheduler_exit())

    assert len(path.static_run_store.events) == 1
    assert len(path.static_run_store.snapshots) == 1
    assert len(static_run.event_log) == 1
    assert path._scheduler_failure_handled == (321, 17)
    assert path.ray_cleanup_calls == 1


def test_scheduler_exit_retries_snapshot_without_duplicate_dynamic_event():
    static_run = StaticRun("static-run", "workflow", _workflow("task"))
    static_run.mark_interrupted("already terminal")
    dynamic_run = _dynamic_run("dynamic-run", "running")
    path = _maintenance_path(
        static_run,
        dynamic_run,
        _Process(False, pid=321, exitcode=17),
    )
    path.dynamic_run_store = _FailingDynamicStore(fail_save=True)

    asyncio.run(path._handle_scheduler_exit())

    assert len(path.dynamic_run_store.events) == 1
    assert path.dynamic_run_store.snapshots == []
    assert len(dynamic_run.event_log) == 1
    assert path._scheduler_failure_handled is None
    assert path.ray_cleanup_calls == 1

    asyncio.run(path._handle_scheduler_exit())

    assert len(path.dynamic_run_store.events) == 1
    assert len(path.dynamic_run_store.snapshots) == 1
    assert path.dynamic_run_store.snapshots[0]["status"] == "interrupted"
    assert len(dynamic_run.event_log) == 1
    assert path.async_que["dynamic-run"].qsize() == 1
    assert path._scheduler_failure_handled == (321, 17)
    assert path.ray_cleanup_calls == 1


def test_static_restart_reuses_dangling_scheduler_interrupt_event(tmp_path):
    run = StaticRun("static-run", "workflow", _workflow("task"))
    store = StaticRunStore(tmp_path)
    baseline_event = run.append_event({"type": "submit_workflow", "data": {}})
    store.append_event(run.run_id, baseline_event)
    store.save_run(run.snapshot())

    reason = "scheduler exited before snapshot persistence"
    run.mark_interrupted(reason)
    event = run.append_event({
        "type": "interrupt_workflow",
        "data": {
            "run_id": run.run_id,
            "workflow_id": run.workflow_id,
            "reason": reason,
        },
    })
    store.append_event(run.run_id, event)
    assert store.append_event_once(run.run_id, event) is False

    restarted_store = StaticRunStore(tmp_path)
    recovered = restarted_store.recover_interrupted_runs()
    persisted_events = restarted_store.load_events(run.run_id)

    assert len(recovered) == 1
    assert recovered[0]["status"] == "interrupted"
    assert recovered[0]["error_summary"] == {"message": reason}
    assert recovered[0]["event_count"] == 2
    assert recovered[0]["last_event_seq"] == 2
    assert [item["type"] for item in persisted_events] == [
        "submit_workflow",
        "interrupt_workflow",
    ]
    assert restarted_store.recover_interrupted_runs() == []
    assert len(restarted_store.load_events(run.run_id)) == 2


def test_dynamic_restart_reuses_dangling_scheduler_interrupt_event(tmp_path):
    run = _dynamic_run("dynamic-run", "running")
    store = DynamicRunStore(tmp_path)
    baseline_event = run.append_event({"type": "create_dynamic_run", "data": {}})
    store.append_event(run.run_id, baseline_event, snapshot=run.snapshot())
    store.save_run(run.snapshot())

    reason = "scheduler exited before snapshot persistence"
    run.interrupt(reason)
    event = run.append_event({
        "type": "interrupt_dynamic_run",
        "data": {
            "run_id": run.run_id,
            "reason": reason,
        },
    })
    terminal_snapshot = run.snapshot()
    store.append_event(run.run_id, event, snapshot=terminal_snapshot)
    assert store.append_event_once(
        run.run_id,
        event,
        snapshot=terminal_snapshot,
    ) is False

    restarted_store = DynamicRunStore(tmp_path)
    recovered = restarted_store.recover_interrupted_runs()
    persisted_events = restarted_store.load_events(run.run_id)

    assert len(recovered) == 1
    assert recovered[0]["status"] == "interrupted"
    assert recovered[0]["failure_reason"] == reason
    assert recovered[0]["event_count"] == 2
    assert recovered[0]["last_event_seq"] == 2
    assert [item["type"] for item in persisted_events] == [
        "create_dynamic_run",
        "interrupt_dynamic_run"
    ]
    assert restarted_store.recover_interrupted_runs() == []
    assert len(restarted_store.load_events(run.run_id)) == 2


def test_dynamic_event_append_keeps_an_existing_legacy_log(tmp_path):
    run = _dynamic_run("dynamic-run", "running")
    store = DynamicRunStore(tmp_path)
    store.save_run(run.snapshot())

    first = run.append_event({"type": "first", "data": {}})
    store.append_event(run.run_id, first)
    second = run.append_event({"type": "second", "data": {}})
    store.append_event(run.run_id, second, snapshot=run.snapshot())

    run_dir = store.run_dir(run.run_id)
    assert (run_dir / "events.jsonl").exists()
    assert not store.dynamic_events_path(run_dir).exists()
    assert [event["type"] for event in store.load_events(run.run_id)] == [
        "first",
        "second",
    ]


def test_scheduler_guard_prevents_submission_state_before_dispatch():
    path = object.__new__(MaPath)
    path.scheduler_process = _Process(False, pid=654, exitcode=9)
    path.workflows = {"workflow": _workflow("task")}
    path.submit_workflows = {}
    path.static_runs = {}
    path.dynamic_runs = {}
    path.async_que = {}
    path.worker_registration_requests = {}

    with pytest.raises(SchedulerUnavailableError) as exc_info:
        path.run_workflow("workflow")
    assert exc_info.value.detail()["code"] == "scheduler_unavailable"
    assert exc_info.value.detail()["scheduler_exitcode"] == 9
    assert path.submit_workflows == {}
    assert path.static_runs == {}

    with pytest.raises(SchedulerUnavailableError):
        asyncio.run(path.create_dynamic_run())
    with pytest.raises(SchedulerUnavailableError):
        asyncio.run(path.start_worker("10.0.0.2", "node", {}))
    assert path.dynamic_runs == {}
    assert path.worker_registration_requests == {}

    availability = iter((True, False))
    path.scheduler_process = SimpleNamespace(
        is_alive=lambda: next(availability),
        pid=654,
        exitcode=9,
    )
    with pytest.raises(SchedulerUnavailableError):
        asyncio.run(path.start_worker("10.0.0.2", "node", {}))
    assert path.worker_registration_requests == {}


def test_submission_api_exposes_machine_readable_scheduler_503(monkeypatch, tmp_path):
    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    server = importlib.import_module("maze.core.server")

    def unavailable(*_args, **_kwargs):
        raise SchedulerUnavailableError("scheduler exited", pid=123, exitcode=7)

    monkeypatch.setattr(server.mapath, "run_workflow", unavailable)
    request = SimpleNamespace(
        json=lambda: None,
    )

    async def request_json():
        return {"workflow_id": "workflow"}

    request.json = request_json
    with pytest.raises(server.HTTPException) as exc_info:
        asyncio.run(server.run_workflow(request))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "scheduler_unavailable",
        "message": "scheduler exited",
        "scheduler_pid": 123,
        "scheduler_exitcode": 7,
    }


def test_cli_runs_and_cancels_maintenance_task(monkeypatch):
    cli = importlib.import_module("maze.cli.cli")

    class Mapath:
        def __init__(self):
            self.started = set()
            self.stopped = set()
            self.cleaned = False

        def init(self, **_kwargs):
            return None

        async def _wait(self, name):
            self.started.add(name)
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.add(name)

        async def monitor_coroutine(self):
            await self._wait("monitor")

        async def maintenance_coroutine(self):
            await self._wait("maintenance")

        def cleanup(self):
            self.cleaned = True
            return True

    fake_mapath = Mapath()
    fake_server_module = SimpleNamespace(app=object(), mapath=fake_mapath)
    monkeypatch.setitem(sys.modules, "maze.core.server", fake_server_module)

    class Server:
        def __init__(self, _config):
            self.should_exit = False

        async def serve(self):
            await asyncio.sleep(0)
            raise RuntimeError("stop test head")

    monkeypatch.setattr(cli.uvicorn, "Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cli.uvicorn, "Server", Server)

    with pytest.raises(RuntimeError, match="stop test head"):
        asyncio.run(cli._async_start_head(8000, 6379))

    assert fake_mapath.started == {"monitor", "maintenance"}
    assert fake_mapath.stopped == {"monitor", "maintenance"}
    assert fake_mapath.cleaned is True
