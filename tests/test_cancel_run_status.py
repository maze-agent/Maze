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
                "idempotency_initialization": {"status": "ready"},
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
                "idempotency_initialization": {"status": "cleanup_pending"},
            },
            {
                "status": "success",
                "run_id": "run",
                "run_status": "created",
                "initialization_status": "cleanup_pending",
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
