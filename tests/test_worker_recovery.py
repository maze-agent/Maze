from types import SimpleNamespace

import pytest

from maze.core.scheduler import resource as resource_module
from maze.core.scheduler.resource import Node, ResourceManager
from maze.core.worker import worker as worker_module
from maze.core.worker.worker import RayClusterMismatchError, Worker


NODE_RESOURCES = {
    "cpu": 4,
    "cpu_mem": 1024,
    "gpu_resource": {},
}
TASK_RESOURCES = {
    "cpu": 1,
    "cpu_mem": 128,
    "gpu": 0,
    "gpu_mem": 0,
}


@pytest.fixture(autouse=True)
def restore_worker_state():
    previous_payload = Worker._last_registration_payload
    previous_deadline = Worker._recovery_deadline
    Worker._last_registration_payload = None
    Worker._recovery_deadline = None
    try:
        yield
    finally:
        Worker._last_registration_payload = previous_payload
        Worker._recovery_deadline = previous_deadline


def _registration_payload(node_id="worker-old", node_ip="10.0.0.2"):
    return {
        "node_id": node_id,
        "node_ip": node_ip,
        "resources": NODE_RESOURCES,
        "capabilities": {
            "workspace_sandbox": True,
            "docker_sandbox": False,
        },
    }


def _registration_response(payload, status="created"):
    return {
        "status": "success",
        "worker": {
            "registration_status": status,
            "node_id": payload["node_id"],
            "node_ip": payload["node_ip"],
        },
    }


def test_join_ray_uses_the_active_python_environment_without_starting_a_driver(monkeypatch):
    commands = []
    monkeypatch.setattr(Worker, "_local_ip_for_target", staticmethod(lambda _: "10.0.0.2"))
    monkeypatch.setattr(Worker, "_local_ray_runtime_active", staticmethod(lambda: False))
    monkeypatch.setattr(worker_module.ray, "init", lambda *_args, **_kwargs: pytest.fail("ray.init called"))

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="started", stderr="")

    monkeypatch.setattr(worker_module.subprocess, "run", run)

    assert Worker._join_ray("10.0.0.1:8000", 6379) == "10.0.0.1:6379"
    assert commands == [[
        *worker_module.build_ray_command("start", "--address", "10.0.0.1:6379"),
        "--node-ip-address",
        "10.0.0.2",
    ]]


def test_registration_handles_mismatch_before_validating_returned_identity(monkeypatch):
    Worker._last_registration_payload = _registration_payload()
    mismatch = {
        "registration_status": "cluster_mismatch",
        "error_code": "ray_cluster_mismatch",
        "error": {
            "code": "ray_cluster_mismatch",
            "message": "worker node is not in this cluster",
        },
        "node_id": "worker-from-an-old-cluster",
        "node_ip": "192.0.2.20",
    }
    monkeypatch.setattr(
        Worker,
        "_send_post_request",
        staticmethod(lambda *_args, **_kwargs: {"status": "success", "worker": mismatch}),
    )

    with pytest.raises(RayClusterMismatchError, match="not in this cluster") as exc_info:
        Worker._register_worker("10.0.0.1:8000", announce=False)

    assert exc_info.value.worker is mismatch


def test_confirmed_mismatch_resets_runtime_before_rejoining(monkeypatch):
    events = []
    Worker._last_registration_payload = _registration_payload()
    monkeypatch.setattr(
        Worker,
        "_reset_local_ray_runtime",
        staticmethod(lambda: events.append("reset")),
    )

    def connect(_addr):
        assert Worker._last_registration_payload is None
        events.append("connect")
        return {"registration": _registration_response(_registration_payload("worker-new"))}

    monkeypatch.setattr(Worker, "_connect_and_register", staticmethod(connect))

    Worker._recover_cluster_mismatch("10.0.0.1:8000")
    assert events == ["reset", "connect"]


def test_agent_does_not_reset_ray_for_an_unconfirmed_heartbeat_failure(monkeypatch):
    monkeypatch.setattr(Worker, "_sleep", staticmethod(lambda *_args: None))
    monkeypatch.setattr(
        Worker,
        "_register_worker",
        staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network down"))),
    )
    monkeypatch.setattr(
        Worker,
        "_connect_and_register",
        staticmethod(lambda _addr: {"registration": _registration_response(_registration_payload())}),
    )
    monkeypatch.setattr(
        Worker,
        "_recover_cluster_mismatch",
        staticmethod(lambda _addr: pytest.fail("unconfirmed failure reset Ray")),
    )

    Worker._agent_loop("10.0.0.1:8000", heartbeat_interval=0, stop_after_iterations=1)


def test_agent_recovers_typed_mismatch_discovered_during_reconnect(monkeypatch):
    mismatch = RayClusterMismatchError({
        "registration_status": "cluster_mismatch",
        "error": {"message": "stale local runtime"},
    })
    recoveries = []
    monkeypatch.setattr(Worker, "_sleep", staticmethod(lambda *_args: None))
    monkeypatch.setattr(
        Worker,
        "_register_worker",
        staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("heartbeat lost"))),
    )
    monkeypatch.setattr(
        Worker,
        "_connect_and_register",
        staticmethod(lambda _addr: (_ for _ in ()).throw(mismatch)),
    )
    monkeypatch.setattr(
        Worker,
        "_recover_cluster_mismatch",
        staticmethod(
            lambda addr: recoveries.append(addr)
            or {"registration": _registration_response(_registration_payload("worker-new"))}
        ),
    )

    Worker._agent_loop("10.0.0.1:8000", heartbeat_interval=0, stop_after_iterations=1)
    assert recoveries == ["10.0.0.1:8000"]


def test_authoritative_cluster_snapshot_turns_missing_worker_into_typed_mismatch(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        worker_module.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    monkeypatch.setattr(Worker, "_local_ip_for_target", staticmethod(lambda _addr: "10.0.0.2"))
    monkeypatch.setattr(
        Worker,
        "_send_get_request",
        staticmethod(lambda *_args, **_kwargs: {
            "status": "success",
            "cluster": {
                "ray_query": {"status": "available"},
                "nodes": [{"node_id": "head", "node_ip": "10.0.0.1", "alive": True}],
                "unregistered_ray_nodes": [],
            },
        }),
    )

    with pytest.raises(RayClusterMismatchError, match="did not appear"):
        Worker._registration_payload_from_cluster(
            "10.0.0.1:8000",
            "10.0.0.1:6379",
            timeout=1,
        )


def test_unavailable_cluster_snapshot_remains_retryable_not_mismatch(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        worker_module.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    monkeypatch.setattr(Worker, "_local_ip_for_target", staticmethod(lambda _addr: "10.0.0.2"))
    monkeypatch.setattr(
        Worker,
        "_send_get_request",
        staticmethod(lambda *_args, **_kwargs: {
            "status": "success",
            "cluster": {
                "ray_query": {"status": "unavailable"},
                "nodes": [],
                "unregistered_ray_nodes": [],
            },
        }),
    )

    with pytest.raises(RuntimeError, match="membership remained unavailable") as exc_info:
        Worker._registration_payload_from_cluster(
            "10.0.0.1:8000",
            "10.0.0.1:6379",
            timeout=1,
        )
    assert not isinstance(exc_info.value, RayClusterMismatchError)


def test_ray_query_failure_is_typed_and_never_creates_registration_or_lease(monkeypatch):
    manager = ResourceManager()
    manager.nodes["worker"] = Node(
        "worker",
        "10.0.0.2",
        NODE_RESOURCES,
        NODE_RESOURCES,
    )
    manager.running_task_counts["worker"] = 0
    monkeypatch.setattr(
        resource_module.ray,
        "nodes",
        lambda: (_ for _ in ()).throw(RuntimeError("gcs unavailable")),
    )

    cluster = manager.get_cluster_resources()
    assert cluster["ray_query"] == {
        "status": "unavailable",
        "error_code": "ray_cluster_unavailable",
    }
    assert cluster["nodes"][0]["alive"] is None
    assert manager.check_dead_node() is False
    assert "worker" in manager.nodes

    registration = manager.start_worker("worker-new", "10.0.0.3", NODE_RESOURCES)
    assert registration["registration_status"] == "ray_cluster_unavailable"
    assert "worker-new" not in manager.nodes

    selection = manager.select_node(TASK_RESOURCES)
    assert not selection
    assert selection.decision["reason"] == "ray_cluster_unavailable"
    assert manager.active_leases == {}


def test_worker_rejoin_does_not_reassign_or_release_the_lost_nodes_lease():
    manager = ResourceManager()
    manager.nodes["worker-old"] = Node(
        "worker-old",
        "10.0.0.2",
        NODE_RESOURCES,
        NODE_RESOURCES,
    )
    manager.running_task_counts["worker-old"] = 0
    manager._ray_node_index = lambda: {
        "worker-old": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
    }
    selection = manager.select_node(
        TASK_RESOURCES,
        run_id="run",
        task_id="task",
        attempt=1,
        dispatch_id="dispatch-1",
    )
    assert selection

    manager._ray_node_index = lambda: {
        "worker-old": {"Alive": False, "NodeManagerAddress": "10.0.0.2"},
    }
    assert manager.check_dead_node() is True
    assert "worker-old" not in manager.nodes
    assert manager.active_leases[selection.lease_id]["node_id"] == "worker-old"

    manager._ray_node_index = lambda: {
        "worker-new": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
    }
    registration = manager.start_worker("worker-new", "203.0.113.9", NODE_RESOURCES)
    assert registration["node_ip"] == "10.0.0.2"
    assert manager.nodes["worker-new"].available_resources["cpu"] == 4

    assert manager.release_lease(selection.lease_id)
    assert manager.nodes["worker-new"].available_resources["cpu"] == 4
    assert manager.running_task_counts["worker-new"] == 0


def test_same_node_rejoin_restores_running_count_from_retained_leases():
    worker_resources = {
        "cpu": 8,
        "cpu_mem": 2048,
        "gpu_resource": {},
    }
    manager = ResourceManager()
    manager.nodes["worker"] = Node(
        "worker",
        "10.0.0.2",
        worker_resources,
        worker_resources,
    )
    manager.running_task_counts["worker"] = 0
    manager._ray_node_index = lambda: {
        "worker": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
    }
    first = manager.select_node(TASK_RESOURCES)
    second = manager.select_node(TASK_RESOURCES)
    assert first and second

    manager._ray_node_index = lambda: {}
    assert manager.check_dead_node() is True
    assert "worker" not in manager.nodes
    assert "worker" not in manager.running_task_counts

    manager.nodes["idle-peer"] = Node(
        "idle-peer",
        "10.0.0.3",
        NODE_RESOURCES,
        NODE_RESOURCES,
    )
    manager.running_task_counts["idle-peer"] = 0
    manager._ray_node_index = lambda: {
        "worker": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
        "idle-peer": {"Alive": True, "NodeManagerAddress": "10.0.0.3"},
    }
    registration = manager.start_worker("worker", "10.0.0.2", worker_resources)

    assert registration["registration_status"] == "created"
    assert manager.nodes["worker"].available_resources["cpu"] == 6
    assert manager.running_task_counts["worker"] == 2

    manager.set_scheduling_policy("least-loaded")
    selection = manager.select_node(TASK_RESOURCES)
    assert selection.node_id == "idle-peer"


def test_release_after_capacity_shrink_is_clamped_and_removed_gpu_is_ignored():
    old_resources = {
        "cpu": 4,
        "cpu_mem": 4096,
        "gpu_resource": {
            0: {"gpu_id": 0, "gpu_mem": 8192, "gpu_num": 1},
        },
    }
    manager = ResourceManager()
    manager.nodes["worker"] = Node(
        "worker",
        "10.0.0.2",
        old_resources,
        old_resources,
    )
    manager.running_task_counts["worker"] = 0
    manager._ray_node_index = lambda: {
        "worker": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
    }
    selection = manager.select_node({
        "cpu": 2,
        "cpu_mem": 512,
        "gpu": 1,
        "gpu_mem": 1024,
    })
    assert selection

    new_resources = {
        "cpu": 1,
        "cpu_mem": 256,
        "gpu_resource": {},
    }
    assert manager.nodes["worker"].update_registration(
        "10.0.0.2",
        new_resources,
    ) == "updated"

    assert manager.release_lease(selection.lease_id)
    assert manager.nodes["worker"].available_resources == new_resources
    assert manager.running_task_counts["worker"] == 0
    assert not manager.release_lease(selection.lease_id)


def test_release_after_cpu_shrink_accounts_for_all_remaining_leases():
    manager = ResourceManager()
    manager.nodes["worker"] = Node(
        "worker",
        "10.0.0.2",
        NODE_RESOURCES,
        NODE_RESOURCES,
    )
    manager.running_task_counts["worker"] = 0
    manager._ray_node_index = lambda: {
        "worker": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
    }
    lease_resources = {
        "cpu": 2,
        "cpu_mem": 128,
        "gpu": 0,
        "gpu_mem": 0,
    }
    first = manager.select_node(lease_resources)
    second = manager.select_node(lease_resources)
    assert first and second

    shrunken_resources = {
        "cpu": 1,
        "cpu_mem": 128,
        "gpu_resource": {},
    }
    registration = manager.start_worker(
        "worker",
        "10.0.0.2",
        shrunken_resources,
    )
    assert registration["registration_status"] == "updated"

    assert manager.release_lease(first.lease_id)
    assert manager.nodes["worker"].available_resources["cpu"] == 0
    assert manager.running_task_counts["worker"] == 1
    pending = manager.select_node({
        "cpu": 1,
        "cpu_mem": 0,
        "gpu": 0,
        "gpu_mem": 0,
    })
    assert not pending
    assert pending.decision["reason"] == "insufficient_cpu"

    assert manager.release_lease(second.lease_id)
    assert manager.nodes["worker"].available_resources["cpu"] == 1
    assert manager.running_task_counts["worker"] == 0


def test_gpu_reappearance_keeps_active_lease_reservation_debt():
    gpu_resources = {
        "cpu": 4,
        "cpu_mem": 1024,
        "gpu_resource": {
            0: {"gpu_id": 0, "gpu_mem": 8192, "gpu_num": 1},
        },
    }
    manager = ResourceManager()
    manager.nodes["worker"] = Node(
        "worker",
        "10.0.0.2",
        gpu_resources,
        gpu_resources,
    )
    manager.running_task_counts["worker"] = 0
    manager._ray_node_index = lambda: {
        "worker": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
    }
    selection = manager.select_node(
        {
            "cpu": 0,
            "cpu_mem": 0,
            "gpu": 1,
            "gpu_mem": 1024,
        },
        reservation_kind="instance",
    )
    assert selection

    without_gpu = {
        "cpu": 4,
        "cpu_mem": 1024,
        "gpu_resource": {},
    }
    assert manager.start_worker(
        "worker",
        "10.0.0.2",
        without_gpu,
    )["registration_status"] == "updated"
    assert manager.nodes["worker"].available_resources["gpu_resource"] == {}

    assert manager.start_worker(
        "worker",
        "10.0.0.2",
        gpu_resources,
    )["registration_status"] == "updated"
    available_gpu = manager.nodes["worker"].available_resources["gpu_resource"][0]
    assert available_gpu["gpu_num"] == 0
    assert available_gpu["gpu_mem"] == 7168

    assert manager.release_lease(selection.lease_id)
    assert manager.nodes["worker"].available_resources == gpu_resources


def test_authoritative_snapshot_omission_cleans_registered_dead_node():
    manager = ResourceManager()
    manager.nodes["worker"] = Node(
        "worker",
        "10.0.0.2",
        NODE_RESOURCES,
        NODE_RESOURCES,
    )
    manager.running_task_counts["worker"] = 0
    manager._ray_node_index = lambda: {
        "worker": {"Alive": True, "NodeManagerAddress": "10.0.0.2"},
    }
    selection = manager.select_node(TASK_RESOURCES)
    assert selection
    manager.disabled_node_ids.add("worker")
    released_contexts = []
    manager.dag_context_manager.release_node_contexts = released_contexts.append
    manager._ray_node_index = lambda: {}

    assert manager.check_dead_node() is True
    assert "worker" not in manager.nodes
    assert "worker" not in manager.running_task_counts
    assert "worker" not in manager.disabled_node_ids
    assert released_contexts == ["worker"]
    assert selection.lease_id in manager.active_leases
    assert manager.release_lease(selection.lease_id)
