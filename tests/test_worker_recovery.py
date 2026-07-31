from types import SimpleNamespace

import pytest

from maze.core.scheduler.resource import Node, ResourceManager
from maze.core.scheduler import resource as resource_module
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
def restore_worker_registration_payload():
    previous = Worker._last_registration_payload
    Worker._last_registration_payload = None
    try:
        yield
    finally:
        Worker._last_registration_payload = previous


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


def test_join_ray_starts_worker_raylet_without_initializing_a_driver(monkeypatch):
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


def test_worker_caches_head_cluster_snapshot_without_starting_a_ray_driver(monkeypatch):
    monkeypatch.setattr(Worker, "_local_ip_for_target", staticmethod(lambda _: "10.0.0.2"))
    monkeypatch.setattr(
        Worker,
        "_send_get_request",
        staticmethod(
            lambda *_args, **_kwargs: {
                "status": "success",
                "cluster": {
                    "nodes": [],
                    "unregistered_ray_nodes": [
                        {
                            "node_id": "worker-new",
                            "node_ip": "10.0.0.2",
                            "alive": True,
                            "ray_resources": {
                                "CPU": 6,
                                "memory": 2048,
                                "GPU": 0,
                            },
                        }
                    ],
                },
            }
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "detect_worker_execution_capabilities",
        lambda: {"workspace_sandbox": True, "docker_sandbox": False},
    )
    monkeypatch.setattr(worker_module.ray, "init", lambda *_args, **_kwargs: pytest.fail("ray.init called"))

    payload = Worker._registration_payload_from_cluster(
        "10.0.0.1:8000",
        "10.0.0.1:6379",
        timeout=1,
    )

    assert payload == {
        "node_id": "worker-new",
        "node_ip": "10.0.0.2",
        "resources": {"cpu": 6, "cpu_mem": 2048, "gpu_resource": {}},
        "capabilities": {"workspace_sandbox": True, "docker_sandbox": False},
    }
    assert Worker._last_registration_payload is payload

    posted = {}

    def post(url=None, data=None, **_kwargs):
        assert url == "http://10.0.0.1:8000/start_worker"
        posted.update(data)
        return _registration_response(payload, "already_registered")

    monkeypatch.setattr(Worker, "_send_post_request", staticmethod(post))
    Worker._register_worker("10.0.0.1:8000", announce=False)
    assert posted == payload


def test_worker_raises_typed_error_for_machine_readable_cluster_mismatch(monkeypatch):
    payload = _registration_payload()
    Worker._last_registration_payload = payload
    mismatch = {
        "registration_status": "cluster_mismatch",
        "error_code": "ray_cluster_mismatch",
        "error": {
            "code": "ray_cluster_mismatch",
            "message": "worker node is not in this cluster",
        },
        "node_id": payload["node_id"],
        "node_ip": payload["node_ip"],
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


def test_runtime_reset_shuts_down_driver_and_force_stops_ray(monkeypatch):
    events = []
    monkeypatch.setattr(worker_module.ray, "is_initialized", lambda: True)
    monkeypatch.setattr(worker_module.ray, "shutdown", lambda: events.append("shutdown"))

    def run(command, **kwargs):
        events.append(command)
        assert kwargs["check"] is False
        assert kwargs["timeout"] == 30
        return SimpleNamespace(returncode=0, stdout="stopped", stderr="")

    monkeypatch.setattr(worker_module.subprocess, "run", run)

    Worker._reset_local_ray_runtime()
    assert events == ["shutdown", worker_module.build_ray_command("stop", "--force")]


def test_agent_does_not_reset_ray_for_an_unconfirmed_heartbeat_failure(monkeypatch):
    monkeypatch.setattr(worker_module.time, "sleep", lambda _seconds: None)
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


def test_agent_recovers_after_a_confirmed_cluster_mismatch(monkeypatch):
    mismatch = {
        "registration_status": "cluster_mismatch",
        "error": {"message": "old cluster"},
    }
    recoveries = []
    monkeypatch.setattr(worker_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        Worker,
        "_register_worker",
        staticmethod(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RayClusterMismatchError(mismatch))
        ),
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


def test_head_rejects_worker_node_not_alive_in_current_ray_cluster():
    manager = ResourceManager()
    manager._ray_node_index = lambda: {
        "head": {"Alive": True},
        "dead-worker": {"Alive": False},
    }

    result = manager.start_worker(
        "foreign-worker",
        "10.0.0.9",
        NODE_RESOURCES,
    )

    assert result["registration_status"] == "cluster_mismatch"
    assert result["error_code"] == "ray_cluster_mismatch"
    assert result["error"] == {
        "code": "ray_cluster_mismatch",
        "message": "Worker node is not alive in the current Maze Ray cluster",
        "worker_node_id": "foreign-worker",
        "current_cluster_node_ids": ["head"],
    }
    assert manager.nodes == {}


def test_ray_membership_query_failure_is_retryable_and_never_resets_worker(monkeypatch):
    manager = ResourceManager()
    monkeypatch.setattr(
        resource_module.ray,
        "nodes",
        lambda: (_ for _ in ()).throw(RuntimeError("gcs unavailable")),
    )

    result = manager.start_worker("worker-old", "10.0.0.2", NODE_RESOURCES)

    assert result["registration_status"] == "ray_cluster_unavailable"
    assert result["error_code"] == "ray_cluster_unavailable"
    assert manager.nodes == {}

    Worker._last_registration_payload = _registration_payload()
    monkeypatch.setattr(worker_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        Worker,
        "_send_post_request",
        staticmethod(lambda *_args, **_kwargs: {"status": "success", "worker": result}),
    )
    monkeypatch.setattr(
        Worker,
        "_connect_and_register",
        staticmethod(lambda _addr: {"registration": _registration_response(_registration_payload())}),
    )
    monkeypatch.setattr(
        Worker,
        "_recover_cluster_mismatch",
        staticmethod(lambda _addr: pytest.fail("Ray query failure reset the local runtime")),
    )

    Worker._agent_loop("10.0.0.1:8000", heartbeat_interval=0, stop_after_iterations=1)


def test_ray_membership_query_failure_does_not_create_a_lease(monkeypatch):
    manager = ResourceManager()
    manager.nodes["worker"] = Node(
        "worker",
        "10.0.0.2",
        NODE_RESOURCES,
        NODE_RESOURCES,
    )
    monkeypatch.setattr(
        resource_module.ray,
        "nodes",
        lambda: (_ for _ in ()).throw(RuntimeError("gcs unavailable")),
    )

    selection = manager.select_node(TASK_RESOURCES)

    assert not selection
    assert selection.decision["reason"] == "ray_cluster_unavailable"
    assert manager.active_leases == {}


def test_worker_registration_uses_ray_canonical_node_ip():
    manager = ResourceManager()
    manager._ray_node_index = lambda: {
        "worker": {
            "Alive": True,
            "NodeManagerAddress": "10.0.0.2",
        },
    }

    result = manager.start_worker("worker", "203.0.113.9", NODE_RESOURCES)

    assert result["registration_status"] == "created"
    assert result["node_ip"] == "10.0.0.2"
    assert manager.nodes["worker"].node_ip == "10.0.0.2"


def test_authoritative_cluster_snapshot_turns_stale_runtime_into_typed_mismatch(monkeypatch):
    clock = iter((0.0, 0.1, 1.1))
    monkeypatch.setattr(worker_module.time, "time", lambda: next(clock))
    monkeypatch.setattr(worker_module.time, "sleep", lambda _seconds: None)
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


def test_unavailable_cluster_snapshot_is_not_a_typed_mismatch(monkeypatch):
    clock = iter((0.0, 0.1, 1.1))
    monkeypatch.setattr(worker_module.time, "time", lambda: next(clock))
    monkeypatch.setattr(worker_module.time, "sleep", lambda _seconds: None)
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


def test_agent_recovers_typed_mismatch_discovered_during_reconnect(monkeypatch):
    mismatch = RayClusterMismatchError({
        "registration_status": "cluster_mismatch",
        "error": {"message": "stale local runtime"},
    })
    recoveries = []
    monkeypatch.setattr(worker_module.time, "sleep", lambda _seconds: None)
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


def test_new_registration_removes_dead_same_ip_ghost_without_reassigning_lease():
    manager = ResourceManager()
    manager.nodes["worker-old"] = Node(
        "worker-old",
        "10.0.0.2",
        NODE_RESOURCES,
        NODE_RESOURCES,
    )
    manager.running_task_counts["worker-old"] = 1
    manager.active_leases["lease-old"] = {
        "lease_id": "lease-old",
        "node_id": "worker-old",
        "reservation_kind": "task",
        "resources": TASK_RESOURCES,
        "gpu_id": None,
    }
    manager._ray_node_index = lambda: {
        "worker-old": {"Alive": False},
        "worker-new": {"Alive": True},
    }

    result = manager.start_worker(
        "worker-new",
        "10.0.0.2",
        NODE_RESOURCES,
    )

    assert result["registration_status"] == "created"
    assert result["removed_stale_node_ids"] == ["worker-old"]
    assert set(manager.nodes) == {"worker-new"}
    assert manager.running_task_counts == {"worker-new": 0}
    assert manager.active_leases["lease-old"]["node_id"] == "worker-old"


def _two_node_manager():
    manager = ResourceManager()
    for node_id, node_ip in (("worker-old", "10.0.0.2"), ("worker-new", "10.0.0.3")):
        manager.nodes[node_id] = Node(
            node_id,
            node_ip,
            NODE_RESOURCES,
            NODE_RESOURCES,
        )
        manager.running_task_counts[node_id] = 0
    manager._ray_node_index = lambda: {
        "worker-old": {"Alive": True},
        "worker-new": {"Alive": True},
    }
    return manager


def test_avoid_node_ids_rejects_failed_node_and_selects_another_node():
    manager = _two_node_manager()
    selection = manager.select_node(
        {**TASK_RESOURCES, "avoid_node_ids": ["worker-old"]},
        run_id="run",
        task_id="task",
        attempt=2,
    )

    assert selection.node_id == "worker-new"
    old_candidate = next(
        candidate
        for candidate in selection.decision["candidate_nodes"]
        if candidate["node_id"] == "worker-old"
    )
    assert old_candidate["reject_reasons"] == ["avoided_after_failure"]
    assert manager.active_leases[selection.lease_id]["node_id"] == "worker-new"


def test_avoid_node_ids_has_stable_reason_when_every_node_is_avoided():
    manager = _two_node_manager()
    selection = manager.select_node(
        {
            **TASK_RESOURCES,
            "avoid_node_ids": ["worker-old", "worker-new"],
        }
    )

    assert not selection
    assert selection.decision["reason"] == "avoided_after_failure"
    assert manager.active_leases == {}
