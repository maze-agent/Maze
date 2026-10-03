import copy
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from maze.core.resource_history import ResourceHistoryStore
from maze.core.resource_policy import (
    MIB,
    ResourceContext,
    ResourceLimitExceeded,
    context_for_task,
    recommend_resources,
)
from maze.core.workflow.resources import model_anchor_gpu_mem_mb
from maze.core.path.path import MaPath
from maze.core.workflow.task import CodeTask
from maze.core.workflow.workflow import Workflow
from maze.core.workflow.dynamic import DynamicRun
from maze.core.scheduler.runtime import TaskRuntime, WorkflowRuntimeManager
from maze.core.scheduler.resource import ResourceManager, Node
from maze.core.scheduler.llm_instance import LlmInstanceManager
from maze.core.scheduler.scheduler import Scheduler
from maze.core.scheduler.strategy import create_scheduling_strategy
from maze.core.scheduler.queues import HeterogeneousTaskQueues
from maze.core.scheduler.recovery import PendingRecovery
from maze.core.scheduler.error import memory_failure_from_message
from maze.core.fault_tolerance import apply_repair_action, diagnose_failure


def task_context(dtype="bfloat16", tokens=128):
    return context_for_task(
        code_str=f'def run():\n return "{dtype}"',
        task_input={
            "input_params": {
                "1": {"key": "max_tokens", "input_schema": "from_user", "value": tokens}
            }
        },
    )


def test_anchor_has_one_meaning_for_submission_and_deployment():
    anchor = {"gpu_mem": 4096, "estimated_gpu_mem_mb": 8192}
    assert (
        model_anchor_gpu_mem_mb(anchor)
        == LlmInstanceManager._estimated_gpu_mem(None, anchor)
        == 8192
    )


def test_history_survives_restart_and_deduplicates(tmp_path):
    path = tmp_path / "history.json"
    context = task_context()
    observation = dict(
        run_id="run",
        task_id="task",
        task_name="shared-name",
        status="succeeded",
        requested_resources={"gpu_mem": 1024},
        context=context,
        observation_id="attempt-1",
        metrics={"gpu_memory_peak_allocated_bytes": 8 * 1024**3},
    )
    ResourceHistoryStore(path).record(**observation)
    store = ResourceHistoryStore(path)
    store.record(**observation)
    learned = store.lookup(context)
    assert learned["success_samples"] == 1
    decision = recommend_resources(context, {"gpu_mem": 1024}, learned)
    assert decision.effective["gpu_mem"] > 8192
    assert decision.declared["gpu_mem"] == 1024
    assert store.lookup(task_context("float32")) == {}
    assert store.lookup(task_context(tokens=4096)) == {}


def test_failure_is_a_lower_bound_and_respects_limit():
    context = task_context()
    failure = {
        "allocated_bytes": 8 * 1024**3,
        "attempted_allocation_bytes": 2 * 1024**3,
    }
    decision = recommend_resources(context, {"gpu_mem": 4096}, failure=failure)
    assert decision.effective["gpu_mem"] > 10 * 1024
    with pytest.raises(ResourceLimitExceeded):
        recommend_resources(
            context, {"gpu_mem": 4096, "gpu_mem_limit": 5000}, failure=failure
        )


def test_routed_caller_peak_cannot_train_instance_memory(tmp_path):
    store = ResourceHistoryStore(tmp_path / "history.json")
    ctx = context_for_task(model_anchor={"local_model": "qwen", "dtype": "bfloat16"})
    store.record(
        run_id="run",
        task_id="caller",
        task_name="caller",
        status="succeeded",
        requested_resources={"gpu_mem": 8000},
        context=ctx,
        metrics={"gpu_memory_peak_allocated_bytes": 32 * MIB, "memory_scope": "task"},
    )
    assert store.lookup(ctx)["success_samples"] == 0
    assert store.lookup(ctx)["success_peak_bytes"] == 0


def test_static_and_dynamic_submission_share_history(tmp_path):
    core = MaPath.__new__(MaPath)
    core.resource_history = ResourceHistoryStore(tmp_path / "history.json")
    task = CodeTask("run", "task", "gpu-function")
    task.save_task(
        {"input_params": {}},
        {},
        "def run(): return 1",
        None,
        {"gpu_mem": 1024},
        task_kind="gpu",
    )
    ctx = context_for_task(code_str=task.code_str, task_input=task.task_input)
    core.resource_history.record(
        run_id="old",
        task_id="old",
        task_name="gpu-function",
        status="succeeded",
        requested_resources=task.resources,
        context=ctx,
        metrics={"gpu_memory_peak_reserved_bytes": 4096 * MIB},
    )
    workflow = Workflow("run")
    workflow.add_task("task", task)
    dynamic = DynamicRun("run")
    core._static_scheduling_context = lambda *args: {}
    core._dynamic_scheduling_context = lambda *args: {}
    static_payload = core._task_run_payload(workflow, task, "run")
    dynamic_payload = core._dynamic_task_run_payload(dynamic, task)
    assert static_payload["resources"] == dynamic_payload["resources"]
    assert static_payload["resources"]["gpu_mem"] > 4096
    assert task.resources["gpu_mem"] == 1024


def resource_manager(capacity=24576):
    manager = ResourceManager()
    resource = {
        "cpu": 8,
        "cpu_mem": 1024**3,
        "gpu_resource": {
            0: {"gpu_num": 1, "gpu_mem": capacity, "gpu_capacity": capacity}
        },
    }
    manager.nodes["node"] = Node("node", "127.0.0.1", resource, resource)
    manager._ray_node_index = lambda: {"node": {"Alive": True}}
    manager._refresh_head_local_models = lambda: None
    return manager


def test_busy_gpu_is_not_a_permanent_capacity_failure():
    manager = resource_manager()
    request = {"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 4096}
    first = manager.select_node(request)
    assert first
    assert manager.select_node(request).decision["reason"] == "insufficient_gpu"
    impossible = manager.select_node({**request, "gpu_mem": 32768})
    assert impossible.decision["reason"] == "resource_capacity_exceeded"
    assert manager.release_lease(first.lease_id)
    assert not manager.release_lease(first.lease_id)
    assert (
        manager.nodes["node"].available_resources["gpu_resource"][0]["gpu_mem"] == 24576
    )


def recovery_fixture(failure=False, cancelled=False):
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.resource_manager = resource_manager()
    scheduler.workflow_manager = WorkflowRuntimeManager()
    scheduler.stopped_workflow_ids = set()
    scheduler.task_queues = HeterogeneousTaskQueues(create_scheduling_strategy("FCFS"))
    ctx = task_context()
    task = TaskRuntime(
        "run",
        "task",
        {"input_params": {}},
        {},
        {"gpu_mem": 4096},
        task_kind="gpu",
        resource_context=ctx.to_dict(),
    )
    selected = scheduler.resource_manager.select_node(task.scheduler_resources)
    task.begin_attempt("dispatch", selected.lease_id)
    task.selected_node = selected.selected_node
    task.status = "recovering"
    task.last_schedule_decision = {"resource_context": ctx.to_dict()}
    scheduler.workflow_manager.add_task(task)
    if cancelled:
        scheduler.workflow_manager.workflows.clear()
    error = {
        "error_type": "resource_insufficient",
        "message": "CUDA out of memory",
        "retryable": True,
        "memory_failure": {
            "scope": "task",
            "device_type": "cuda",
            "peak_bytes": 4096 * MIB,
        },
    }
    repair = apply_repair_action(task, error, diagnose_failure(error, task))
    future = Future()
    if failure:
        future.set_exception(RuntimeError("cleanup failed"))
    else:
        future.set_result(None)
    item = PendingRecovery(
        task, (task.attempt, task.dispatch_id, task.lease_id), error, repair, future
    )
    scheduler.resource_recovery = SimpleNamespace(completed=lambda: [item])
    events = []
    scheduler._send_task_retry = lambda socket, t, err, outbound, callback: (
        events.append(err),
        callback(),
    )
    scheduler._retry_or_fail_task = lambda socket, t, err, **kwargs: events.append(err)
    return scheduler, task, selected.lease_id, events


def test_recovery_releases_old_lease_and_updates_demand():
    scheduler, task, old_lease, events = recovery_fixture()
    scheduler._drain_resource_recoveries(None, [])
    assert old_lease not in scheduler.resource_manager.active_leases
    assert task.resources["gpu_mem"] > 4096
    assert task.declared_resources["gpu_mem"] == 4096
    assert scheduler.task_queues.peek("gpu") is task
    assert events[0]["repair_action"]["type"] == "resource_reanchor"


def test_failed_cleanup_keeps_lease_reserved():
    scheduler, task, old_lease, events = recovery_fixture(failure=True)
    scheduler._drain_resource_recoveries(None, [])
    assert old_lease in scheduler.resource_manager.active_leases
    assert scheduler.task_queues.peek("gpu") is None
    assert events[0]["error_type"] == "resource_cleanup_failed"


def test_cancelled_recovery_does_not_start_new_attempt():
    scheduler, task, old_lease, events = recovery_fixture(cancelled=True)
    scheduler._drain_resource_recoveries(None, [])
    assert old_lease not in scheduler.resource_manager.active_leases
    assert scheduler.task_queues.peek("gpu") is None
    assert events == []


def test_instance_pressure_preserves_other_inflight_requests():
    manager = LlmInstanceManager()
    manager.register_instance(
        "instance", "qwen", "127.0.0.1", "node", 0, 8001, {}, backend="transformers"
    )
    anchor = {"local_model": "qwen", "backend": "transformers"}
    manager.route_model_request("a", anchor, task_id="a")
    manager.route_model_request("b", anchor, task_id="b")
    assert manager.prepare_memory_recovery("instance")
    assert manager.id_to_state["instance"] == "ready"
    assert manager.id_to_instance_metadata["instance"]["inflight_requests"] == 2
    assert manager.route_model_request("c", anchor, task_id="c") is None
    manager.release_model_route({"instance_id": "instance"})
    manager.release_model_route({"instance_id": "instance"})
    assert (
        manager.route_model_request("c", anchor, task_id="c")["instance_id"]
        == "instance"
    )


def test_structured_cuda_failure_keeps_failed_allocation():
    failure = memory_failure_from_message(
        "CUDA out of memory. Tried to allocate 2.00 GiB. "
        "GPU 0 has a total capacity of 24.00 GiB. Of the allocated memory 8.00 GiB is allocated by PyTorch"
    )
    assert failure["device_type"] == "cuda"
    assert failure["attempted_allocation_bytes"] == 2 * 1024**3
    assert failure["allocated_bytes"] == 8 * 1024**3


def test_model_base_profile_applies_without_poisoning_other_input_sizes(tmp_path):
    store = ResourceHistoryStore(tmp_path / "history.json")
    anchor = {"local_model": "qwen", "backend": "transformers", "revision": "v1"}
    base = context_for_task(model_anchor=anchor)
    request = context_for_task(
        model_anchor=anchor,
        task_input={
            "input_params": {
                "1": {"key": "max_tokens", "input_schema": "from_user", "value": 1024}
            }
        },
    )
    store.record(
        run_id="load",
        task_id="load",
        task_name="qwen",
        status="failed",
        context=base,
        requested_resources={"gpu_mem": 8192},
        required_gpu_mem_mib=10000,
        error={"memory_failure": {"phase": "load"}},
    )
    assert store.lookup(request)["oom_lower_bound_mib"] == 10000
    store.record(
        run_id="request",
        task_id="inference",
        task_name="qwen",
        status="failed",
        context=request,
        requested_resources={"gpu_mem": 10000},
        required_gpu_mem_mib=16000,
    )
    assert store.lookup(request)["oom_lower_bound_mib"] == 16000
    assert store.lookup(base)["oom_lower_bound_mib"] == 10000


def test_actual_dtype_argument_separates_identical_function_profiles():
    def context(dtype):
        return context_for_task(
            code_str="def run(dtype): return dtype",
            task_input={
                "input_params": {
                    "1": {"key": "dtype", "value": dtype, "input_schema": "from_user"}
                }
            },
        )

    a, b = context("float32"), context("bfloat16")
    assert a.identity == b.identity
    assert a.key != b.key


def test_corrupt_history_is_not_silently_overwritten(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        ResourceHistoryStore(path).load()
    assert path.read_text() == "{broken"


def test_capacity_rejection_after_retry_is_accepted_but_stale_oom_is_not():
    core = MaPath.__new__(MaPath)
    core.task_attempts = {}
    event = {
        "workflow_id": "run",
        "task_id": "task",
        "attempt": 1,
        "dispatch_id": "dispatch",
        "lease_id": "lease",
    }
    assert core._accept_task_attempt_event("start_task", event)
    assert core._accept_task_attempt_event("task_retry", event)
    assert not core._accept_task_attempt_event(
        "task_exception", {**event, "error": {"error_type": "resource_insufficient"}}
    )
    assert core._accept_task_attempt_event(
        "task_exception",
        {
            **event,
            "capacity_rejection": True,
            "error": {"error_type": "resource_capacity_exceeded"},
        },
    )


def test_service_oom_preserves_owning_instance(monkeypatch):
    import io
    import json
    from urllib.error import HTTPError
    from workflows import _inference
    from maze.core.scheduler.error import (
        ModelInferenceError,
        exception_to_error_envelope,
    )

    endpoint = "http://127.0.0.1:8001/v1"
    monkeypatch.setenv(
        "MAZE_MODEL_ROUTE", json.dumps({"instance_id": "owned", "endpoint": endpoint})
    )

    def failing_request(*args, **kwargs):
        raise HTTPError(
            endpoint,
            500,
            "error",
            {},
            io.BytesIO(b'{"error":"CUDA out of memory. Tried to allocate 64.00 MiB"}'),
        )

    monkeypatch.setattr(_inference, "urlopen", failing_request)
    with pytest.raises(ModelInferenceError) as caught:
        _inference.chat(
            [{"role": "user", "content": "hello"}],
            base_url=endpoint,
            model="qwen",
            api_key="",
        )
    error = exception_to_error_envelope("resource_insufficient", caught.value)
    assert error["memory_failure"]["scope"] == "model_instance"
    assert error["memory_failure"]["instance_id"] == "owned"
    assert error["memory_failure"]["attempted_allocation_bytes"] == 64 * MIB


def test_gpu_memory_limit_validates_units_and_value():
    from maze.core.workflow.resources import normalize_resources, ResourceSpecError

    with pytest.raises(ResourceSpecError):
        normalize_resources({"gpu_mem": 128, "gpu_mem_limit": -1})
    assert normalize_resources({"gpu_mem_limit": "4096"})["gpu_mem_limit"] == 4096


def test_instance_load_retry_requires_cleanup_and_stops_after_one_retry():
    from maze.core.scheduler.recovery import plan_model_load_retry

    message = {
        "resource_context": context_for_task(
            model_anchor={"local_model": "qwen"}
        ).to_dict(),
        "gpu_mem": 8192,
        "request_id": "request",
    }
    failure = {
        "device_type": "cuda",
        "allocated_bytes": 8 * 1024**3,
        "attempted_allocation_bytes": 2 * 1024**3,
    }
    assert (
        plan_model_load_retry(message, failure, cleaned=False, cancelled=False) is None
    )
    assert plan_model_load_retry(message, failure, cleaned=True, cancelled=True) is None
    retry = plan_model_load_retry(message, failure, cleaned=True, cancelled=False)
    assert retry["gpu_mem"] > 10 * 1024
    assert retry["request_id"] == message["request_id"]
    assert plan_model_load_retry(retry, failure, cleaned=True, cancelled=False) is None
    with pytest.raises(ResourceLimitExceeded):
        plan_model_load_retry(
            {**message, "model_anchor": {"gpu_mem_limit": 9000}},
            failure,
            cleaned=True,
            cancelled=False,
        )


def test_old_instance_generation_cannot_stop_a_new_server():
    from maze.core.scheduler.recovery import prepare_cleanup

    task = SimpleNamespace(
        failed_model_route={"instance_id": "same-id", "generation_id": "old"},
        last_error={"memory_failure": {"instance_id": "same-id"}},
    )
    manager = SimpleNamespace(
        get_instance_resource_detail=lambda identifier: {"generation_id": "new"}
    )
    with pytest.raises(RuntimeError, match="earlier model generation"):
        prepare_cleanup(
            task,
            {"scope": "model_instance"},
            instances=manager,
            stop_instance=lambda identifier: pytest.fail("stopped new generation"),
            shutting_down=lambda: False,
        )


def test_cancelled_large_request_does_not_raise_small_request_reservation():
    manager = LlmInstanceManager()
    common = {"local_model": "qwen", "backend": "transformers"}
    manager.record_model_demand(
        {**common, "gpu_mem": 8000}, workflow_id="small", task_id="small"
    )
    manager.record_model_demand(
        {**common, "gpu_mem": 16000}, workflow_id="large", task_id="large"
    )
    assert manager.scale_out_recommendations()[0]["gpu_mem"] == 16000
    manager.clear_workflow_state("large")
    assert manager.scale_out_recommendations()[0]["gpu_mem"] == 8000


def test_routing_respects_instance_reservation_and_can_retire_idle_small_instance():
    manager = LlmInstanceManager()
    manager.register_instance(
        "small",
        "qwen",
        "127.0.0.1",
        "node",
        0,
        8001,
        {"gpu_mem": 8000},
        backend="transformers",
    )
    anchor = {"local_model": "qwen", "backend": "transformers", "gpu_mem": 12000}
    assert manager.route_model_request("large", anchor, task_id="large") is None
    assert (
        manager.undersized_idle_instances("qwen", "transformers", 12000)[0][
            "instance_id"
        ]
        == "small"
    )
    assert manager.claim_lru_scale_in("small", idle_seconds=0)
    manager.register_instance(
        "large",
        "qwen",
        "127.0.0.1",
        "node",
        0,
        8002,
        {"gpu_mem": 16000},
        backend="transformers",
    )
    assert (
        manager.route_model_request("large", anchor, task_id="large")["instance_id"]
        == "large"
    )


def test_dynamic_snapshot_keeps_declared_and_effective_resources():
    task = CodeTask("dynamic", "task", "gpu")
    task.save_task(
        {"input_params": {}}, {}, "code", None, {"gpu_mem": 128}, task_kind="gpu"
    )
    task.effective_resources = {"gpu_mem": 384, "cpu_num": 1, "io_num": 0}
    run = DynamicRun("dynamic")
    run.tasks["task"] = task
    snapshot = run.snapshot()["task_nodes"]["task"]
    assert snapshot["resources"]["gpu_mem"] == 384
    assert snapshot["declared_resources"]["gpu_mem"] == 128
    assert task.resources["gpu_mem"] == 128


def test_dynamic_resource_override_preserves_hard_limit():
    from maze.core.workflow.dynamic import merge_dynamic_resources

    assert (
        merge_dynamic_resources({"gpu_mem": 128}, {"gpu_mem_limit": 256})[
            "gpu_mem_limit"
        ]
        == 256
    )


def test_increased_demand_selects_a_larger_device():
    manager = resource_manager(capacity=8192)
    large = {
        "cpu": 8,
        "cpu_mem": 1024**3,
        "gpu_resource": {0: {"gpu_num": 1, "gpu_mem": 24576, "gpu_capacity": 24576}},
    }
    manager.nodes["large"] = Node("large", "127.0.0.2", large, large)
    manager._ray_node_index = lambda: {
        "node": {"Alive": True},
        "large": {"Alive": True},
    }
    selected = manager.select_node({"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 12000})
    assert selected.selected_node.node_id == "large"
    assert selected.decision["candidate_nodes"][0]["can_run"] is False
