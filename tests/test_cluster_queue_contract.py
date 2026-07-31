import importlib
import json
from argparse import Namespace
from types import SimpleNamespace

import pytest

from maze.cli import cli
from maze.core.path.path import MaPath
from maze.core.scheduler.scheduler import PriorityQueue, Scheduler


PRIVATE_TASK_LEASE_ID = "private-task-lease-id"
PRIVATE_INSTANCE_LEASE_ID = "private-instance-lease-id"
PRIVATE_MODEL_INSTANCE_ID = "private-model-instance-id"


@pytest.fixture
def aggregate_queue_snapshot():
    scheduler = object.__new__(Scheduler)
    scheduler.stopped_workflow_ids = set()
    scheduler.task_queue = PriorityQueue()
    scheduler.workflow_manager = SimpleNamespace(get_running_tasks=lambda: [])
    scheduler.resource_manager = SimpleNamespace(
        scheduling_policy="least-loaded",
        active_leases={
            PRIVATE_TASK_LEASE_ID: {
                "reservation_kind": "task",
                "dispatch_id": "private-dispatch-id",
            },
            PRIVATE_INSTANCE_LEASE_ID: {
                "reservation_kind": "instance",
                "instance_id": PRIVATE_MODEL_INSTANCE_ID,
            },
        },
    )

    snapshot = scheduler.get_queue_snapshot()
    assert snapshot["active_lease_count"] == 2
    assert snapshot["active_lease_counts_by_kind"] == {"instance": 1, "task": 1}
    return snapshot


def _assert_private_lease_inventory_is_absent(value):
    serialized = json.dumps(value, sort_keys=True)
    assert PRIVATE_TASK_LEASE_ID not in serialized
    assert PRIVATE_INSTANCE_LEASE_ID not in serialized
    assert PRIVATE_MODEL_INSTANCE_ID not in serialized


@pytest.mark.asyncio
async def test_mapath_cluster_queue_message_returns_aggregate_snapshot(
    aggregate_queue_snapshot,
):
    path = object.__new__(MaPath)
    path.scheduler_process = SimpleNamespace(
        is_alive=lambda: True,
        pid=4321,
        exitcode=None,
    )
    path.cluster_queue_requests = {}
    sent_messages = []

    def respond(message):
        sent_messages.append(message)
        request_id = message["data"]["request_id"]
        path.cluster_queue_requests[request_id].put_nowait(aggregate_queue_snapshot)

    path._send_scheduler_message = respond

    result = await path.get_cluster_queues(timeout=0.1)

    assert result == aggregate_queue_snapshot
    assert len(sent_messages) == 1
    assert sent_messages[0]["type"] == "get_cluster_queues"
    assert sent_messages[0]["data"]["request_id"]
    assert path.cluster_queue_requests == {}
    _assert_private_lease_inventory_is_absent(result)


@pytest.mark.asyncio
async def test_cluster_queue_api_returns_aggregate_snapshot(
    monkeypatch,
    tmp_path,
    aggregate_queue_snapshot,
):
    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    server = importlib.import_module("maze.core.server")

    async def get_cluster_queues():
        return aggregate_queue_snapshot

    monkeypatch.setattr(server.mapath, "get_cluster_queues", get_cluster_queues)

    response = await server.get_cluster_queues()

    assert response == {
        "status": "success",
        "queues": aggregate_queue_snapshot,
    }
    _assert_private_lease_inventory_is_absent(response)


def test_cluster_queue_cli_json_prints_aggregate_fields(
    monkeypatch,
    capsys,
    aggregate_queue_snapshot,
):
    payload = {"status": "success", "queues": aggregate_queue_snapshot}
    captured_request = {}

    def request_core(method, server_url, path):
        captured_request.update(method=method, server_url=server_url, path=path)
        return payload

    monkeypatch.setattr(cli, "_request_core", request_core)

    cli._print_cluster_queues(
        Namespace(server_url="http://maze:8000", json=True)
    )

    output = json.loads(capsys.readouterr().out)
    assert captured_request == {
        "method": "GET",
        "server_url": "http://maze:8000",
        "path": "/cluster/queues",
    }
    assert output["queues"]["active_lease_count"] == 2
    assert output["queues"]["active_lease_counts_by_kind"] == {
        "instance": 1,
        "task": 1,
    }
    _assert_private_lease_inventory_is_absent(output)


def test_cluster_queue_cli_text_prints_aggregate_fields(
    monkeypatch,
    capsys,
    aggregate_queue_snapshot,
):
    payload = {"status": "success", "queues": aggregate_queue_snapshot}
    monkeypatch.setattr(cli, "_request_core", lambda *_args, **_kwargs: payload)

    cli._print_cluster_queues(
        Namespace(server_url="http://maze:8000", json=False)
    )

    output = capsys.readouterr().out
    assert "leases=2" in output
    assert "Lease kinds: instance=1 task=1" in output
    assert PRIVATE_TASK_LEASE_ID not in output
    assert PRIVATE_INSTANCE_LEASE_ID not in output
    assert PRIVATE_MODEL_INSTANCE_ID not in output
