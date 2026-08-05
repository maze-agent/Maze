import asyncio

import pytest


class _Request:
    async def json(self):
        return {"reason": "user requested"}


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (
            {
                "run_id": "run",
                "status": "cancelled",
                "dispatch": {"status": "active"},
            },
            {
                "status": "success",
                "run_id": "run",
                "run_status": "cancelled",
            },
        ),
        (
            {
                "run_id": "run",
                "status": "created",
                "dispatch": {"status": "cleanup_pending"},
            },
            {
                "status": "success",
                "run_id": "run",
                "run_status": "created",
                "dispatch_status": "cleanup_pending",
            },
        ),
    ],
)
def test_cancel_run_reports_durable_static_status(
    tmp_path,
    monkeypatch,
    snapshot,
    expected,
):
    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    from maze.core import server

    class Path:
        dynamic_runs = {}

        def __init__(self):
            self.stop_calls = []

        async def stop_workflow(self, run_id):
            self.stop_calls.append(run_id)

        async def get_run_snapshot(self, run_id):
            assert run_id == snapshot["run_id"]
            return snapshot

    path = Path()
    monkeypatch.setattr(server, "mapath", path)

    assert asyncio.run(server.cancel_run("run", _Request())) == expected
    assert path.stop_calls == ["run"]


def test_v1_list_run_tasks_maps_canonical_static_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    from maze.core import server

    class Path:
        def get_static_run_snapshot(self, run_id):
            assert run_id == "run"
            return {
                "task_counts": {"total": 1},
                "task_nodes": {
                    "task": {
                        "status": "running",
                        "artifact_store": {
                            "capability": "secret",
                            "uri": "maze://runs/run/artifacts/file.txt",
                        },
                    },
                },
            }

    monkeypatch.setattr(server, "mapath", Path())

    assert asyncio.run(server.list_run_tasks("run")) == {
        "run_id": "run",
        "task_total": 1,
        "tasks": {
            "task": {
                "status": "running",
                "artifact_store": {"uri": "maze://runs/run/artifacts/file.txt"},
            },
        },
    }
