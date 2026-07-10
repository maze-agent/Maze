from __future__ import annotations

from maze.core.scheduler.standby_worker import (
    STANDBY_WORKER_RESOURCE_OPTIONS,
    StandbyWorkerPoolManager,
)


def test_standby_worker_resource_options_are_zero_vram():
    assert STANDBY_WORKER_RESOURCE_OPTIONS["num_gpus"] == 0
    assert STANDBY_WORKER_RESOURCE_OPTIONS["num_cpus"] > 0


def test_standby_worker_pool_maintains_per_node_targets():
    created = []

    def fake_actor_factory(node_id, worker_type):
        actor = {"node_id": node_id, "worker_type": worker_type, "index": len(created)}
        created.append(actor)
        return actor

    manager = StandbyWorkerPoolManager(
        pool_sizes={"gpu": 2, "cpu": 1, "io": 0},
        actor_factory=fake_actor_factory,
    )

    manager.ensure_for_nodes({"node-a": object()})
    manager.ensure_for_nodes({"node-a": object()})

    assert len(created) == 3
    assert manager.snapshot()["nodes"]["node-a"] == {"gpu": 2, "cpu": 1, "io": 0}


def test_standby_worker_pool_removes_stale_nodes():
    created = []

    def fake_actor_factory(node_id, worker_type):
        actor = {"node_id": node_id, "worker_type": worker_type, "index": len(created)}
        created.append(actor)
        return actor

    manager = StandbyWorkerPoolManager(
        pool_sizes={"gpu": 1},
        actor_factory=fake_actor_factory,
        actor_killer=lambda actor: None,
    )

    manager.ensure_for_nodes({"node-a": object(), "node-b": object()})
    manager.ensure_for_nodes({"node-b": object()})

    snapshot = manager.snapshot()
    assert "node-a" not in snapshot["nodes"]
    assert snapshot["nodes"]["node-b"] == {"gpu": 1}


def test_standby_worker_pool_acquire_release_tracks_busy_state():
    created = []

    def fake_actor_factory(node_id, worker_type):
        actor = {"node_id": node_id, "worker_type": worker_type, "index": len(created)}
        created.append(actor)
        return actor

    manager = StandbyWorkerPoolManager(
        pool_sizes={"cpu": 1},
        execution_enabled=True,
        actor_factory=fake_actor_factory,
    )

    manager.ensure_for_nodes({"node-a": object()})

    lease = manager.acquire("node-a", "cpu")
    assert lease is not None
    assert lease.actor == created[0]
    assert manager.acquire("node-a", "cpu") is None

    execution = manager.snapshot()["execution"]["nodes"]["node-a"]["cpu"]
    assert execution == {"total": 1, "busy": 1, "idle": 0}

    manager.release(lease)
    execution = manager.snapshot()["execution"]["nodes"]["node-a"]["cpu"]
    assert execution == {"total": 1, "busy": 0, "idle": 1}
    assert manager.acquire("node-a", "cpu") is not None


def test_standby_worker_pool_does_not_acquire_when_execution_disabled():
    manager = StandbyWorkerPoolManager(
        pool_sizes={"cpu": 1},
        execution_enabled=False,
        actor_factory=lambda node_id, worker_type: object(),
    )
    manager.ensure_for_nodes({"node-a": object()})

    assert manager.acquire("node-a", "cpu") is None
