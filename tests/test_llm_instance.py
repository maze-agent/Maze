from concurrent.futures import Future
import multiprocessing
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from maze.core.scheduler import llm_instance
from maze.core.scheduler.llm_instance import (
    LlmInstanceManager,
    build_transformers_command,
    build_vllm_command,
)


RESOURCES = {"cpu": 1, "cpu_mem": 1024, "gpu": 1, "gpu_mem": 0}
NODE_ID = "1" * 56


def reserve_port_in_process(
    start_port,
    reservation_root,
    start_event,
    result_queue,
    release_event,
    release_explicitly,
):
    start_event.wait(5)
    descriptor = None
    try:
        port, descriptor = llm_instance._reserve_llm_port(
            start_port,
            reservation_root,
        )
        result_queue.put(("ok", port))
        release_event.wait(5)
        if release_explicitly:
            llm_instance._release_port_reservation(descriptor)
            descriptor = None
    except BaseException as exc:
        result_queue.put(("error", repr(exc)))
    finally:
        if release_explicitly and descriptor is not None:
            llm_instance._release_port_reservation(descriptor)


def background_workers_after_fork(result_queue):
    try:
        LlmInstanceManager()
        executors = (
            llm_instance._ACTOR_CREATION_EXECUTOR,
            llm_instance._LATE_ACTOR_KILL_EXECUTOR,
            llm_instance._RAY_CONTROL_EXECUTOR,
        )
        worker_pids = [
            executor.submit(os.getpid).result(timeout=2)
            for executor in executors
        ]
        result_queue.put(("ok", worker_pids))
    except BaseException as exc:
        result_queue.put(("error", repr(exc)))


class FakeRef:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error


class FakeRemoteMethod:
    def __init__(self, name, events, value=None, error=None):
        self.name = name
        self.events = events
        self.value = value
        self.error = error

    def remote(self, *args, **kwargs):
        self.events.append((self.name, args, kwargs))
        return FakeRef(self.value, self.error)


class FakeActor:
    def __init__(self, events, *, backend="vllm", process_error=None):
        self.launch_server = FakeRemoteMethod(
            "launch",
            events,
            value={
                "port": "8123",
                "process_group_id": 4321,
                "backend": backend,
            },
        )
        self.get_process_status = FakeRemoteMethod(
            "status",
            events,
            value={"return_code": None, "ready": False},
            error=process_error,
        )
        self.mark_ready = FakeRemoteMethod("ready", events, value=True)
        self.stop_server = FakeRemoteMethod("stop", events, value=True)


def fake_ray_get(ref, timeout=None):
    if ref.error is not None:
        raise ref.error
    return ref.value


def install_fake_runtime(
    monkeypatch,
    events,
    *,
    process_error=None,
    cleanup_error=None,
):
    class FakeActorOptions:
        def remote(self, **kwargs):
            events.append(("actor", (), kwargs))
            return FakeActor(
                events,
                backend=kwargs["backend"],
                process_error=process_error,
            )

    class FakeLLMServerActor:
        @classmethod
        def options(cls, **kwargs):
            events.append(("options", (), kwargs))
            return FakeActorOptions()

    class FakeCleanupTask:
        @classmethod
        def options(cls, **kwargs):
            events.append(("cleanup_options", (), kwargs))
            return cls

        @classmethod
        def remote(cls, instance_id, generation_id=None):
            events.append(("cleanup", (instance_id, generation_id), {}))
            return FakeRef(
                {"stopped_process_groups": [4321]},
                cleanup_error,
            )

    class FakeResponse:
        status_code = 200

        def __init__(self, payload=None):
            self.payload = payload or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, timeout):
        events.append(("GET", url, timeout))
        if url.endswith("/v1/models"):
            return FakeResponse({"data": [{"id": "/models/qwen"}]})
        return FakeResponse()

    def fake_post(url, json, timeout):
        events.append(("POST", url, timeout, json))
        return FakeResponse({"choices": [{"message": {"content": "READY"}}]})

    monkeypatch.setattr(llm_instance, "LLMServerActor", FakeLLMServerActor)
    monkeypatch.setattr(
        llm_instance,
        "stop_llm_instance_processes",
        FakeCleanupTask,
    )
    monkeypatch.setattr(llm_instance.ray, "get", fake_ray_get)
    monkeypatch.setattr(
        llm_instance.ray,
        "nodes",
        lambda: [{
            "NodeID": NODE_ID,
            "NodeManagerAddress": "127.0.0.1",
            "Alive": True,
        }],
    )
    monkeypatch.setattr(llm_instance.requests, "get", fake_get)
    monkeypatch.setattr(llm_instance.requests, "post", fake_post)
    monkeypatch.setattr(
        llm_instance.ray,
        "kill",
        lambda actor, **kwargs: events.append(("kill", (actor,), kwargs)),
    )
    monkeypatch.setattr(
        llm_instance,
        "_confirm_actor_terminated",
        lambda actor, deadline, operation: None,
    )


def manager_start(manager, **kwargs):
    return manager.start_llm_instance(
        instance_id=kwargs.pop("instance_id", "instance-1"),
        model=kwargs.pop("model", "/models/qwen"),
        node_ip="127.0.0.1",
        node_id=NODE_ID,
        gpu_id=0,
        resources=RESOURCES,
        lease_id=kwargs.pop("lease_id", "lease-1"),
        **kwargs,
    )


def _capture_error(errors, function):
    try:
        function()
    except BaseException as exc:
        errors.append(exc)


def install_control_executor(monkeypatch, name):
    executor = llm_instance._BoundedDaemonExecutor(
        1,
        2,
        name,
        max_abandoned=1,
    )
    monkeypatch.setattr(llm_instance, "_RAY_CONTROL_EXECUTOR", executor)
    return executor


def wait_for_executor_retirement(executor, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with executor._state_lock:
            if not executor._abandoned_futures:
                return
        time.sleep(0.01)
    pytest.fail("timed-out daemon control worker did not retire")


@pytest.mark.parametrize(
    ("backend", "backend_args", "expected"),
    [
        (None, None, ("vllm", {})),
        (" VLLM ", {"max_model_len": 2048}, ("vllm", {"max_model_len": 2048})),
        ("transformers", None, ("transformers", {})),
    ],
)
def test_validate_model_backend(backend, backend_args, expected):
    assert llm_instance.validate_model_backend(backend, backend_args) == expected


def test_validate_model_backend_rejects_invalid_configuration():
    with pytest.raises(ValueError, match="Unsupported model backend"):
        llm_instance.validate_model_backend("other")
    with pytest.raises(ValueError, match="does not support vLLM arguments"):
        llm_instance.validate_model_backend(
            "transformers",
            {"max_model_len": 2048},
        )


def test_vllm_command_uses_current_interpreter_and_formats_options():
    command = build_vllm_command(
        "/models/qwen",
        "0.0.0.0",
        "8123",
        {
            "gpu_memory_utilization": 0.8,
            "max_model_len": 4096,
            "trust_remote_code": True,
            "disabled": False,
        },
    )

    assert command[:3] == [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
    ]
    assert command[-5:] == [
        "--gpu-memory-utilization",
        "0.8",
        "--max-model-len",
        "4096",
        "--trust-remote-code",
    ]
    assert "--disabled" not in command


def test_transformers_command_uses_current_environment_server():
    assert build_transformers_command("/models/qwen", "0.0.0.0", "8123") == [
        os.path.join(os.path.dirname(sys.executable), "transformers"),
        "serve",
        "/models/qwen",
        "--host",
        "0.0.0.0",
        "--port",
        "8123",
        "--device",
        "cuda:0",
        "--dtype",
        "auto",
    ]


def test_model_env_keeps_current_environment_and_adds_markers(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    env = llm_instance.build_model_env(
        "2",
        instance_id="instance-1",
        owner_id="owner-1",
        generation_id="generation-1",
    )

    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(sys.executable)
    assert env["CUDA_VISIBLE_DEVICES"] == "2"
    assert env[llm_instance.LLM_INSTANCE_ENV_VAR] == "instance-1"
    assert env[llm_instance.LLM_GENERATION_ENV_VAR] == "generation-1"
    assert env[llm_instance.LLM_OWNER_ENV_VAR] == "owner-1"


def test_instance_cleanup_targets_generation_marker_and_cache(monkeypatch):
    marker_queries = []
    cleaned_caches = []

    monkeypatch.setattr(
        llm_instance,
        "_generation_process_groups",
        lambda generation_id: marker_queries.append(generation_id) or {4321},
    )
    monkeypatch.setattr(
        llm_instance,
        "_instance_process_groups",
        lambda instance_id: pytest.fail("generation cleanup must not use instance ID"),
    )
    monkeypatch.setattr(
        llm_instance,
        "_stop_marked_process_groups",
        lambda process_groups, timeout, settle_timeout: process_groups(),
    )
    monkeypatch.setattr(
        llm_instance,
        "cleanup_transformers_cache",
        lambda cache_key: cleaned_caches.append(cache_key),
    )

    result = llm_instance.stop_llm_instance_processes._function(
        "instance-1",
        generation_id="generation-1",
    )

    assert result == {"stopped_process_groups": [4321]}
    assert marker_queries == ["generation-1"]
    assert cleaned_caches == ["generation-1"]


def test_actor_launches_selected_backend_with_marked_process(monkeypatch):
    captured = {}

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

    def fake_launch(command, env, owner_state_file=None):
        captured.update(
            command=command,
            env=env,
            owner_state_file=owner_state_file,
        )
        return FakeProcess()

    monkeypatch.setattr(llm_instance, "_launch_model_subprocess", fake_launch)
    actor_class = llm_instance.LLMServerActor.__ray_metadata__.modified_class
    actor = actor_class(
        model="/models/qwen",
        gpu_id=0,
        instance_id="instance-1",
        backend="vllm",
        backend_args={"max_model_len": 2048},
        owner_id=None,
    )

    launch = actor.launch_server()
    actor.mark_ready()

    assert captured["command"][:3] == [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
    ]
    assert captured["env"][llm_instance.LLM_INSTANCE_ENV_VAR] == "instance-1"
    assert captured["env"][llm_instance.LLM_GENERATION_ENV_VAR] == "instance-1"
    assert launch == {
        "port": actor.port,
        "process_group_id": 4321 if os.name == "posix" else None,
        "backend": "vllm",
    }
    assert actor._port_reservation_fd is None


@pytest.mark.skipif(
    llm_instance.fcntl is None or os.name != "posix",
    reason="requires POSIX flock",
)
def test_concurrent_actor_instances_reserve_different_ports(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    actor_class = llm_instance.LLMServerActor.__ray_metadata__.modified_class
    barrier = threading.Barrier(3)
    actors = []
    errors = []

    def create_actor(instance_id):
        barrier.wait()
        try:
            actors.append(
                actor_class(
                    model="model",
                    gpu_id=0,
                    instance_id=instance_id,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=create_actor, args=(f"instance-{index}",))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2)

    try:
        assert errors == []
        assert len(actors) == 2
        assert len({actor.port for actor in actors}) == 2
        assert all(actor._port_reservation_fd is not None for actor in actors)
    finally:
        for actor in actors:
            actor.stop_server()


@pytest.mark.skipif(
    llm_instance.fcntl is None or os.name != "posix",
    reason="requires POSIX flock",
)
def test_port_reservation_is_cross_process_and_reusable(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("0.0.0.0", 0))
        start_port = probe.getsockname()[1]

    reservation_root = tmp_path / "port-reservations"
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    release_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=reserve_port_in_process,
            args=(
                start_port,
                str(reservation_root),
                start_event,
                result_queue,
                release_event,
                release_explicitly,
            ),
        )
        for release_explicitly in (True, False)
    ]
    for process in processes:
        process.start()
    start_event.set()
    try:
        results = [result_queue.get(timeout=10) for _ in processes]
        assert all(status == "ok" for status, _ in results), results
        reserved_ports = [port for _, port in results]
        assert len(set(reserved_ports)) == 2
        assert str(start_port) in reserved_ports
    finally:
        release_event.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(2)
        result_queue.close()
        result_queue.join_thread()

    assert all(process.exitcode == 0 for process in processes)
    reused_port, descriptor = llm_instance._reserve_llm_port(
        start_port,
        reservation_root,
    )
    try:
        assert reused_port == str(start_port)
    finally:
        llm_instance._release_port_reservation(descriptor)

    explicitly_reused_port, descriptor = llm_instance._reserve_llm_port(
        start_port,
        reservation_root,
    )
    try:
        assert explicitly_reused_port == str(start_port)
    finally:
        llm_instance._release_port_reservation(descriptor)


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="requires multiprocessing fork",
)
def test_background_workers_restart_after_fork():
    context = multiprocessing.get_context("fork")
    result_queue = context.Queue()
    process = context.Process(
        target=background_workers_after_fork,
        args=(result_queue,),
    )
    process.start()
    try:
        result = result_queue.get(timeout=5)
        process.join(5)
        assert process.exitcode == 0
        assert result == ("ok", [process.pid] * 3)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(2)
        result_queue.close()
        result_queue.join_thread()


@pytest.mark.parametrize("backend", ["vllm", "transformers"])
def test_readiness_checks_health_models_and_chat(monkeypatch, backend):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    actor = FakeActor(events, backend=backend)
    manager._register_starting_instance(
        "instance-1",
        actor,
        "/models/qwen",
        backend,
        NODE_ID,
        "10.0.0.2",
        0,
        RESOURCES,
        "lease-1",
    )

    manager._wait_until_ready(
        "instance-1",
        actor,
        "10.0.0.2",
        "8123",
        "/models/qwen",
        backend,
        {},
        timeout=1,
    )

    http_paths = [
        event[1].removeprefix("http://10.0.0.2:8123")
        for event in events
        if event[0] in {"GET", "POST"}
    ]
    assert http_paths == ["/health", "/v1/models", "/v1/chat/completions"]


@pytest.mark.parametrize(
    ("backend", "backend_args"),
    [
        ("vllm", {"gpu_memory_utilization": 0.8}),
        ("transformers", {}),
    ],
)
def test_manager_start_preserves_routing_metadata_and_compatibility(
    monkeypatch,
    backend,
    backend_args,
):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager(owner_id="owner-1")

    port = manager_start(
        manager,
        backend=backend,
        backend_args=backend_args,
    )

    assert port == "8123"
    info = manager.get_instance_info("instance-1")
    assert info == {
        "instance_id": "instance-1",
        "model": "/models/qwen",
        "backend": backend,
        "host": "127.0.0.1",
        "port": "8123",
        "endpoint": "http://127.0.0.1:8123/v1",
        "status": "ready",
    }
    detail = manager.get_instance_resource_detail("instance-1")
    assert detail["lease_id"] == "lease-1"
    assert detail["process_group_id"] == 4321
    assert manager.snapshot()["owner_nodes"] == {NODE_ID: "127.0.0.1"}

    first_route = manager.route_model_request(
        "workflow-1",
        {"local_model": "/models/qwen", "backend": backend},
    )
    manager.release_model_route(first_route)
    second_route = manager.route_model_request(
        "workflow-1",
        {"local_model": "/models/qwen", "backend": backend},
    )
    assert first_route["endpoint"] == "http://127.0.0.1:8123/v1"
    assert second_route["affinity_hit"] is True
    assert manager.snapshot()["instances"]["instance-1"][
        "total_routed_requests"
    ] == 2


def test_manager_can_return_structured_instance_info(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()

    result = manager_start(manager, return_info=True)

    assert result["instance_id"] == "instance-1"
    assert result["endpoint"] == "http://127.0.0.1:8123/v1"
    assert result["status"] == "ready"


def test_manager_launches_node_path_but_routes_by_logical_model(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()

    result = manager_start(
        manager,
        model="qwen-id",
        launch_model="/models/qwen",
        backend="transformers",
        return_info=True,
    )
    actor_event = next(event for event in events if event[0] == "actor")
    route = manager.route_model_request(
        "workflow-1",
        {"local_model": "qwen-id", "backend": "transformers"},
    )

    assert actor_event[2]["model"] == "/models/qwen"
    assert result["model"] == "qwen-id"
    assert result["served_model"] == "/models/qwen"
    assert route["model"] == "qwen-id"
    assert route["served_model"] == "/models/qwen"


def test_full_instance_waits_for_scale_out_instead_of_overrouting(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager(max_requests_per_instance=1)
    manager_start(manager)
    anchor = {"local_model": "/models/qwen", "backend": "vllm"}

    first_route = manager.route_model_request("workflow-1", anchor)
    second_route = manager.route_model_request("workflow-2", anchor)
    assert manager.route_model_request("workflow-2", anchor) is None

    assert first_route["inflight_requests"] == 1
    assert second_route is None
    assert manager.snapshot()["instances"]["instance-1"]["inflight_requests"] == 1
    assert manager.snapshot()["pending_model_requests"]["/models/qwen|vllm"] == 1
    recommendation = manager.scale_out_recommendations()[0]
    assert recommendation["reason"] == "pending_ratio_exceeded"

    manager.release_model_route(first_route)
    recovered_route = manager.route_model_request("workflow-2", anchor)
    assert recovered_route is not None
    assert manager.scale_out_recommendations() == []
    manager.clear_workflow_state("workflow-2")
    assert not any(
        key.startswith("workflow-2|")
        for key in manager.snapshot()["workflow_model_affinity"]
    )

    assert manager.route_model_request("workflow-3", anchor) is None
    manager.clear_workflow_state("workflow-3")
    assert manager.scale_out_recommendations() == []

    assert manager.route_model_request("workflow-4", anchor) is None
    recommendation = manager.scale_out_recommendations()[0]
    manager.mark_model_deploying(
        recommendation["model"],
        recommendation["backend"],
        instance_id="instance-2",
    )
    assert manager.scale_out_recommendations() == []


def test_stop_cannot_interleave_ready_check_and_route_registration(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    register_entered = threading.Event()
    allow_registration = threading.Event()
    stop_attempted = threading.Event()
    stop_finished = threading.Event()
    original_register = manager.register_instance
    start_errors = []
    stop_errors = []
    stopped_details = []

    def delayed_register(*args, **kwargs):
        register_entered.set()
        assert allow_registration.wait(2)
        return original_register(*args, **kwargs)

    monkeypatch.setattr(manager, "register_instance", delayed_register)

    def start():
        try:
            manager_start(manager)
        except Exception as exc:
            start_errors.append(exc)

    def stop():
        stop_attempted.set()
        try:
            stopped_details.append(
                manager.stop_llm_instance("instance-1", finalize=False)
            )
        except Exception as exc:
            stop_errors.append(exc)
        finally:
            stop_finished.set()

    start_thread = threading.Thread(target=start)
    stop_thread = threading.Thread(target=stop)
    start_thread.start()
    assert register_entered.wait(1)
    stop_thread.start()
    assert stop_attempted.wait(1)
    stop_interleaved = stop_finished.wait(0.2)
    allow_registration.set()
    start_thread.join(2)
    stop_thread.join(2)

    assert stop_interleaved is False
    assert start_errors == []
    assert stop_errors == []
    assert stopped_details[0]["lease_id"] == "lease-1"
    assert manager.get_instance_state("instance-1") == "stopped"
    assert manager.route_model_request(
        "workflow-1",
        {"local_model": "/models/qwen", "backend": "vllm"},
    ) is None
    assert [event[0] for event in events].count("stop") == 1
    assert [event[0] for event in events].count("kill") == 1
    assert [event[0] for event in events].count("cleanup") == 1
    assert manager.finalize_stopped_instance("instance-1") is True
    assert manager.finalize_stopped_instance("instance-1") is False


def test_route_and_scale_in_claim_are_atomic(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)
    route_in_critical_section = threading.Event()
    release_route = threading.Event()
    original_time = time.time

    def controlled_time():
        if threading.current_thread().name == "route-thread":
            route_in_critical_section.set()
            assert release_route.wait(2)
        return original_time()

    monkeypatch.setattr(llm_instance.time, "time", controlled_time)
    results = {}

    route_thread = threading.Thread(
        name="route-thread",
        target=lambda: results.setdefault(
            "route",
            manager.route_model_request(
                "workflow-1",
                {"local_model": "/models/qwen", "backend": "vllm"},
            ),
        ),
    )
    claim_thread = threading.Thread(
        name="claim-thread",
        target=lambda: results.setdefault(
            "claim",
            manager.claim_lru_scale_in("instance-1"),
        ),
    )
    route_thread.start()
    assert route_in_critical_section.wait(1)
    claim_thread.start()
    assert claim_thread.is_alive()
    release_route.set()
    route_thread.join(2)
    claim_thread.join(2)

    assert results["route"]["instance_id"] == "instance-1"
    assert results["claim"] is False
    assert manager.get_instance_state("instance-1") == "ready"


def test_lru_claim_rechecks_usage_after_advisory_selection(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager(idle_scale_in_seconds=1)
    manager_start(manager)
    metadata = manager.id_to_instance_metadata["instance-1"]
    metadata["created_time"] = 1

    candidates = manager.lru_scale_in_candidates(now=10, idle_seconds=1)
    route = manager.route_model_request(
        "workflow-1",
        {"local_model": "/models/qwen", "backend": "vllm"},
    )

    assert [item["instance_id"] for item in candidates] == ["instance-1"]
    assert route is not None
    assert manager.claim_lru_scale_in(
        "instance-1",
        expected_idle_since=candidates[0]["idle_since"],
        now=10,
    ) is False

    manager.release_model_route(route)
    assert manager.claim_lru_scale_in(
        "instance-1",
        expected_idle_since=candidates[0]["idle_since"],
        now=10,
    ) is False
    metadata["last_used_time"] = 1
    candidates = manager.lru_scale_in_candidates(now=10, idle_seconds=1)
    assert manager.claim_lru_scale_in(
        "instance-1",
        expected_idle_since=candidates[0]["idle_since"],
        now=10,
    ) is True
    assert manager.route_model_request(
        "workflow-2",
        {"local_model": "/models/qwen", "backend": "vllm"},
    ) is None


def test_concurrent_stop_takes_one_claim_without_double_cleanup(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager(idle_scale_in_seconds=1)
    manager_start(manager)
    manager.id_to_instance_metadata["instance-1"]["created_time"] = 1
    candidate = manager.lru_scale_in_candidates(now=10)[0]
    assert manager.claim_lru_scale_in(
        "instance-1",
        expected_idle_since=candidate["idle_since"],
        now=10,
    ) is True
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def stop():
        barrier.wait()
        try:
            results.append(
                manager.stop_llm_instance("instance-1", finalize=False)
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=stop) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(2)

    assert errors == []
    assert len(results) == 2
    assert [event[0] for event in events].count("stop") == 1
    assert [event[0] for event in events].count("kill") == 1
    assert [event[0] for event in events].count("cleanup") == 1
    assert manager.finalize_stopped_instance("instance-1") is True


def test_actor_launch_is_bounded_by_startup_timeout_and_cleans_up(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    get_timeouts = []

    def timeout_first_get(ref, timeout=None):
        get_timeouts.append(timeout)
        if len(get_timeouts) == 1:
            raise llm_instance.ray.exceptions.GetTimeoutError(
                "actor remained pending"
            )
        return fake_ray_get(ref, timeout=timeout)

    monkeypatch.setattr(llm_instance.ray, "get", timeout_first_get)
    manager = LlmInstanceManager()

    with pytest.raises(TimeoutError, match="actor launch timed out"):
        manager_start(manager, startup_timeout=0.1)

    assert 0 < get_timeouts[0] <= 0.1
    assert manager.get_instance_state("instance-1") == "stopped"
    assert {event[0] for event in events} >= {"stop", "kill", "cleanup"}


def test_blocked_actor_creation_times_out_without_holding_manager_lock(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    actor = FakeActor(events)
    remote_entered = threading.Event()
    release_remote = threading.Event()
    actor_killed = threading.Event()
    killed_actors = []

    class BlockingActorOptions:
        def remote(self, **kwargs):
            remote_entered.set()
            release_remote.wait(5)
            return actor

    class BlockingLLMServerActor:
        @classmethod
        def options(cls, **kwargs):
            return BlockingActorOptions()

    def kill(stale_actor, **kwargs):
        killed_actors.append(stale_actor)
        actor_killed.set()

    monkeypatch.setattr(llm_instance, "LLMServerActor", BlockingLLMServerActor)
    monkeypatch.setattr(llm_instance.ray, "kill", kill)
    manager = LlmInstanceManager()
    start_errors = []
    start_finished = threading.Event()

    def start():
        try:
            manager_start(manager, startup_timeout=0.05)
        except BaseException as exc:
            start_errors.append(exc)
        finally:
            start_finished.set()

    start_thread = threading.Thread(target=start)
    start_thread.start()
    try:
        assert remote_entered.wait(1)
        snapshot_started = time.monotonic()
        snapshot = manager.snapshot()
        assert time.monotonic() - snapshot_started < 0.2
        assert snapshot["instances"] == {}
        assert start_finished.wait(0.5)
        assert isinstance(start_errors[0], TimeoutError)
        assert manager.get_instance_state("instance-1") is None
        assert manager.has_instance("instance-1") is False
        assert release_remote.is_set() is False
    finally:
        release_remote.set()
        start_thread.join(1)

    assert actor_killed.wait(1)
    assert killed_actors == [actor]
    assert manager.route_model_request(
        "workflow-1",
        {"local_model": "/models/qwen", "backend": "vllm"},
    ) is None


def test_blocked_actor_creation_can_be_cancelled_and_shutdown_promptly(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    actor = FakeActor(events)
    remote_entered = threading.Event()
    release_remote = threading.Event()
    actor_killed = threading.Event()

    class BlockingActorOptions:
        def remote(self, **kwargs):
            remote_entered.set()
            release_remote.wait(5)
            return actor

    class BlockingLLMServerActor:
        @classmethod
        def options(cls, **kwargs):
            return BlockingActorOptions()

    monkeypatch.setattr(llm_instance, "LLMServerActor", BlockingLLMServerActor)
    monkeypatch.setattr(
        llm_instance.ray,
        "kill",
        lambda stale_actor, **kwargs: actor_killed.set(),
    )
    manager = LlmInstanceManager()
    start_errors = []
    start_thread = threading.Thread(
        target=lambda: _capture_error(
            start_errors,
            lambda: manager_start(manager, startup_timeout=5),
        )
    )
    start_thread.start()
    try:
        assert remote_entered.wait(1)
        cancel_started = time.monotonic()
        assert manager.request_start_cancellation("instance-1") == "creating"
        assert time.monotonic() - cancel_started < 0.2
        shutdown_started = time.monotonic()
        manager.begin_shutdown()
        assert time.monotonic() - shutdown_started < 0.2
        start_thread.join(0.5)
        assert start_thread.is_alive() is False
        assert isinstance(start_errors[0], RuntimeError)
        assert "cancelled" in str(start_errors[0])
        assert release_remote.is_set() is False
    finally:
        release_remote.set()
        start_thread.join(1)

    assert actor_killed.wait(1)
    assert manager.get_instance_state("instance-1") is None


def test_late_old_actor_does_not_kill_or_pollute_reused_instance_id(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    old_actor = FakeActor(events)
    new_actor = FakeActor(events)
    old_remote_entered = threading.Event()
    release_old_remote = threading.Event()
    old_actor_killed = threading.Event()
    creation_lock = threading.Lock()
    creation_count = 0
    killed_actors = []

    class GenerationActorOptions:
        def remote(self, **kwargs):
            nonlocal creation_count
            with creation_lock:
                creation_count += 1
                generation_number = creation_count
            if generation_number == 1:
                old_remote_entered.set()
                release_old_remote.wait(5)
                return old_actor
            return new_actor

    class GenerationLLMServerActor:
        @classmethod
        def options(cls, **kwargs):
            return GenerationActorOptions()

    def kill(stale_actor, **kwargs):
        killed_actors.append(stale_actor)
        old_actor_killed.set()

    monkeypatch.setattr(llm_instance, "LLMServerActor", GenerationLLMServerActor)
    monkeypatch.setattr(llm_instance.ray, "kill", kill)
    manager = LlmInstanceManager()
    first_errors = []
    first_thread = threading.Thread(
        target=lambda: _capture_error(
            first_errors,
            lambda: manager_start(manager, startup_timeout=0.05),
        )
    )
    first_thread.start()
    try:
        assert old_remote_entered.wait(1)
        first_thread.join(0.5)
        assert isinstance(first_errors[0], TimeoutError)

        assert manager_start(manager, startup_timeout=1) == "8123"
        assert manager.id_to_instance_actor["instance-1"] is new_actor
        assert manager.get_instance_state("instance-1") == "ready"
    finally:
        release_old_remote.set()
        first_thread.join(1)

    assert old_actor_killed.wait(1)
    assert killed_actors == [old_actor]
    assert manager.id_to_instance_actor["instance-1"] is new_actor
    assert manager.get_instance_state("instance-1") == "ready"


def test_late_actor_kill_retries_after_transient_ray_failure(monkeypatch):
    manager = LlmInstanceManager()
    generation = llm_instance._PendingActorStart(time.monotonic() + 1)
    cleanup_slots = llm_instance._BoundedCleanupSlots(1)
    assert generation.reserve_cleanup_slot(cleanup_slots) is True
    actor = object()
    attempts = []
    killed = threading.Event()

    def kill(stale_actor, **kwargs):
        attempts.append(stale_actor)
        if len(attempts) == 1:
            raise RuntimeError("Ray control plane unavailable")
        killed.set()

    monkeypatch.setattr(llm_instance.ray, "kill", kill)
    monkeypatch.setattr(llm_instance, "LLM_LATE_ACTOR_KILL_RETRY_SECONDS", 0)
    monkeypatch.setattr(
        llm_instance,
        "_confirm_actor_terminated",
        lambda stale_actor, deadline, operation: None,
    )

    manager._schedule_late_actor_kill("instance-1", generation, actor)

    assert killed.wait(1)
    assert attempts == [actor, actor]
    assert generation.late_actor_kill_claimed is True
    assert cleanup_slots.available == 1


def test_late_actor_schedule_only_enqueues_cleanup(monkeypatch):
    manager = LlmInstanceManager()

    class RecordingExecutor:
        def __init__(self):
            self.retained = []

        def submit(self, function, *args, **kwargs):
            self.retained.append((function, args, kwargs))
            return Future()

    class TerminationMethod:
        def __init__(self, error=None):
            self.error = error
            self.calls = 0

        def remote(self):
            self.calls += 1
            if self.error is not None:
                raise self.error
            return FakeRef()

    recording_executor = RecordingExecutor()
    monkeypatch.setattr(
        llm_instance,
        "_LATE_ACTOR_KILL_EXECUTOR",
        recording_executor,
    )
    monkeypatch.setattr(
        llm_instance.ray,
        "kill",
        lambda *_args, **_kwargs: pytest.fail("cleanup must not run inline"),
    )
    monkeypatch.setattr(
        llm_instance,
        "_confirm_actor_terminated",
        lambda stale_actor, deadline, operation: None,
    )

    termination = TerminationMethod()
    actor = SimpleNamespace(__ray_terminate__=termination)
    generation = llm_instance._PendingActorStart(time.monotonic() + 1)
    cleanup_slots = llm_instance._BoundedCleanupSlots(1)
    assert generation.reserve_cleanup_slot(cleanup_slots) is True
    manager._schedule_late_actor_kill("instance-1", generation, actor)
    assert termination.calls == 0
    assert len(recording_executor.retained) == 1
    assert cleanup_slots.available == 0

    killed = []
    monkeypatch.setattr(
        llm_instance.ray,
        "kill",
        lambda stale_actor, **kwargs: killed.append(stale_actor),
    )
    function, args, kwargs = recording_executor.retained[0]
    function(*args, **kwargs)
    assert killed == [actor]
    assert cleanup_slots.available == 1


def test_late_actor_waits_for_native_termination_before_force_kill(monkeypatch):
    manager = LlmInstanceManager()

    class RecordingExecutor:
        def __init__(self):
            self.retained = []

        def submit(self, function, *args, **kwargs):
            self.retained.append((function, args, kwargs))
            return Future()

    terminate_ref = FakeRef()
    native_calls = []
    actor = SimpleNamespace(
        __ray_terminate__=SimpleNamespace(
            remote=lambda: native_calls.append("submitted") or terminate_ref
        )
    )
    cleanup_slots = llm_instance._BoundedCleanupSlots(1)
    generation = llm_instance._PendingActorStart(time.monotonic() + 1)
    assert generation.reserve_cleanup_slot(cleanup_slots) is True
    recording_executor = RecordingExecutor()
    monkeypatch.setattr(
        llm_instance,
        "_LATE_ACTOR_KILL_EXECUTOR",
        recording_executor,
    )

    waited_refs = []

    def fail_native_wait(ref, timeout=None):
        waited_refs.append((ref, timeout))
        assert cleanup_slots.available == 0
        raise RuntimeError("native termination was not confirmed")

    force_killed = []

    def force_kill(stale_actor, **kwargs):
        assert cleanup_slots.available == 0
        force_killed.append(stale_actor)

    monkeypatch.setattr(llm_instance.ray, "get", fail_native_wait)
    monkeypatch.setattr(llm_instance.ray, "kill", force_kill)
    monkeypatch.setattr(
        llm_instance,
        "_confirm_actor_terminated",
        lambda stale_actor, deadline, operation: None,
    )

    manager._schedule_late_actor_kill("instance-1", generation, actor)
    function, args, kwargs = recording_executor.retained[0]
    function(*args, **kwargs)

    assert native_calls == ["submitted"]
    assert waited_refs == [(terminate_ref, llm_instance.LLM_ACTOR_STOP_TIMEOUT)]
    assert force_killed == [actor]
    assert cleanup_slots.available == 1


def test_force_kill_confirmation_waits_until_actor_death(monkeypatch):
    probes = [
        FakeRef(True),
        FakeRef(
            error=llm_instance.ray.exceptions.RayActorError(
                error_msg="actor is dead"
            )
        ),
    ]

    class ReadyMethod:
        def remote(self):
            return probes.pop(0)

    monkeypatch.setattr(llm_instance.ray, "get", fake_ray_get)

    llm_instance._confirm_actor_terminated(
        SimpleNamespace(__ray_ready__=ReadyMethod()),
        time.monotonic() + 1,
        "test actor force kill",
    )

    assert probes == []


def test_cleanup_slot_releases_for_every_no_actor_terminal_path():
    manager = LlmInstanceManager()
    cleanup_slots = llm_instance._BoundedCleanupSlots(1)

    unsubmitted = llm_instance._PendingActorStart(time.monotonic() + 1)
    assert unsubmitted.reserve_cleanup_slot(cleanup_slots) is True
    manager.pending_start_generations["unsubmitted"] = unsubmitted
    with manager.lock:
        manager._invalidate_pending_start_locked(
            "unsubmitted",
            RuntimeError("cancelled before submit"),
        )
    assert cleanup_slots.available == 1
    assert unsubmitted.release_cleanup_slot() is False

    cancelled = llm_instance._PendingActorStart(time.monotonic() + 1)
    assert cancelled.reserve_cleanup_slot(cleanup_slots) is True
    cancelled.actor_creation_submitted = True
    cancelled.future = Future()
    cancelled.future.add_done_callback(
        lambda future: manager._actor_creation_finished(
            "cancelled",
            cancelled,
            future,
        )
    )
    manager.pending_start_generations["cancelled"] = cancelled
    with manager.lock:
        manager._invalidate_pending_start_locked(
            "cancelled",
            RuntimeError("cancelled while queued"),
        )
    assert cancelled.future.cancelled() is True
    assert cleanup_slots.available == 1
    assert cancelled.release_cleanup_slot() is False

    failed = llm_instance._PendingActorStart(time.monotonic() + 1)
    assert failed.reserve_cleanup_slot(cleanup_slots) is True
    failed.actor_creation_submitted = True
    failed.future = Future()
    failed.future.add_done_callback(
        lambda future: manager._actor_creation_finished(
            "failed",
            failed,
            future,
        )
    )
    manager.pending_start_generations["failed"] = failed
    assert failed.future.set_running_or_notify_cancel() is True
    failed.future.set_exception(RuntimeError("actor creation failed"))
    assert cleanup_slots.available == 1
    assert failed.release_cleanup_slot() is False
    manager.pending_start_generations.pop("failed")


def test_cleanup_slot_saturation_keeps_cas_timeout_nonblocking(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    cleanup_slots = llm_instance._BoundedCleanupSlots(3)
    cleanup_executor = llm_instance._BoundedDaemonExecutor(
        1,
        3,
        "test-stale-cleanup",
    )
    creation_executor = llm_instance._BoundedDaemonExecutor(
        1,
        1,
        "test-cas-create",
        max_abandoned=1,
    )
    monkeypatch.setattr(
        llm_instance,
        "_STALE_ACTOR_CLEANUP_SLOTS",
        cleanup_slots,
    )
    monkeypatch.setattr(
        llm_instance,
        "_LATE_ACTOR_KILL_EXECUTOR",
        cleanup_executor,
    )
    monkeypatch.setattr(
        llm_instance,
        "_ACTOR_CREATION_EXECUTOR",
        creation_executor,
    )

    actors = [FakeActor(events) for _ in range(4)]
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    remote_entered = threading.Event()
    allow_remote_return = threading.Event()
    killed = []
    remote_calls = 0
    remote_lock = threading.Lock()

    def kill(actor, **kwargs):
        cleanup_entered.set()
        release_cleanup.wait(5)
        killed.append(actor)

    class SaturatedActorOptions:
        def remote(self, **kwargs):
            nonlocal remote_calls
            with remote_lock:
                remote_calls += 1
                call_number = remote_calls
            if call_number == 1:
                remote_entered.set()
                allow_remote_return.wait(5)
                return actors[2]
            return actors[3]

    class SaturatedLLMServerActor:
        @classmethod
        def options(cls, **kwargs):
            return SaturatedActorOptions()

    monkeypatch.setattr(llm_instance.ray, "kill", kill)
    monkeypatch.setattr(llm_instance, "LLMServerActor", SaturatedLLMServerActor)
    manager = LlmInstanceManager()

    for index in range(2):
        generation = llm_instance._PendingActorStart(time.monotonic() + 1)
        assert generation.reserve_cleanup_slot(cleanup_slots) is True
        manager._schedule_late_actor_kill(
            f"preloaded-{index}",
            generation,
            actors[index],
        )
    assert cleanup_entered.wait(1)
    assert cleanup_slots.available == 1

    start_errors = []
    start_finished = threading.Event()

    def start_that_loses_cas():
        try:
            manager_start(
                manager,
                instance_id="cas-timeout",
                startup_timeout=0.1,
            )
        except BaseException as exc:
            start_errors.append(exc)
        finally:
            start_finished.set()

    start_thread = threading.Thread(target=start_that_loses_cas)
    start_thread.start()
    try:
        assert remote_entered.wait(1)
        with manager.lock:
            allow_remote_return.set()
            time.sleep(0.15)

        assert start_finished.wait(0.2)
        assert isinstance(start_errors[0], TimeoutError)
        assert cleanup_slots.available == 0

        rejected_at = time.monotonic()
        with pytest.raises(RuntimeError, match="cleanup capacity is exhausted"):
            manager_start(
                manager,
                instance_id="capacity-rejected",
                startup_timeout=1,
            )
        assert time.monotonic() - rejected_at < 0.2
        assert remote_calls == 1

        release_cleanup.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if cleanup_slots.available == cleanup_slots.capacity:
                break
            time.sleep(0.01)
        assert cleanup_slots.available == cleanup_slots.capacity
        assert set(killed) == set(actors[:3])
        assert all(killed.count(actor) == 1 for actor in actors[:3])

        assert manager_start(
            manager,
            instance_id="capacity-recovered",
            startup_timeout=1,
        ) == "8123"
        assert cleanup_slots.available == cleanup_slots.capacity
        assert remote_calls == 2
    finally:
        allow_remote_return.set()
        release_cleanup.set()
        start_thread.join(1)


def test_all_blocked_actor_creation_workers_are_replaced_with_a_fixed_cap(
    monkeypatch,
):
    events = []
    install_fake_runtime(monkeypatch, events)
    executor = llm_instance._BoundedDaemonExecutor(
        2,
        2,
        "test-actor-create",
        max_abandoned=2,
    )
    monkeypatch.setattr(llm_instance, "_ACTOR_CREATION_EXECUTOR", executor)
    actors = [FakeActor(events) for _ in range(3)]
    entered = [threading.Event(), threading.Event()]
    release_blocked = threading.Event()
    creation_lock = threading.Lock()
    creation_count = 0
    killed = []

    class BlockingActorOptions:
        def remote(self, **kwargs):
            nonlocal creation_count
            with creation_lock:
                index = creation_count
                creation_count += 1
            if index < 2:
                entered[index].set()
                release_blocked.wait(5)
            return actors[index]

    class BlockingLLMServerActor:
        @classmethod
        def options(cls, **kwargs):
            return BlockingActorOptions()

    monkeypatch.setattr(llm_instance, "LLMServerActor", BlockingLLMServerActor)
    monkeypatch.setattr(
        llm_instance.ray,
        "kill",
        lambda actor, **kwargs: killed.append(actor),
    )
    manager = LlmInstanceManager()
    errors = []
    threads = [
        threading.Thread(
            target=lambda instance_id=instance_id: _capture_error(
                errors,
                lambda: manager_start(
                    manager,
                    instance_id=instance_id,
                    startup_timeout=0.05,
                ),
            )
        )
        for instance_id in ("blocked-1", "blocked-2")
    ]
    for thread in threads:
        thread.start()

    try:
        assert all(event.wait(1) for event in entered)
        for thread in threads:
            thread.join(0.5)
        assert all(thread.is_alive() is False for thread in threads)
        assert len(errors) == 2
        assert all(isinstance(error, TimeoutError) for error in errors)

        assert manager_start(
            manager,
            instance_id="replacement",
            startup_timeout=1,
        ) == "8123"
        assert executor._next_worker_id == 4

        shutdown_started = time.monotonic()
        manager.begin_shutdown()
        assert time.monotonic() - shutdown_started < 0.2
        manager.stop_llm_instance("replacement")
    finally:
        release_blocked.set()
        for thread in threads:
            thread.join(1)

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not set(actors[:2]).issubset(killed):
        time.sleep(0.01)
    assert set(actors[:2]).issubset(killed)
    assert all(killed.count(actor) == 1 for actor in actors[:2])
    assert executor._next_worker_id == 4
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with executor._state_lock:
            abandoned_count = len(executor._abandoned_futures)
        live_worker_count = sum(
            thread.name.startswith("test-actor-create-") and thread.is_alive()
            for thread in threading.enumerate()
        )
        if abandoned_count == 0 and live_worker_count == 2:
            break
        time.sleep(0.01)
    with executor._state_lock:
        assert executor._abandoned_futures == set()
    live_workers = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("test-actor-create-") and thread.is_alive()
    ]
    assert len(live_workers) == 2
    assert llm_instance._STALE_ACTOR_CLEANUP_SLOTS.available == (
        llm_instance._STALE_ACTOR_CLEANUP_SLOTS.capacity
    )


def test_runtime_process_exit_is_removed_from_routing_before_cleanup(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)
    actor = manager.id_to_instance_actor["instance-1"]
    actor.get_process_status.value = {
        "return_code": 137,
        "ready": True,
        "running": False,
    }

    candidates = manager.runtime_cleanup_candidates()

    assert candidates == [{
        "instance_id": "instance-1",
        "state": "unhealthy",
        "reason": "model process exited with code 137",
    }]
    assert manager.get_instance_state("instance-1") == "unhealthy"
    assert manager.route_model_request(
        "workflow-1",
        {"local_model": "/models/qwen", "backend": "vllm"},
    ) is None


def test_runtime_health_failure_is_marked_for_confirmed_cleanup(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)
    actor = manager.id_to_instance_actor["instance-1"]
    actor.get_process_status.value = {
        "return_code": None,
        "ready": True,
        "running": True,
    }

    class UnhealthyResponse:
        status_code = 503

    monkeypatch.setattr(
        llm_instance.requests,
        "get",
        lambda _url, timeout: UnhealthyResponse(),
    )

    candidates = manager.runtime_cleanup_candidates()

    assert candidates[0]["instance_id"] == "instance-1"
    assert candidates[0]["state"] == "unhealthy"
    assert candidates[0]["reason"] == "model health check returned HTTP 503"
    assert manager.snapshot()["runtime_errors"]["instance-1"] == (
        "model health check returned HTTP 503"
    )


def test_manager_stop_is_synchronous_and_can_defer_finalization(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager(owner_id="owner-1")
    manager_start(manager)

    detail = manager.stop_llm_instance("instance-1", finalize=False)

    event_names = [event[0] for event in events]
    assert event_names.index("stop") < event_names.index("kill")
    assert event_names.index("kill") < event_names.index("cleanup")
    assert detail["lease_id"] == "lease-1"
    assert manager.get_instance_state("instance-1") == "stopped"
    assert manager.route_model_request(
        "workflow-2",
        {"local_model": "/models/qwen", "backend": "vllm"},
    ) is None
    assert manager.finalize_stopped_instance("instance-1") is True
    assert manager.has_instance("instance-1") is False


def test_blocked_stop_submission_obeys_deadline_without_holding_manager_lock(
    monkeypatch,
):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)
    actor = manager.id_to_instance_actor["instance-1"]
    entered = threading.Event()
    release = threading.Event()
    control_threads = []
    executor = install_control_executor(monkeypatch, "test-stop-submit")

    def blocked_stop(*args, **kwargs):
        control_threads.append(threading.current_thread().name)
        entered.set()
        release.wait(2)
        return FakeRef(True)

    actor.stop_server = SimpleNamespace(remote=blocked_stop)
    started_at = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="exceeded its deadline"):
            manager.stop_llm_instance(
                "instance-1",
                deadline=time.monotonic() + 0.1,
            )
        assert entered.is_set()
        assert time.monotonic() - started_at < 1
        assert control_threads[0].startswith("test-stop-submit-")
        assert manager.get_instance_state("instance-1") == "cleanup_pending"
        assert manager.snapshot()["instance_states"]["instance-1"] == (
            "cleanup_pending"
        )
        assert manager.get_instance_resource_detail("instance-1")["status"] == (
            "cleanup_pending"
        )
        manager.begin_shutdown()
    finally:
        release.set()
        wait_for_executor_retirement(executor)


def test_blocked_force_kill_keeps_successful_graceful_stop_pending(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)
    actor = manager.id_to_instance_actor["instance-1"]
    entered = threading.Event()
    release = threading.Event()
    control_threads = []
    executor = install_control_executor(monkeypatch, "test-force-kill")

    def blocked_kill(stopped_actor, **kwargs):
        assert stopped_actor is actor
        control_threads.append(threading.current_thread().name)
        entered.set()
        release.wait(2)

    monkeypatch.setattr(llm_instance.ray, "kill", blocked_kill)
    try:
        with pytest.raises(RuntimeError, match="exceeded its deadline"):
            manager.stop_llm_instance(
                "instance-1",
                deadline=time.monotonic() + 0.1,
            )
        assert entered.is_set()
        assert control_threads[0].startswith("test-force-kill-")
        assert [event[0] for event in events].count("stop") == 1
        assert manager.get_instance_state("instance-1") == "cleanup_pending"
        assert manager.has_instance("instance-1") is True
        assert manager.get_instance_resource_detail("instance-1")["lease_id"] == (
            "lease-1"
        )
    finally:
        release.set()
        wait_for_executor_retirement(executor)


def test_returned_force_kill_must_be_confirmed_before_finalization(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)

    def unconfirmed(*args, **kwargs):
        raise RuntimeError("actor death was not observed")

    monkeypatch.setattr(llm_instance, "_confirm_actor_terminated", unconfirmed)

    with pytest.raises(RuntimeError, match="actor termination was not confirmed"):
        manager.stop_llm_instance("instance-1")

    assert {event[0] for event in events} >= {"stop", "kill", "cleanup"}
    assert manager.get_instance_state("instance-1") == "cleanup_pending"
    assert manager.has_instance("instance-1") is True
    with pytest.raises(RuntimeError, match="cleanup_pending"):
        manager.finalize_stopped_instance("instance-1")


def test_blocked_cleanup_submission_obeys_stop_deadline(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)
    entered = threading.Event()
    release = threading.Event()
    control_threads = []
    executor = install_control_executor(monkeypatch, "test-cleanup-submit")

    class BlockingCleanupTask:
        @classmethod
        def options(cls, **kwargs):
            return cls

        @classmethod
        def remote(cls, instance_id, generation_id=None):
            control_threads.append(threading.current_thread().name)
            entered.set()
            release.wait(2)
            return FakeRef({"stopped_process_groups": []})

    monkeypatch.setattr(
        llm_instance,
        "stop_llm_instance_processes",
        BlockingCleanupTask,
    )
    try:
        with pytest.raises(RuntimeError, match="exceeded its deadline"):
            manager.stop_llm_instance(
                "instance-1",
                deadline=time.monotonic() + 0.1,
            )
        assert entered.is_set()
        assert control_threads[0].startswith("test-cleanup-submit-")
        assert manager.get_instance_state("instance-1") == "cleanup_pending"
        with pytest.raises(RuntimeError, match="cleanup_pending"):
            manager.finalize_stopped_instance("instance-1")
    finally:
        release.set()
        wait_for_executor_retirement(executor)


def test_late_cleanup_submission_is_scoped_to_old_generation(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)
    old_generation = manager.get_instance_resource_detail("instance-1")[
        "generation_id"
    ]
    first_submission_entered = threading.Event()
    release_first_submission = threading.Event()
    submissions = []
    executor = install_control_executor(monkeypatch, "test-generation-cleanup")

    class GenerationCleanupTask:
        @classmethod
        def options(cls, **kwargs):
            return cls

        @classmethod
        def remote(cls, instance_id, generation_id=None):
            submissions.append((instance_id, generation_id))
            if len(submissions) == 1:
                first_submission_entered.set()
                release_first_submission.wait(2)
            return FakeRef({"stopped_process_groups": []})

    monkeypatch.setattr(
        llm_instance,
        "stop_llm_instance_processes",
        GenerationCleanupTask,
    )
    try:
        with pytest.raises(RuntimeError, match="exceeded its deadline"):
            manager.stop_llm_instance(
                "instance-1",
                deadline=time.monotonic() + 0.1,
            )
        assert first_submission_entered.is_set()

        manager.stop_llm_instance(
            "instance-1",
            deadline=time.monotonic() + 1,
        )
        manager_start(manager)
        new_generation = manager.get_instance_resource_detail("instance-1")[
            "generation_id"
        ]

        assert new_generation != old_generation
        assert submissions == [
            ("instance-1", old_generation),
            ("instance-1", old_generation),
        ]
    finally:
        release_first_submission.set()
        wait_for_executor_retirement(executor)


def test_manager_stop_default_matches_current_caller_cleanup(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)

    detail = manager.stop_llm_instance("instance-1")

    assert detail["lease_id"] == "lease-1"
    assert manager.get_instance_state("instance-1") is None
    assert "instance-1" not in manager.snapshot()["instances"]


def test_cleanup_follows_same_physical_node_after_ray_rejoin(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)
    rejoined_node_id = "2" * 56
    monkeypatch.setattr(
        llm_instance.ray,
        "nodes",
        lambda: [{
            "NodeID": rejoined_node_id,
            "NodeManagerAddress": "127.0.0.1",
            "Alive": True,
        }],
    )

    manager.stop_llm_instance("instance-1", finalize=False)

    cleanup_options = next(
        event[2]["scheduling_strategy"]
        for event in events
        if event[0] == "cleanup_options"
    )
    assert cleanup_options.node_id == rejoined_node_id
    assert manager.get_instance_state("instance-1") == "stopped"


def test_cleanup_keeps_pending_state_while_physical_node_is_unavailable(
    monkeypatch,
):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager()
    manager_start(manager)
    monkeypatch.setattr(llm_instance.ray, "nodes", lambda: [])

    with pytest.raises(RuntimeError, match="cleanup is unverified"):
        manager.stop_llm_instance("instance-1", finalize=False)

    assert manager.get_instance_state("instance-1") == "cleanup_pending"


def test_old_start_failure_does_not_stop_reused_instance_actor(monkeypatch):
    old_events = []
    new_events = []
    install_fake_runtime(monkeypatch, old_events)
    manager = LlmInstanceManager()
    new_actor = FakeActor(new_events)
    old_actors = []

    def replace_actor_then_fail(instance_id, actor, *args, **kwargs):
        old_actors.append(actor)
        with manager.lock:
            manager.id_to_instance_actor[instance_id] = new_actor
            manager.id_to_state[instance_id] = "ready"
            manager.id_to_resource_detail[instance_id]["status"] = "ready"
        raise RuntimeError("old generation readiness failed")

    monkeypatch.setattr(manager, "_wait_until_ready", replace_actor_then_fail)

    with pytest.raises(RuntimeError, match="old generation readiness failed"):
        manager_start(manager)

    assert len(old_actors) == 1
    assert manager.id_to_instance_actor["instance-1"] is new_actor
    assert manager.get_instance_state("instance-1") == "ready"
    assert not {"stop", "kill", "cleanup"}.intersection(
        event[0] for event in old_events
    )
    assert new_events == []


def test_readiness_rejects_superseded_actor_before_remote_probe(monkeypatch):
    events = []
    manager = LlmInstanceManager()
    old_actor = FakeActor(events)
    new_actor = FakeActor(events)
    manager._register_starting_instance(
        "instance-1",
        old_actor,
        "/models/qwen",
        "vllm",
        NODE_ID,
        "127.0.0.1",
        0,
        RESOURCES,
        "lease-1",
    )
    manager.id_to_instance_actor["instance-1"] = new_actor

    with pytest.raises(RuntimeError, match="startup was cancelled"):
        manager._wait_until_ready(
            "instance-1",
            old_actor,
            "127.0.0.1",
            "8123",
            "/models/qwen",
            "vllm",
            {},
            timeout=0.1,
        )

    assert events == []


def test_start_failure_is_not_routable_and_keeps_cleanup_state(monkeypatch):
    events = []
    install_fake_runtime(
        monkeypatch,
        events,
        process_error=RuntimeError("startup failed"),
    )
    manager = LlmInstanceManager()

    with pytest.raises(RuntimeError, match="startup failed"):
        manager_start(manager)

    assert manager.get_instance_state("instance-1") == "stopped"
    assert manager.get_instance_resource_detail("instance-1")["lease_id"] == (
        "lease-1"
    )
    assert "instance-1" not in manager.snapshot()["instances"]
    assert manager.route_model_request(
        "workflow-1",
        {"local_model": "/models/qwen", "backend": "vllm"},
    ) is None


def test_start_failure_retains_cleanup_pending_state(monkeypatch):
    events = []
    install_fake_runtime(
        monkeypatch,
        events,
        process_error=RuntimeError("startup failed"),
        cleanup_error=RuntimeError("cleanup unavailable"),
    )
    manager = LlmInstanceManager()

    with pytest.raises(RuntimeError, match="cleanup is pending"):
        manager_start(manager)

    assert manager.get_instance_state("instance-1") == "cleanup_pending"
    assert manager.get_instance_resource_detail("instance-1")["status"] == (
        "cleanup_pending"
    )


def test_auto_deploy_token_clears_once_after_deferred_cleanup(monkeypatch):
    events = []
    install_fake_runtime(
        monkeypatch,
        events,
        process_error=RuntimeError("startup failed"),
        cleanup_error=RuntimeError("cleanup unavailable"),
    )
    manager = LlmInstanceManager()
    manager.mark_model_deploying(
        "/models/qwen",
        "vllm",
        instance_id="instance-1",
    )

    with pytest.raises(RuntimeError, match="cleanup is pending"):
        manager_start(manager)

    assert manager.get_instance_state("instance-1") == "cleanup_pending"
    assert manager.snapshot()["deploying_model_counts"]["/models/qwen|vllm"] == 1

    def cleanup_succeeds(ref, timeout=None):
        if isinstance(ref.value, dict) and "stopped_process_groups" in ref.value:
            return ref.value
        return fake_ray_get(ref, timeout=timeout)

    monkeypatch.setattr(llm_instance.ray, "get", cleanup_succeeds)
    manager.stop_llm_instance("instance-1", finalize=False)
    assert manager.finalize_stopped_instance("instance-1") is True

    snapshot = manager.snapshot()
    assert snapshot["deploying_model_counts"]["/models/qwen|vllm"] == 0
    assert "instance-1" not in snapshot["auto_deploy_by_instance"]
    assert manager.clear_model_deploying(
        "/models/qwen",
        "vllm",
        instance_id="instance-1",
    ) is False


def test_manager_shutdown_gate_and_duplicate_check_precede_actor_creation(
    monkeypatch,
):
    manager = LlmInstanceManager(owner_id="owner-1")
    manager.begin_shutdown()
    monkeypatch.setattr(
        llm_instance.LLMServerActor,
        "options",
        lambda **kwargs: pytest.fail("shutdown manager must not create an actor"),
    )
    with pytest.raises(RuntimeError, match="shutting down"):
        manager_start(manager)

    manager = LlmInstanceManager(owner_id="owner-1")
    actor = object()
    manager._register_starting_instance(
        "instance-1",
        actor,
        "/models/qwen",
        "vllm",
        NODE_ID,
        "127.0.0.1",
        0,
        RESOURCES,
        "lease-1",
    )
    monkeypatch.setattr(
        llm_instance.LLMServerActor,
        "options",
        lambda **kwargs: SimpleNamespace(
            remote=lambda **actor_args: pytest.fail(
                "duplicate instance must not create an actor"
            )
        ),
    )
    with pytest.raises(RuntimeError, match="already registered"):
        manager_start(manager)


def test_stop_all_keeps_details_for_later_resource_release(monkeypatch):
    events = []
    install_fake_runtime(monkeypatch, events)
    manager = LlmInstanceManager(owner_id="owner-1")
    manager_start(manager, instance_id="instance-1", lease_id="lease-1")
    manager_start(manager, instance_id="instance-2", lease_id="lease-2")

    stopped, errors = manager.stop_all_llm_instances()

    assert errors == {}
    assert set(stopped) == {"instance-1", "instance-2"}
    assert {detail["lease_id"] for detail in stopped.values()} == {
        "lease-1",
        "lease-2",
    }
    assert manager.get_instance_state("instance-1") == "stopped"
    assert manager.get_instance_state("instance-2") == "stopped"


def test_stop_all_uses_fixed_worker_limit_and_shared_deadline(monkeypatch):
    manager = LlmInstanceManager()
    actors = {}
    for index in range(12):
        instance_id = f"instance-{index}"
        actors[instance_id] = object()
        manager.id_to_instance_actor[instance_id] = actors[instance_id]
        manager.id_to_resource_detail[instance_id] = {
            "instance_id": instance_id,
            "lease_id": f"lease-{index}",
        }

    monkeypatch.setattr(llm_instance, "LLM_STOP_ALL_WORKERS", 3)
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()
    worker_names = set()
    deadlines = []

    def fake_stop(
        instance_id,
        *,
        finalize,
        expected_actor,
        deadline,
    ):
        nonlocal active, maximum_active
        assert finalize is False
        assert expected_actor is actors[instance_id]
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
            worker_names.add(threading.current_thread().name)
            deadlines.append(deadline)
        time.sleep(0.03)
        with state_lock:
            active -= 1
        return dict(manager.id_to_resource_detail[instance_id])

    monkeypatch.setattr(manager, "stop_llm_instance", fake_stop)

    stopped, errors = manager.stop_all_llm_instances()

    assert errors == {}
    assert set(stopped) == set(actors)
    assert maximum_active == 3
    assert len(worker_names) <= 3
    assert len(set(deadlines)) == 1


def test_owner_cleanup_skips_cluster_sweep_without_any_launch_attempt(monkeypatch):
    manager = LlmInstanceManager(owner_id="owner-1")
    monkeypatch.setattr(
        llm_instance,
        "stop_llm_owner_processes_on_cluster",
        lambda *_args, **_kwargs: pytest.fail(
            "an unused model owner must not submit Ray cleanup tasks"
        ),
    )

    assert manager.stop_owned_llm_processes() == {}


def test_actor_creation_attempt_requires_owner_cleanup_even_before_registration(
    monkeypatch,
):
    manager = LlmInstanceManager(owner_id="owner-1")
    manager.owner_cleanup_required = True
    calls = []
    monkeypatch.setattr(
        llm_instance,
        "stop_llm_owner_processes_on_cluster",
        lambda owner_id, **kwargs: calls.append((owner_id, kwargs)) or {"ok": True},
    )

    assert manager.stop_owned_llm_processes() == {"ok": True}
    assert calls == [("owner-1", {})]


def test_owner_cleanup_fans_out_to_every_alive_ray_node(monkeypatch):
    events = []
    head_id = "0" * 56
    worker_id = "1" * 56

    class FakeCleanupTask:
        @classmethod
        def options(cls, **kwargs):
            node_id = kwargs["scheduling_strategy"].node_id
            return SimpleNamespace(
                remote=lambda owner_id, timeout: (
                    events.append((node_id, owner_id, timeout))
                    or FakeRef({"stopped_process_groups": []})
                )
            )

    monkeypatch.setattr(
        llm_instance.ray,
        "nodes",
        lambda: [
            {"NodeID": head_id, "Alive": True},
            {"NodeID": worker_id, "Alive": True},
            {"NodeID": "2" * 56, "Alive": False},
        ],
    )
    monkeypatch.setattr(llm_instance, "stop_llm_owner_processes", FakeCleanupTask)
    monkeypatch.setattr(llm_instance.ray, "get", fake_ray_get)

    result = llm_instance.stop_llm_owner_processes_on_cluster(
        "owner-1",
        timeout=3,
    )

    assert events == [
        (head_id, "owner-1", 3),
        (worker_id, "owner-1", 3),
    ]
    assert set(result) == {head_id, worker_id}


def test_blocked_owner_cleanup_submission_obeys_total_deadline(monkeypatch):
    node_id = "0" * 56
    entered = threading.Event()
    release = threading.Event()
    control_threads = []
    executor = install_control_executor(monkeypatch, "test-owner-cleanup")

    class BlockingOwnerCleanupTask:
        @classmethod
        def options(cls, **kwargs):
            return cls

        @classmethod
        def remote(cls, owner_id, timeout):
            control_threads.append(threading.current_thread().name)
            entered.set()
            release.wait(2)
            return FakeRef({"stopped_process_groups": []})

    monkeypatch.setattr(
        llm_instance.ray,
        "nodes",
        lambda: [{"NodeID": node_id, "Alive": True}],
    )
    monkeypatch.setattr(
        llm_instance,
        "stop_llm_owner_processes",
        BlockingOwnerCleanupTask,
    )
    monkeypatch.setattr(llm_instance.ray, "get", fake_ray_get)
    monkeypatch.setattr(llm_instance, "LLM_OWNER_CLEANUP_GRACE_SECONDS", 0.1)
    try:
        with pytest.raises(TimeoutError, match="exceeded its deadline"):
            llm_instance.stop_llm_owner_processes_on_cluster(
                "owner-1",
                timeout=0,
            )
        assert entered.is_set()
        assert control_threads[0].startswith("test-owner-cleanup-")
    finally:
        release.set()
        wait_for_executor_retirement(executor)


def test_owner_cleanup_requires_expected_node_or_rejoined_ip(monkeypatch):
    old_id = "1" * 56
    new_id = "2" * 56
    worker_ip = "10.0.0.2"
    monkeypatch.setattr(
        llm_instance.ray,
        "nodes",
        lambda: [{
            "NodeID": new_id,
            "NodeManagerAddress": worker_ip,
            "Alive": True,
        }],
    )

    class FakeCleanupTask:
        @classmethod
        def options(cls, **kwargs):
            return SimpleNamespace(
                remote=lambda owner_id, timeout: FakeRef(
                    {"stopped_process_groups": []}
                )
            )

    monkeypatch.setattr(llm_instance, "stop_llm_owner_processes", FakeCleanupTask)
    monkeypatch.setattr(llm_instance.ray, "get", fake_ray_get)
    assert set(
        llm_instance.stop_llm_owner_processes_on_cluster(
            "owner-1",
            expected_nodes={old_id: worker_ip},
        )
    ) == {new_id}

    with pytest.raises(RuntimeError, match="unverified on unavailable Ray nodes"):
        llm_instance.stop_llm_owner_processes_on_cluster(
            "owner-1",
            expected_nodes={old_id: "10.0.0.99"},
        )


def test_transformers_cache_is_hashed_and_cleaned(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    model = tmp_path / "model-cache" / "qwen"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")

    cache_dir = llm_instance.prepare_transformers_cache(
        str(model),
        "../../../instance",
    )
    config_links = list(Path(cache_dir).glob("models--*/snapshots/*/config.json"))
    assert len(config_links) == 1
    assert config_links[0].resolve() == (model / "config.json").resolve()
    assert llm_instance._transformers_cache_root("../../../instance").parent == tmp_path

    llm_instance.cleanup_transformers_cache("../../../instance")
    assert not llm_instance._transformers_cache_root("../../../instance").exists()


def test_transformers_rejects_ambiguous_local_path(tmp_path):
    model = tmp_path / "model--cache" / "qwen"
    model.mkdir(parents=True)
    with pytest.raises(ValueError, match="losslessly"):
        llm_instance.validate_transformers_model(str(model))


@pytest.mark.skipif(
    os.name != "posix" or not os.path.isdir("/proc"),
    reason="requires POSIX process groups and /proc",
)
def test_owner_marker_sweep_stops_only_marked_process(tmp_path, monkeypatch):
    owner_id = f"owner-{uuid.uuid4()}"
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    env = llm_instance.build_model_env(
        None,
        instance_id=f"instance-{uuid.uuid4()}",
        owner_id=owner_id,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2
        while process.pid not in llm_instance._owner_process_groups(owner_id):
            if time.monotonic() >= deadline:
                raise AssertionError("marked process did not become visible in /proc")
            time.sleep(0.01)

        result = llm_instance.stop_llm_owner_processes_locally(
            owner_id,
            timeout=2,
            settle_timeout=0.1,
        )
        process.wait(timeout=2)
        assert result == {"stopped_process_groups": [process.pid]}
        assert llm_instance._owner_process_groups(owner_id) == set()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, llm_instance.signal.SIGKILL)
            process.wait(timeout=2)


@pytest.mark.skipif(
    os.name != "posix" or not os.path.isdir("/proc"),
    reason="requires POSIX flock and /proc",
)
def test_closed_owner_rejects_late_actor_launch(monkeypatch, tmp_path):
    owner_id = f"owner-{uuid.uuid4()}"
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    llm_instance._close_owner_launches_locally(owner_id)
    monkeypatch.setattr(
        llm_instance.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("closed owner must not launch"),
    )
    actor_class = llm_instance.LLMServerActor.__ray_metadata__.modified_class
    actor = actor_class(
        model="model",
        gpu_id=0,
        instance_id="instance-1",
        owner_id=owner_id,
    )

    with pytest.raises(RuntimeError, match="refusing a late model launch"):
        actor.launch_server()


def test_real_ray_stop_confirms_graceful_and_force_kill(monkeypatch):
    owned_runtime = not llm_instance.ray.is_initialized()
    if owned_runtime:
        llm_instance.ray.init(
            num_cpus=1,
            include_dashboard=False,
            log_to_driver=False,
        )

    @llm_instance.ray.remote(num_cpus=0)
    class StopProbe:
        def __init__(self):
            self.stop_calls = 0

        def record_stop(self):
            self.stop_calls += 1

        def get_stop_calls(self):
            return self.stop_calls

    @llm_instance.ray.remote(num_cpus=0)
    class StoppableActor:
        def __init__(self, probe):
            self.probe = probe

        def stop_server(self, timeout):
            import ray

            ray.get(self.probe.record_stop.remote())
            return True

        def ping(self):
            return "alive"

    @llm_instance.ray.remote(num_cpus=0)
    def confirmed_cleanup(instance_id):
        return {"instance_id": instance_id, "stopped_process_groups": []}

    probe = StopProbe.remote()
    actor = StoppableActor.remote(probe)
    try:
        assert llm_instance.ray.get(actor.ping.remote(), timeout=5) == "alive"
        manager = LlmInstanceManager()
        manager._register_starting_instance(
            "instance-1",
            actor,
            "/models/qwen",
            "vllm",
            "real-node",
            "127.0.0.1",
            0,
            RESOURCES,
            "lease-1",
        )
        manager.register_instance(
            "instance-1",
            "/models/qwen",
            "127.0.0.1",
            "real-node",
            0,
            "8123",
            RESOURCES,
            lease_id="lease-1",
        )
        monkeypatch.setattr(
            manager,
            "_cleanup_remote",
            lambda instance_id, node_id, node_ip=None, generation_id=None: (
                confirmed_cleanup.remote(instance_id)
            ),
        )

        manager.stop_llm_instance(
            "instance-1",
            finalize=False,
            deadline=time.monotonic() + 10,
        )

        assert manager.get_instance_state("instance-1") == "stopped"
        assert llm_instance.ray.get(probe.get_stop_calls.remote(), timeout=5) == 1
        with pytest.raises(llm_instance.ray.exceptions.RayActorError):
            llm_instance.ray.get(actor.ping.remote(), timeout=5)
    finally:
        try:
            llm_instance.ray.kill(actor, no_restart=True)
        except Exception:
            pass
        try:
            llm_instance.ray.kill(probe, no_restart=True)
        except Exception:
            pass
        if owned_runtime:
            llm_instance.ray.shutdown()
