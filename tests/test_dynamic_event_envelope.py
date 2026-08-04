import asyncio
import copy

import pytest
from fastapi import HTTPException

from maze.core.path.path import MaPath
from maze.core.workflow.dynamic import DynamicRun
from maze.core.workflow.dynamic_store import DynamicRunStore
from maze.core.workflow.static_run import StaticRun
from maze.core.workflow.workflow import Workflow


def _dynamic_path(tmp_path):
    run_id = "dynamic-envelope"
    run = DynamicRun(run_id)
    path = object.__new__(MaPath)
    path.dynamic_runs = {run_id: run}
    path.dynamic_run_store = DynamicRunStore(tmp_path)
    path.async_que = {run_id: asyncio.Queue()}
    return path, run


async def _seed_dynamic_run(path, run_id):
    return await path._emit_dynamic_event(run_id, {
        "type": "start_dynamic_run",
        "data": {"run_id": run_id},
    })


@pytest.mark.parametrize(
    "event",
    [
        {"type": "custom", "data": {}, "seq": 100},
        {"type": "custom", "data": {}, "timestamp": "attacker"},
        {"type": "custom", "data": {}, "schema_version": 99},
        {"type": "custom", "data": {}, "ts": 123.0},
        {"type": "custom", "data": {}, "unknown": True},
        {"type": "custom", "data": {"run_id": "other"}},
        {"type": "custom", "data": {"run_status": "finalized"}},
        {"type": "custom", "data": []},
    ],
)
def test_public_dynamic_event_rejects_injection_without_mutating_log(
    tmp_path,
    event,
):
    path, run = _dynamic_path(tmp_path)
    asyncio.run(_seed_dynamic_run(path, run.run_id))
    before_log = copy.deepcopy(run.event_log)
    before_snapshot = path.dynamic_run_store.load_run(run.run_id)

    with pytest.raises(ValueError):
        asyncio.run(path.emit_dynamic_run_event(run.run_id, event))

    assert run.event_seq == 1
    assert run.event_log == before_log
    assert path.dynamic_run_store.load_events(run.run_id) == before_log
    assert path.dynamic_run_store.load_run(run.run_id) == before_snapshot


def test_dynamic_event_envelope_is_server_owned_and_replay_remains_deduplicated(
    tmp_path,
):
    path, run = _dynamic_path(tmp_path)
    first = asyncio.run(_seed_dynamic_run(path, run.run_id))

    custom = asyncio.run(path.emit_dynamic_run_event(
        run.run_id,
        {"type": "custom_observation", "data": {"value": 7}},
    ))
    run.status = "running"
    system = asyncio.run(path._emit_dynamic_event(run.run_id, {
        "type": "task_pending",
        "seq": 100,
        "timestamp": "attacker",
        "schema_version": 99,
        "ts": 123.0,
        "data": {"run_status": "finalized"},
    }))
    replayed = asyncio.run(
        path._emit_dynamic_event(run.run_id, copy.deepcopy(system))
    )

    assert [first["seq"], custom["seq"], system["seq"]] == [1, 2, 3]
    assert custom["data"] == {
        "value": 7,
        "run_id": run.run_id,
        "run_status": "created",
    }
    assert system["schema_version"] == 1
    assert system["timestamp"] != "attacker"
    assert "ts" not in system
    assert system["data"]["run_id"] == run.run_id
    assert system["data"]["run_status"] == "running"
    assert replayed == system
    assert run.event_seq == 3

    reloaded_store = DynamicRunStore(tmp_path)
    persisted = reloaded_store.load_events(run.run_id)
    snapshot = reloaded_store.load_run(run.run_id)
    assert [event["seq"] for event in persisted] == [1, 2, 3]
    assert snapshot["event_count"] == 3
    assert snapshot["last_event_seq"] == 3


def test_cancel_seals_active_tasks_and_rejects_late_public_events(tmp_path):
    path, run = _dynamic_path(tmp_path)
    asyncio.run(_seed_dynamic_run(path, run.run_id))
    run.pending_tasks["pending-task"] = object()
    run.submitted_tasks.add("submitted-task")
    run.running_tasks.add("running-task")

    assert run.cancel("stop now") is True
    terminal = asyncio.run(path._emit_dynamic_event(run.run_id, {
        "type": "cancel_dynamic_run",
        "data": {"reason": "stop now"},
    }))

    with pytest.raises(ValueError, match="canceled"):
        asyncio.run(path.emit_dynamic_run_event(
            run.run_id,
            {"type": "agent_error", "data": {"error": "late"}},
        ))

    assert run.pending_tasks == {}
    assert run.submitted_tasks == set()
    assert run.running_tasks == set()
    assert run.failed_tasks == {"pending-task", "submitted-task", "running-task"}
    assert all(error["error_type"] == "canceled" for error in run.task_errors.values())
    assert terminal["type"] == "cancel_dynamic_run"
    assert run.event_seq == 2


def test_dynamic_event_http_validation_error_is_400_without_append(
    tmp_path,
    monkeypatch,
):
    from maze.core import server

    path, run = _dynamic_path(tmp_path)
    asyncio.run(_seed_dynamic_run(path, run.run_id))

    class Request:
        async def json(self):
            return {"type": "custom", "data": {}, "seq": 100}

    monkeypatch.setattr(server, "mapath", path)
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.emit_dynamic_run_event(run.run_id, Request()))

    assert error.value.status_code == 400
    assert run.event_seq == 1
    assert [event["seq"] for event in path.dynamic_run_store.load_events(run.run_id)] == [1]


def test_static_run_overwrites_caller_event_envelope():
    run = StaticRun("static-run", "workflow", Workflow("workflow"))
    event = run.append_event({
        "type": "custom",
        "seq": 100,
        "timestamp": "attacker",
        "schema_version": 99,
        "data": {},
    })

    assert event["seq"] == 1
    assert event["schema_version"] == 1
    assert event["timestamp"] != "attacker"
