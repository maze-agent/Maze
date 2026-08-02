from types import SimpleNamespace

import pytest

from maze.core.worker import worker as worker_module
from maze.core.worker.worker import (
    WORKER_RECOVERY_TIMEOUT_SECONDS,
    RayClusterMismatchError,
    Worker,
)


def _payload(node_ip="192.0.2.10"):
    return {
        "node_id": "worker-node",
        "node_ip": node_ip,
        "resources": {"cpu": 2, "cpu_mem": 1024, "gpu_resource": {}},
        "capabilities": {"workspace_sandbox": True, "docker_sandbox": False},
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


def test_registration_accepts_and_caches_scheduler_canonical_ip(monkeypatch):
    payload = _payload()
    Worker._last_registration_payload = payload
    posted_ips = []

    def post(_url=None, data=None, **_kwargs):
        posted_ips.append(data["node_ip"])
        return {
            "status": "success",
            "worker": {
                "registration_status": "already_registered",
                "node_id": payload["node_id"],
                "node_ip": "10.0.0.2",
            },
        }

    monkeypatch.setattr(Worker, "_send_post_request", staticmethod(post))

    Worker._register_worker("10.0.0.1:8000", announce=False)
    Worker._register_worker("10.0.0.1:8000", announce=False)

    assert posted_ips == ["192.0.2.10", "10.0.0.2"]
    assert Worker._last_registration_payload["node_ip"] == "10.0.0.2"


@pytest.mark.parametrize(
    "non_authoritative_query",
    (
        {"status": "unavailable"},
        {"status": "unknown"},
        {},
    ),
    ids=("unavailable", "unknown", "missing"),
)
def test_membership_state_changes_do_not_reset_the_total_deadline(
    monkeypatch,
    non_authoritative_query,
):
    now = [0.0]
    request_count = [0]
    snapshots = iter((
        {
            "ray_query": {"status": "available"},
            "nodes": [{"node_id": "head", "node_ip": "10.0.0.1", "alive": True}],
            "unregistered_ray_nodes": [],
        },
        {
            "ray_query": non_authoritative_query,
            "nodes": [],
            "unregistered_ray_nodes": [],
        },
        {
            "ray_query": {"status": "available"},
            "nodes": [{"node_id": "head", "node_ip": "10.0.0.1", "alive": True}],
            "unregistered_ray_nodes": [],
        },
        {
            "ray_query": {"status": "available"},
            "nodes": [],
            "unregistered_ray_nodes": [],
        },
    ))
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        Worker,
        "_sleep",
        staticmethod(lambda _delay, _operation: now.__setitem__(0, now[0] + 0.25)),
    )
    monkeypatch.setattr(Worker, "_local_ip_for_target", staticmethod(lambda _addr: "10.0.0.2"))

    def get_cluster(*_args, **_kwargs):
        request_count[0] += 1
        return {"status": "success", "cluster": next(snapshots)}

    monkeypatch.setattr(
        Worker,
        "_send_get_request",
        staticmethod(get_cluster),
    )

    with pytest.raises(RayClusterMismatchError, match="did not appear"):
        Worker._registration_payload_from_cluster(
            "10.0.0.1:8000",
            "10.0.0.1:6379",
            timeout=1,
        )

    assert request_count[0] == 4


def test_reset_and_rejoin_share_one_enforced_sixty_second_budget(monkeypatch):
    now = [100.0]
    post_called = False
    Worker._last_registration_payload = _payload()
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: now[0])

    def reset():
        now[0] += WORKER_RECOVERY_TIMEOUT_SECONDS + 0.1

    def connect(_addr):
        Worker._send_post_request("http://10.0.0.1:8000/get_head_ray_port", retries=1)

    def post(*_args, **_kwargs):
        nonlocal post_called
        post_called = True
        return SimpleNamespace(status_code=200, json=lambda: {"status": "success"})

    monkeypatch.setattr(Worker, "_reset_local_ray_runtime", staticmethod(reset))
    monkeypatch.setattr(Worker, "_connect_and_register", staticmethod(connect))
    monkeypatch.setattr(worker_module.requests, "post", post)

    with pytest.raises(TimeoutError, match="60-second budget"):
        Worker._recover_cluster_mismatch("10.0.0.1:8000")

    assert not post_called
    assert Worker._recovery_deadline is None


def test_agent_heartbeat_wait_does_not_consume_the_recovery_budget(monkeypatch):
    deadlines_during_sleep = []
    deadlines_during_registration = []

    def sleep(delay, _operation):
        deadlines_during_sleep.append((delay, Worker._recovery_deadline))

    monkeypatch.setattr(Worker, "_sleep", staticmethod(sleep))
    monkeypatch.setattr(
        Worker,
        "_register_worker",
        staticmethod(
            lambda *_args, **_kwargs: deadlines_during_registration.append(
                Worker._recovery_deadline
            )
        ),
    )

    Worker._agent_loop(
        "10.0.0.1:8000",
        heartbeat_interval=WORKER_RECOVERY_TIMEOUT_SECONDS + 5,
        stop_after_iterations=1,
    )

    assert deadlines_during_sleep == [
        (WORKER_RECOVERY_TIMEOUT_SECONDS + 5, None),
    ]
    assert len(deadlines_during_registration) == 1
    assert deadlines_during_registration[0] is not None
    assert Worker._recovery_deadline is None
