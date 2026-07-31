import asyncio
import json
import multiprocessing as mp
import os
import select
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from maze.core.path.path import MaPath
from maze.core.scheduler import llm_instance
from maze.core.scheduler import scheduler as scheduler_module
from maze.core.scheduler.llm_instance import (
    LlmInstanceManager,
    build_transformers_command,
    build_vllm_command,
)
from maze.core.scheduler.resource import Node, ResourceManager
from maze.core.scheduler.scheduler import Scheduler


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
    def __init__(self, events, start_error=None, backend="vllm"):
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
            error=start_error,
        )
        self.mark_ready = FakeRemoteMethod("ready", events, value=True)
        self.stop_server = FakeRemoteMethod("stop", events)


def fake_ray_get(ref, timeout=None):
    if ref.error is not None:
        raise ref.error
    return ref.value


def install_fake_actor(monkeypatch, events, start_error=None, cleanup_error=None):
    class FakeActorOptions:
        def remote(self, **kwargs):
            events.append(("actor", (), kwargs))
            return FakeActor(events, start_error, kwargs["backend"])

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
        def remote(cls, instance_id):
            events.append(("cleanup", (instance_id,), {}))
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
        if url.endswith("/v1/models"):
            return FakeResponse({"data": [{"id": "/models/qwen"}]})
        return FakeResponse()

    def fake_post(url, json, timeout):
        return FakeResponse({"choices": [{"message": {"content": "READY"}}]})

    monkeypatch.setattr(llm_instance, "LLMServerActor", FakeLLMServerActor)
    monkeypatch.setattr(
        llm_instance,
        "stop_llm_instance_processes",
        FakeCleanupTask,
    )
    monkeypatch.setattr(llm_instance.ray, "get", fake_ray_get)
    monkeypatch.setattr(llm_instance.requests, "get", fake_get)
    monkeypatch.setattr(llm_instance.requests, "post", fake_post)
    monkeypatch.setattr(
        llm_instance.ray,
        "kill",
        lambda target, **kwargs: events.append(("kill", (target,), kwargs)),
    )
def manager_start(manager, **kwargs):
    return manager.start_llm_instance(
        instance_id="instance-1",
        model="/models/qwen",
        node_ip="127.0.0.1",
        node_id="0" * 56,
        gpu_id=0,
        resources={"cpu": 1, "cpu_mem": 1024, "gpu": 1, "gpu_mem": 0},
        lease_id="lease-1",
        **kwargs,
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
        llm_instance.sys.executable,
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
    assert build_transformers_command(
        "/models/qwen",
        "0.0.0.0",
        "8123",
    ) == [
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


def test_model_env_exposes_tools_from_current_environment(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")

    env = llm_instance.build_model_env("3", "instance-1")

    assert env["PATH"].split(llm_instance.os.pathsep) == [
        llm_instance.os.path.dirname(llm_instance.sys.executable),
        "/usr/bin",
    ]
    assert env["CUDA_VISIBLE_DEVICES"] == "3"
    assert env[llm_instance.LLM_INSTANCE_ENV_VAR] == "instance-1"

    owned_env = llm_instance.build_model_env("3", "instance-1", "owner-1")
    assert owned_env[llm_instance.LLM_OWNER_ENV_VAR] == "owner-1"


def test_vllm_process_group_is_force_killed_before_stop_returns(monkeypatch):
    signals = []
    waits = iter([False, True])

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(
        llm_instance,
        "_wait_for_process_group_exit",
        lambda process_group_id, timeout, proc: next(waits),
    )
    monkeypatch.setattr(
        llm_instance.os,
        "killpg",
        lambda process_group_id, sig: signals.append((process_group_id, sig)),
    )

    llm_instance._stop_subprocess(FakeProcess(), 4321, 1)

    assert signals == [
        (4321, llm_instance.signal.SIGTERM),
        (4321, llm_instance.signal.SIGKILL),
    ]


@pytest.mark.skipif(os.name != "posix" or not os.path.isdir("/proc"), reason="requires /proc")
def test_instance_marker_cleanup_only_targets_matching_process_group():
    instance_id = f"maze-test-{uuid.uuid4()}"
    env = llm_instance.build_model_env(None, instance_id)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
    )
    reaper = threading.Thread(target=proc.wait)
    reaper.start()
    try:
        deadline = time.monotonic() + 2
        while proc.pid not in llm_instance._instance_process_groups(instance_id):
            if time.monotonic() >= deadline:
                raise AssertionError("marked process did not become visible in /proc")
            time.sleep(0.01)

        assert llm_instance._instance_process_groups(instance_id + "-other") == set()
        result = llm_instance.stop_llm_instance_processes._function(
            instance_id,
            timeout=1,
            settle_timeout=0.1,
        )
        reaper.join(timeout=2)

        assert not reaper.is_alive()
        assert result["stopped_process_groups"] == [proc.pid]
        assert llm_instance._instance_process_groups(instance_id) == set()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, llm_instance.signal.SIGKILL)
            proc.wait()


@pytest.mark.skipif(os.name != "posix" or not os.path.isdir("/proc"), reason="requires /proc")
def test_owner_cleanup_does_not_kill_other_owner_or_unmarked_processes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    owner_id = f"maze-owner-{uuid.uuid4()}"
    other_owner_id = f"maze-owner-{uuid.uuid4()}"
    instance_id = f"maze-instance-{uuid.uuid4()}"

    owned_env = llm_instance.build_model_env(None, instance_id, owner_id)
    other_env = llm_instance.build_model_env(None, instance_id, other_owner_id)
    unmarked_env = os.environ.copy()
    unmarked_env.pop(llm_instance.LLM_INSTANCE_ENV_VAR, None)
    unmarked_env.pop(llm_instance.LLM_OWNER_ENV_VAR, None)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            env=env,
            start_new_session=True,
        )
        for env in (owned_env, other_env, unmarked_env)
    ]
    reapers = [threading.Thread(target=process.wait) for process in processes]
    for reaper in reapers:
        reaper.start()
    try:
        deadline = time.monotonic() + 2
        while processes[0].pid not in llm_instance._owner_process_groups(owner_id):
            if time.monotonic() >= deadline:
                raise AssertionError("owned process did not become visible in /proc")
            time.sleep(0.01)

        result = llm_instance.stop_llm_owner_processes_locally(
            owner_id,
            timeout=1,
            settle_timeout=0.1,
        )
        reapers[0].join(timeout=2)

        assert not reapers[0].is_alive()
        assert result["stopped_process_groups"] == [processes[0].pid]
        assert processes[1].poll() is None
        assert processes[2].poll() is None
        assert processes[1].pid in llm_instance._owner_process_groups(other_owner_id)
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, llm_instance.signal.SIGKILL)
        for reaper in reapers:
            reaper.join(timeout=2)


@pytest.mark.skipif(os.name != "posix" or not os.path.isdir("/proc"), reason="requires /proc")
def test_owner_cleanup_force_kills_process_group_that_ignores_sigterm(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    owner_id = f"maze-owner-{uuid.uuid4()}"
    env = llm_instance.build_model_env(None, "instance-1", owner_id)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)"
            ),
        ],
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    reaper = threading.Thread(target=proc.wait)
    reaper.start()
    try:
        result = llm_instance.stop_llm_owner_processes_locally(
            owner_id,
            timeout=0.1,
            settle_timeout=0.05,
        )
        reaper.join(timeout=2)

        assert not reaper.is_alive()
        assert proc.returncode == -llm_instance.signal.SIGKILL
        assert result["stopped_process_groups"] == [proc.pid]
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, llm_instance.signal.SIGKILL)
        reaper.join(timeout=2)


def test_marked_process_cleanup_uses_one_total_deadline(monkeypatch):
    now = [100.0]
    signals = []

    def sleep(seconds):
        assert seconds >= 0
        now[0] += seconds

    monkeypatch.setattr(llm_instance.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(llm_instance.time, "sleep", sleep)
    monkeypatch.setattr(
        llm_instance.os,
        "killpg",
        lambda process_group_id, sig: signals.append((process_group_id, sig)),
    )

    with pytest.raises(RuntimeError, match="did not stop"):
        llm_instance._stop_marked_process_groups(
            lambda: {4321},
            timeout=0.3,
            settle_timeout=0.05,
        )

    assert now[0] == pytest.approx(100.3)
    assert (4321, llm_instance.signal.SIGTERM) in signals
    assert (4321, llm_instance.signal.SIGKILL) in signals


def test_owner_cleanup_fence_rejects_a_late_model_launch(monkeypatch, tmp_path):
    owner_id = "owner-late-launch"
    launches = []
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        llm_instance,
        "_stop_marked_process_groups",
        lambda *args, **kwargs: set(),
    )
    monkeypatch.setattr(
        llm_instance.subprocess,
        "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)),
    )

    llm_instance.stop_llm_owner_processes_locally(
        owner_id,
        timeout=0,
        settle_timeout=0,
    )

    with pytest.raises(RuntimeError, match="refusing a late model launch"):
        with llm_instance._owner_launch_guard(owner_id):
            launches.append("launched")
    assert launches == []


def test_actor_launch_checks_owner_fence_before_preparing_transformers_cache(
    monkeypatch,
    tmp_path,
):
    owner_id = "owner-closed-before-actor-launch"
    instance_id = "instance-closed-before-actor-launch"
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        llm_instance.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("closed owner must not launch a process"),
    )
    llm_instance._close_owner_launches_locally(owner_id)
    actor_class = llm_instance.LLMServerActor.__ray_metadata__.modified_class
    actor = actor_class(
        instance_id,
        str(model),
        0,
        backend="transformers",
        owner_id=owner_id,
    )

    with pytest.raises(RuntimeError, match="refusing a late model launch"):
        actor.launch_server()

    assert not llm_instance._transformers_cache_root(instance_id).exists()


@pytest.mark.skipif(
    os.name != "posix" or not os.path.isdir("/proc"),
    reason="requires POSIX /proc owner cleanup",
)
def test_owned_actor_launcher_hands_lock_off_only_after_marker_is_visible(
    monkeypatch,
    tmp_path,
):
    owner_id = f"owner-launcher-{uuid.uuid4()}"
    instance_id = f"instance-launcher-{uuid.uuid4()}"
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        llm_instance,
        "build_vllm_command",
        lambda *_args, **_kwargs: [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
    )
    actor_class = llm_instance.LLMServerActor.__ray_metadata__.modified_class
    actor = actor_class(
        instance_id,
        "model",
        0,
        backend="vllm",
        owner_id=owner_id,
    )
    launch_info = actor.launch_server()
    process = actor.proc
    reaper = threading.Thread(target=process.wait)
    reaper.start()
    try:
        deadline = time.monotonic() + 2
        while process.pid not in llm_instance._owner_process_groups(owner_id):
            if time.monotonic() >= deadline:
                raise AssertionError("launcher released the owner lock before marker visibility")
            time.sleep(0.01)

        result = llm_instance.stop_llm_owner_processes_locally(
            owner_id,
            timeout=1,
            settle_timeout=0.1,
        )
        reaper.join(timeout=2)

        assert launch_info["process_group_id"] == process.pid
        assert not reaper.is_alive()
        assert result["stopped_process_groups"] == [process.pid]
        assert llm_instance._owner_process_groups(owner_id) == set()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, llm_instance.signal.SIGKILL)
        reaper.join(timeout=2)


@pytest.mark.skipif(
    os.name != "posix" or not os.path.isdir("/proc"),
    reason="requires fork, flock, and POSIX /proc",
)
def test_owner_cleanup_catches_child_after_launcher_is_sigkilled_before_exec(
    monkeypatch,
    tmp_path,
):
    owner_id = f"owner-pre-exec-{uuid.uuid4()}"
    instance_id = f"instance-pre-exec-{uuid.uuid4()}"
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    expected_state_path = llm_instance._owner_state_path(owner_id)
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()

    def launch_with_delayed_exec():
        os.close(ready_read)
        os.close(release_write)
        real_popen = llm_instance.subprocess.Popen

        def delayed_popen(command, **kwargs):
            inherited_fds = tuple(kwargs.pop("pass_fds", ()))

            def stop_before_exec():
                os.write(ready_write, f"{os.getpid()}\n".encode("ascii"))
                os.read(release_read, 1)

            return real_popen(
                command,
                pass_fds=(*inherited_fds, ready_write, release_read),
                preexec_fn=stop_before_exec,
                **kwargs,
            )

        llm_instance.subprocess.Popen = delayed_popen
        llm_instance.build_vllm_command = lambda *_args, **_kwargs: [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ]
        actor_class = llm_instance.LLMServerActor.__ray_metadata__.modified_class
        actor = actor_class(
            instance_id,
            "model",
            0,
            backend="vllm",
            owner_id=owner_id,
        )
        actor.launch_server()

    context = mp.get_context("fork")
    launcher = context.Process(target=launch_with_delayed_exec)
    cleanup_result = []
    cleanup_error = []
    launcher.start()
    os.close(ready_write)
    os.close(release_read)
    child_pid = None
    cleanup_thread = None
    try:
        readable, _, _ = select.select([ready_read], [], [], 2)
        assert readable, "model child did not reach the pre-exec fault point"
        child_pid = int(os.read(ready_read, 64).strip())
        assert expected_state_path.exists()

        os.kill(launcher.pid, llm_instance.signal.SIGKILL)
        launcher.join(timeout=2)
        assert launcher.exitcode == -llm_instance.signal.SIGKILL

        def cleanup():
            try:
                cleanup_result.append(
                    llm_instance.stop_llm_owner_processes_locally(
                        owner_id,
                        timeout=1,
                        settle_timeout=0.1,
                    )
                )
            except Exception as exc:
                cleanup_error.append(exc)

        cleanup_thread = threading.Thread(target=cleanup)
        cleanup_thread.start()
        cleanup_thread.join(timeout=0.3)
        assert cleanup_thread.is_alive(), (
            "cleanup passed the inherited owner lock before the first marker-bearing exec"
        )

        os.write(release_write, b"1")
        cleanup_thread.join(timeout=3)

        assert not cleanup_thread.is_alive()
        assert cleanup_error == []
        assert cleanup_result == [{"stopped_process_groups": [child_pid]}]
        assert llm_instance._owner_process_groups(owner_id) == set()
    finally:
        try:
            os.write(release_write, b"1")
        except OSError:
            pass
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=2)
        if child_pid is not None:
            try:
                os.killpg(child_pid, llm_instance.signal.SIGKILL)
            except ProcessLookupError:
                pass
        if launcher.is_alive():
            launcher.kill()
        launcher.join(timeout=2)
        os.close(ready_read)
        os.close(release_write)


@pytest.mark.skipif(os.name != "posix", reason="requires a process-shared file lock")
def test_owner_cleanup_waits_for_inflight_popen_before_sweeping(
    monkeypatch,
    tmp_path,
):
    owner_id = "owner-inflight-launch"
    popen_entered = threading.Event()
    allow_popen_return = threading.Event()
    sweep_started = threading.Event()
    launch_errors = []
    cleanup_errors = []
    fake_process = object()
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))

    def blocking_popen(*args, **kwargs):
        popen_entered.set()
        assert allow_popen_return.wait(timeout=2)
        return fake_process

    def record_sweep(*args, **kwargs):
        sweep_started.set()
        return set()

    monkeypatch.setattr(llm_instance.subprocess, "Popen", blocking_popen)
    monkeypatch.setattr(llm_instance, "_stop_marked_process_groups", record_sweep)

    def launch():
        try:
            with llm_instance._owner_launch_guard(owner_id):
                assert llm_instance.subprocess.Popen(
                    ["model-server"],
                    env={},
                    start_new_session=True,
                ) is fake_process
        except Exception as exc:
            launch_errors.append(exc)

    def cleanup():
        try:
            llm_instance.stop_llm_owner_processes_locally(
                owner_id,
                timeout=1,
                settle_timeout=0,
            )
        except Exception as exc:
            cleanup_errors.append(exc)

    launch_thread = threading.Thread(target=launch)
    cleanup_thread = threading.Thread(target=cleanup)
    launch_thread.start()
    assert popen_entered.wait(timeout=1)
    cleanup_thread.start()

    cleanup_thread.join(timeout=0.1)
    assert cleanup_thread.is_alive()
    assert not sweep_started.is_set()

    allow_popen_return.set()
    launch_thread.join(timeout=2)
    cleanup_thread.join(timeout=2)

    assert not launch_thread.is_alive()
    assert not cleanup_thread.is_alive()
    assert launch_errors == []
    assert cleanup_errors == []
    assert sweep_started.is_set()
    with pytest.raises(RuntimeError, match="refusing a late model launch"):
        with llm_instance._owner_launch_guard(owner_id):
            pytest.fail("closed owner launch guard must not yield")


def test_owner_cleanup_fans_out_to_every_alive_ray_node(monkeypatch):
    events = []
    head_id = "0" * 56
    worker_id = "1" * 56
    dead_worker_id = "2" * 56

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
            {"NodeID": dead_worker_id, "Alive": False},
        ],
    )
    monkeypatch.setattr(llm_instance, "stop_llm_owner_processes", FakeCleanupTask)
    monkeypatch.setattr(
        llm_instance.ray,
        "get",
        lambda ref, timeout=None: ref.value,
    )

    result = llm_instance.stop_llm_owner_processes_on_cluster("owner-1", timeout=3)

    assert events == [
        (head_id, "owner-1", 3),
        (worker_id, "owner-1", 3),
    ]
    assert result == {
        head_id: {"stopped_process_groups": []},
        worker_id: {"stopped_process_groups": []},
    }


def test_owner_cleanup_rejects_an_unavailable_expected_model_node(monkeypatch):
    head_id = "0" * 56
    worker_id = "1" * 56
    worker_ip = "10.0.0.2"
    monkeypatch.setattr(
        llm_instance.ray,
        "nodes",
        lambda: [
            {
                "NodeID": head_id,
                "NodeManagerAddress": "10.0.0.1",
                "Alive": True,
            },
            {
                "NodeID": worker_id,
                "NodeManagerAddress": worker_ip,
                "Alive": False,
            },
        ],
    )

    with pytest.raises(RuntimeError, match="unverified on unavailable Ray nodes"):
        llm_instance.stop_llm_owner_processes_on_cluster(
            "owner-1",
            expected_nodes={worker_id: worker_ip},
        )


def test_owner_cleanup_accepts_a_rejoined_node_with_the_same_ip(monkeypatch):
    events = []
    old_worker_id = "1" * 56
    new_worker_id = "2" * 56
    worker_ip = "10.0.0.2"

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
        lambda: [{
            "NodeID": new_worker_id,
            "NodeManagerAddress": worker_ip,
            "Alive": True,
        }],
    )
    monkeypatch.setattr(llm_instance, "stop_llm_owner_processes", FakeCleanupTask)
    monkeypatch.setattr(
        llm_instance.ray,
        "get",
        lambda ref, timeout=None: ref.value,
    )

    result = llm_instance.stop_llm_owner_processes_on_cluster(
        "owner-1",
        timeout=3,
        expected_nodes={old_worker_id: worker_ip},
    )

    assert events == [(new_worker_id, "owner-1", 3)]
    assert result == {new_worker_id: {"stopped_process_groups": []}}


def test_scheduler_sends_each_owner_node_receipt_only_once():
    scheduler = object.__new__(Scheduler)
    scheduler.llm_instance_manager = LlmInstanceManager(owner_id="owner-1")
    receipts = []
    scheduler.owner_node_sender = SimpleNamespace(send=receipts.append)
    node_id = "1" * 56

    scheduler._record_llm_owner_node(node_id, "10.0.0.2")
    scheduler._record_llm_owner_node(node_id, "10.0.0.2")

    assert receipts == [{"node_id": node_id, "node_ip": "10.0.0.2"}]


def test_manager_shutdown_gate_prevents_new_actor_creation(monkeypatch):
    manager = LlmInstanceManager(owner_id="owner-1")
    manager.begin_shutdown()
    monkeypatch.setattr(
        llm_instance.LLMServerActor,
        "options",
        lambda **_kwargs: SimpleNamespace(
            remote=lambda **_actor_args: pytest.fail(
                "shutdown manager must not create a model actor"
            )
        ),
    )

    with pytest.raises(RuntimeError, match="shutting down"):
        manager.start_llm_instance(
            instance_id="instance",
            model="model",
            node_ip="10.0.0.2",
            node_id="1" * 56,
            gpu_id=0,
            resources={"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 0},
        )


def test_manager_rejects_duplicate_instance_before_actor_creation(monkeypatch):
    manager = LlmInstanceManager(owner_id="owner-1")
    instance_id = "instance"
    node_id = "1" * 56
    existing_actor = object()
    resources = {"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 0}
    manager._register_starting_instance(
        instance_id,
        existing_actor,
        "existing-model",
        "vllm",
        node_id,
        "10.0.0.2",
        0,
        resources,
        "existing-lease",
    )
    monkeypatch.setattr(
        llm_instance.LLMServerActor,
        "options",
        lambda **_kwargs: SimpleNamespace(
            remote=lambda **_actor_args: pytest.fail(
                "duplicate instance must not create a model actor"
            )
        ),
    )

    with pytest.raises(RuntimeError, match="already registered"):
        manager.start_llm_instance(
            instance_id=instance_id,
            model="new-model",
            node_ip="10.0.0.2",
            node_id=node_id,
            gpu_id=0,
            resources=resources,
            lease_id="new-lease",
        )

    assert manager.id_to_instance_actor == {instance_id: existing_actor}


@pytest.mark.parametrize(
    ("backend", "expected_paths"),
    [
        ("vllm", ["/health", "/v1/models", "/v1/chat/completions"]),
        ("transformers", ["/health", "/v1/models", "/v1/chat/completions"]),
    ],
)
def test_readiness_uses_backend_specific_probe_and_common_chat_warmup(
    monkeypatch,
    backend,
    expected_paths,
):
    calls = []

    class FakeResponse:
        def __init__(self, payload=None, status_code=200):
            self.payload = payload or {}
            self.status_code = status_code

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_get(url, timeout):
        calls.append(("GET", url, timeout))
        if url.endswith("/v1/models"):
            return FakeResponse({"data": [{"id": "/models/qwen"}]})
        return FakeResponse()

    def fake_post(url, json, timeout):
        calls.append(("POST", url, timeout, json))
        return FakeResponse({"choices": [{"message": {"content": "READY"}}]})

    monkeypatch.setattr(llm_instance.requests, "get", fake_get)
    monkeypatch.setattr(llm_instance.requests, "post", fake_post)
    monkeypatch.setattr(llm_instance.ray, "get", fake_ray_get)
    events = []
    actor = FakeActor(events)
    manager = LlmInstanceManager()
    manager._register_starting_instance(
        instance_id="instance-1",
        actor=actor,
        model="/models/qwen",
        backend=backend,
        node_id="node-1",
        node_ip="10.0.0.2",
        gpu_id=0,
        resources={"cpu": 1, "cpu_mem": 1024, "gpu": 1, "gpu_mem": 0},
        lease_id="lease-1",
    )

    manager._wait_until_ready(
        instance_id="instance-1",
        actor=actor,
        node_ip="10.0.0.2",
        port="8123",
        model="/models/qwen",
        backend=backend,
        backend_args={},
        timeout=1,
    )

    assert [call[1].removeprefix("http://10.0.0.2:8123") for call in calls] == expected_paths
    assert calls[-1][3]["model"] == "/models/qwen"


def test_transformers_local_cache_exposes_exact_model_and_is_cleaned(tmp_path):
    from transformers.cli.serving.model_manager import ModelManager

    model = tmp_path / "model-cache" / "qwen"
    model.mkdir(parents=True)
    (model / "config.json").write_text(
        '{"architectures": ["Qwen2ForCausalLM"]}',
        encoding="utf-8",
    )
    instance_id = "instance-cache-test"

    cache_dir = llm_instance.prepare_transformers_cache(str(model), instance_id)
    try:
        config_links = list((llm_instance.Path(cache_dir)).glob("models--*/snapshots/*/config.json"))
        assert len(config_links) == 1
        assert config_links[0].resolve() == (model / "config.json").resolve()
        assert ModelManager.get_gen_models(cache_dir) == [
            {
                "owned_by": "",
                "id": str(model),
                "object": "model",
                "created": pytest.approx(model.stat().st_mtime),
            }
        ]
    finally:
        llm_instance.cleanup_transformers_cache(instance_id)

    llm_instance.cleanup_transformers_cache(instance_id)
    assert not llm_instance._transformers_cache_root(instance_id).exists()


def test_transformers_cache_cleanup_cannot_escape_temp_dir(monkeypatch, tmp_path):
    temp_dir = tmp_path / "temp"
    outside = tmp_path / "outside"
    temp_dir.mkdir()
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(temp_dir))

    instance_id = "../../../outside"
    cache_root = llm_instance._transformers_cache_root(instance_id)
    llm_instance.cleanup_transformers_cache(instance_id)

    assert cache_root.parent == temp_dir
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("error", [PermissionError("denied"), FileNotFoundError("child")])
def test_transformers_cache_cleanup_propagates_unconfirmed_errors(
    monkeypatch,
    tmp_path,
    error,
):
    monkeypatch.setattr(llm_instance.tempfile, "gettempdir", lambda: str(tmp_path))
    cache_root = llm_instance._transformers_cache_root("instance-cleanup-error")
    cache_root.mkdir()

    def fail_cleanup(_path):
        raise error

    monkeypatch.setattr(llm_instance.shutil, "rmtree", fail_cleanup)

    with pytest.raises(type(error), match=str(error)):
        llm_instance.cleanup_transformers_cache("instance-cleanup-error")


def test_transformers_rejects_local_model_path_that_cache_cannot_encode(tmp_path):
    model = tmp_path / "model--cache" / "qwen"
    model.mkdir(parents=True)

    with pytest.raises(ValueError, match="losslessly"):
        llm_instance.validate_transformers_model(str(model))


def test_transformers_rejects_local_model_path_over_cache_name_limit(tmp_path):
    model = tmp_path
    for index in range(24):
        model /= f"segment-{index:02d}"
    model.mkdir(parents=True)

    with pytest.raises(ValueError, match="too long"):
        llm_instance.validate_transformers_model(str(model))


@pytest.mark.parametrize(
    ("backend", "backend_args", "model", "error_text"),
    [
        ("unsupported", {}, "/models/qwen", "Unsupported model backend"),
        (
            "transformers",
            {"max_model_len": 4096},
            "/models/qwen",
            "Transformers backend does not support vLLM arguments",
        ),
        ("transformers", {}, None, "losslessly"),
    ],
)
def test_scheduler_rejects_invalid_model_configuration_before_resource_selection(
    tmp_path,
    backend,
    backend_args,
    model,
    error_text,
):
    class StopLoop(Exception):
        pass

    if model is None:
        local_model = tmp_path / "model--cache"
        local_model.mkdir()
        model = str(local_model)

    message = SimpleNamespace(
        message_type="start_llm_instance",
        message_data={
            "instance_id": "instance-1",
            "model": model,
            "backend": backend,
            "backend_args": backend_args,
            "cpu_nums": 1,
            "memory": 1024,
            "gpu_nums": 1,
            "gpu_mem": 0,
        },
    )

    class FakeQueue:
        def __init__(self):
            self.message = message

        def get(self):
            if self.message is None:
                raise StopLoop
            current = self.message
            self.message = None
            return current

    sent = []

    class FakeSocket:
        def connect(self, _address):
            return None

        def send(self, payload):
            sent.append(json.loads(payload))

    class FakeContext:
        def socket(self, _socket_type):
            return FakeSocket()

    selections = []

    def select_node(**kwargs):
        selections.append(kwargs)
        return None

    scheduler = object.__new__(Scheduler)
    scheduler.context = FakeContext()
    scheduler.llm_instance_queue = FakeQueue()
    scheduler.resource_manager = SimpleNamespace(select_node=select_node)
    scheduler.lock = threading.Lock()

    with pytest.raises(StopLoop):
        scheduler._llm_instance_thread(12345)

    assert selections == []
    assert len(sent) == 1
    assert sent[0]["type"] == "fail_llm_instance_launch"
    assert sent[0]["data"]["instance_id"] == "instance-1"
    assert sent[0]["data"]["backend"] == backend
    assert error_text in sent[0]["data"]["error"]


def test_scheduler_ray_commands_use_current_interpreter(monkeypatch):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(scheduler_module.subprocess, "run", fake_run)
    monkeypatch.setattr(scheduler_module.os, "_exit", lambda code: None)
    scheduler = object.__new__(Scheduler)
    scheduler.ray_head_port = 16379
    scheduler.llm_instance_manager = LlmInstanceManager()

    scheduler._launch_ray_head()
    scheduler._cleanup()

    ray_executable = scheduler_module.os.path.join(
        scheduler_module.os.path.dirname(scheduler_module.sys.executable),
        "ray.exe" if scheduler_module.os.name == "nt" else "ray",
    )
    assert commands[0] == [ray_executable, "start", "--head", "--port", "16379"]
    assert commands[1] == [ray_executable, "stop", "--force"]


@pytest.mark.parametrize(
    ("backend", "backend_args"),
    [
        ("vllm", {"gpu_memory_utilization": 0.8}),
        ("transformers", {}),
    ],
)
def test_manager_registers_backend_and_stops_synchronously(
    monkeypatch,
    backend,
    backend_args,
):
    events = []
    install_fake_actor(monkeypatch, events)
    manager = LlmInstanceManager(owner_id="owner-1")

    assert manager_start(
        manager,
        backend=backend,
        backend_args=backend_args,
    ) == {
        "instance_id": "instance-1",
        "model": "/models/qwen",
        "backend": backend,
        "host": "127.0.0.1",
        "port": "8123",
        "endpoint": "http://127.0.0.1:8123/v1",
        "status": "ready",
    }
    resource_detail = manager.get_instance_resource_detail("instance-1")
    assert resource_detail["lease_id"] == "lease-1"
    assert resource_detail["backend"] == backend
    assert resource_detail["status"] == "ready"
    assert (
        "actor",
        (),
        {
            "instance_id": "instance-1",
            "model": "/models/qwen",
            "gpu_id": 0,
            "backend": backend,
            "backend_args": backend_args,
            "owner_id": "owner-1",
        },
    ) in events
    assert manager.get_instance_state("instance-1") == "ready"

    resource_detail = manager.stop_llm_instance("instance-1")

    assert [event[0] for event in events].index("stop") < [
        event[0] for event in events
    ].index("kill")
    assert resource_detail["lease_id"] == "lease-1"
    assert resource_detail["backend"] == backend
    assert manager.get_instance_state("instance-1") == "stopped"
    assert manager.has_instance("instance-1")
    manager.finalize_stopped_instance("instance-1")
    assert "instance-1" not in manager.id_to_instance_actor


def test_manager_keeps_failed_instance_until_lease_release(monkeypatch):
    events = []
    install_fake_actor(monkeypatch, events, RuntimeError("startup failed"))
    manager = LlmInstanceManager()

    with pytest.raises(RuntimeError, match="startup failed"):
        manager_start(manager)

    assert manager.get_instance_state("instance-1") == "stopped"
    assert manager.get_instance_resource_detail("instance-1")["lease_id"] == "lease-1"
    assert "stop" in [event[0] for event in events]
    assert "kill" in [event[0] for event in events]


def test_manager_retains_lease_record_when_process_cleanup_is_unconfirmed(monkeypatch):
    events = []
    install_fake_actor(
        monkeypatch,
        events,
        start_error=RuntimeError("startup failed"),
        cleanup_error=RuntimeError("cleanup unavailable"),
    )
    manager = LlmInstanceManager()

    with pytest.raises(RuntimeError, match="cleanup is pending"):
        manager_start(manager)

    assert manager.get_instance_state("instance-1") == "cleanup_pending"
    assert manager.get_instance_resource_detail("instance-1")["lease_id"] == "lease-1"


@pytest.mark.parametrize(
    ("state", "expected_events", "expected_result"),
    [
        ("stopped", ["release_instance", "finalize"], True),
        (None, ["release_lease"], True),
        ("cleanup_pending", [], False),
    ],
)
def test_scheduler_releases_launch_lease_only_after_cleanup_confirmation(
    state,
    expected_events,
    expected_result,
):
    events = []

    class FakeManager:
        def get_instance_state(self, instance_id):
            return state

        def get_instance_resource_detail(self, instance_id):
            return {"lease_id": "lease-1"}

        def finalize_stopped_instance(self, instance_id):
            events.append("finalize")

    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.Lock()
    scheduler.llm_instance_manager = FakeManager()
    scheduler.resource_manager = SimpleNamespace(
        release_instance_resource=lambda detail: events.append("release_instance"),
        release_lease=lambda lease_id: events.append("release_lease"),
    )

    result = scheduler._rollback_failed_llm_launch("instance-1", "lease-1")

    assert result is expected_result
    assert events == expected_events


@pytest.mark.parametrize(
    ("cleanup_error", "expected_message_type"),
    [
        (None, "finish_llm_instance_stop"),
        (RuntimeError("cleanup failed"), "fail_llm_instance_stop"),
    ],
)
def test_scheduler_stop_ack_only_after_cleanup_confirmation_and_lease_release(
    cleanup_error,
    expected_message_type,
):
    manager = ResourceManager()
    capacity = {
        "cpu": 4,
        "cpu_mem": 4096,
        "gpu_resource": {
            0: {"gpu_id": 0, "gpu_num": 1, "gpu_mem": 24000},
        },
    }
    manager.nodes["node-1"] = Node(
        "node-1",
        "127.0.0.1",
        capacity,
        capacity,
    )
    manager.running_task_counts["node-1"] = 0
    manager._ray_node_index = lambda: {}
    manager._is_node_alive = lambda *_: True
    selection = manager.select_node(
        {"cpu": 1, "cpu_mem": 1024, "gpu": 1, "gpu_mem": 18000},
        reservation_kind="instance",
        run_id="instance-1",
    )
    assert selection

    entered = threading.Event()
    allow_cleanup = threading.Event()
    events = []
    messages = []

    class FakeManager:
        def stop_llm_instance(self, instance_id):
            events.append("stop_entered")
            entered.set()
            assert allow_cleanup.wait(timeout=2)
            if cleanup_error is not None:
                events.append("cleanup_failed")
                raise cleanup_error
            events.append("cleanup_confirmed")
            return {
                "lease_id": selection.lease_id,
                "backend": "transformers",
            }

        def finalize_stopped_instance(self, instance_id):
            events.append("finalize")

    original_release = manager.release_instance_resource

    def release_instance(resource_detail):
        events.append("release_lease")
        return original_release(resource_detail)

    manager.release_instance_resource = release_instance

    class FakeSocket:
        def send(self, payload):
            events.append("ack")
            messages.append(json.loads(payload))

    scheduler = object.__new__(Scheduler)
    scheduler.lock = threading.Lock()
    scheduler.resource_manager = manager
    scheduler.llm_instance_manager = FakeManager()
    thread = threading.Thread(
        target=scheduler._handle_llm_instance_stop,
        args=(
            FakeSocket(),
            {"instance_id": "instance-1", "request_id": "request-1"},
        ),
    )
    thread.start()
    assert entered.wait(timeout=1)

    assert selection.lease_id in manager.active_leases
    assert messages == []
    allow_cleanup.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert messages[0]["type"] == expected_message_type
    if cleanup_error is None:
        assert messages[0]["data"]["backend"] == "transformers"
        assert selection.lease_id not in manager.active_leases
        assert events == [
            "stop_entered",
            "cleanup_confirmed",
            "release_lease",
            "finalize",
            "ack",
        ]
    else:
        assert selection.lease_id in manager.active_leases
        assert events == ["stop_entered", "cleanup_failed", "ack"]


@pytest.mark.asyncio
async def test_mapath_round_trips_backend_for_start_and_stop():
    path = object.__new__(MaPath)
    path.scheduler_process = SimpleNamespace(is_alive=lambda: True, pid=123, exitcode=None)
    path.llm_instance_async_que = {}
    messages = []

    def respond(message):
        messages.append(message)
        data = message["data"]
        if message["type"] == "start_llm_instance":
            response = {
                "type": "finish_llm_instance_launch",
                "data": {
                    "instance_id": data["instance_id"],
                    "model": data["model"],
                    "backend": data["backend"],
                    "host": "10.0.0.2",
                    "port": "8123",
                    "endpoint": "http://10.0.0.2:8123/v1",
                    "status": "ready",
                },
            }
            queue_id = data["instance_id"]
        else:
            response = {
                "type": "finish_llm_instance_stop",
                "data": {
                    "instance_id": data["instance_id"],
                    "backend": "transformers",
                    "request_id": data["request_id"],
                },
            }
            queue_id = data["request_id"]
        path.llm_instance_async_que[queue_id].put_nowait(response)

    path._send_scheduler_message = respond
    info = await path.start_llm_instance(
        "instance-1",
        "/models/qwen",
        5,
        1,
        1024,
        0,
        backend="transformers",
    )

    assert info["backend"] == "transformers"
    assert info["status"] == "ready"
    assert messages[0]["data"]["backend"] == "transformers"
    assert messages[0]["data"]["backend_args"] == {}

    stopped = await path.stop_llm_instance("instance-1")
    assert stopped["backend"] == "transformers"
    assert stopped["request_id"]
    assert path.llm_instance_async_que == {}


@pytest.mark.asyncio
async def test_mapath_propagates_launch_failure_and_waits_for_stop_ack():
    path = object.__new__(MaPath)
    path.scheduler_process = SimpleNamespace(is_alive=lambda: True, pid=123, exitcode=None)
    path.llm_instance_async_que = {}

    def respond(message):
        instance_id = message["data"]["instance_id"]
        if message["type"] == "start_llm_instance":
            response = {
                "type": "fail_llm_instance_launch",
                "data": {"instance_id": instance_id, "error": "bad model"},
            }
            queue_id = instance_id
        else:
            request_id = message["data"]["request_id"]
            response = {
                "type": "finish_llm_instance_stop",
                "data": {
                    "instance_id": instance_id,
                    "request_id": request_id,
                },
            }
            queue_id = request_id
        path.llm_instance_async_que[queue_id].put_nowait(response)

    path._send_scheduler_message = respond
    with pytest.raises(RuntimeError, match="bad model"):
        await path.start_llm_instance(
            "instance-1",
            "/bad/model",
            5,
            1,
            1024,
            0,
        )
    assert path.llm_instance_async_que == {}

    stopped = await path.stop_llm_instance("instance-1")
    assert stopped["instance_id"] == "instance-1"
    assert stopped["request_id"]
    assert path.llm_instance_async_que == {}


@pytest.mark.asyncio
async def test_concurrent_stop_requests_use_distinct_response_queues():
    path = object.__new__(MaPath)
    path.scheduler_process = SimpleNamespace(is_alive=lambda: True, pid=123, exitcode=None)
    path.llm_instance_async_que = {}
    messages = []

    def respond_after_both(message):
        messages.append(message)
        if len(messages) != 2:
            return
        for item in reversed(messages):
            data = item["data"]
            request_id = data["request_id"]
            path.llm_instance_async_que[request_id].put_nowait(
                {
                    "type": "finish_llm_instance_stop",
                    "data": {
                        "instance_id": data["instance_id"],
                        "request_id": request_id,
                    },
                }
            )

    path._send_scheduler_message = respond_after_both
    first, second = await asyncio.gather(
        path.stop_llm_instance("instance-1"),
        path.stop_llm_instance("instance-1"),
    )

    assert first["instance_id"] == "instance-1"
    assert second["instance_id"] == "instance-1"
    assert first["request_id"] != second["request_id"]
    assert path.llm_instance_async_que == {}
