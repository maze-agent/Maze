from __future__ import annotations

import json
import os
import queue

from maze.core.scheduler.llm_instance import LlmInstanceMessage
from maze.core.scheduler.llm_instance import LlmInstanceManager
from maze.core.scheduler.runner import _apply_model_route_env
from maze.core.scheduler.runtime import TaskRuntime
from maze.core.scheduler.scheduler import Scheduler


def register_qwen_instances(manager: LlmInstanceManager):
    manager.register_instance(
        "instance-1",
        "qwen",
        "10.0.0.1",
        "node-a",
        0,
        "8100",
        {"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 1024},
    )
    manager.register_instance(
        "instance-2",
        "qwen",
        "10.0.0.2",
        "node-b",
        0,
        "8101",
        {"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 1024},
    )


def test_same_workflow_model_routes_to_same_instance_for_kv_cache_reuse():
    manager = LlmInstanceManager()
    register_qwen_instances(manager)
    anchor = {"local_model": "qwen", "backend": "vllm"}

    first = manager.route_model_request("run-1", anchor)
    second = manager.route_model_request("run-1", anchor)

    assert first["instance_id"] == "instance-1"
    assert second["instance_id"] == "instance-1"
    assert second["affinity_hit"] is True
    assert manager.id_to_instance_metadata["instance-1"]["inflight_requests"] == 2


def test_new_workflow_model_route_uses_least_loaded_instance():
    manager = LlmInstanceManager()
    register_qwen_instances(manager)
    anchor = {"local_model": "qwen", "backend": "vllm"}

    first = manager.route_model_request("run-1", anchor)
    second = manager.route_model_request("run-2", anchor)

    assert first["instance_id"] == "instance-1"
    assert second["instance_id"] == "instance-2"


def test_model_route_release_and_stop_clean_affinity():
    manager = LlmInstanceManager()
    register_qwen_instances(manager)
    anchor = {"local_model": "qwen", "backend": "vllm"}
    route = manager.route_model_request("run-1", anchor)

    manager.release_model_route(route)
    assert manager.id_to_instance_metadata["instance-1"]["inflight_requests"] == 0

    manager.stop_llm_instance("instance-1")
    next_route = manager.route_model_request("run-1", anchor)
    assert next_route["instance_id"] == "instance-2"
    assert next_route["affinity_hit"] is False


def test_runner_model_route_environment(monkeypatch):
    route = {
        "model": "qwen",
        "instance_id": "instance-1",
        "endpoint": "http://10.0.0.1:8100",
    }

    _apply_model_route_env(route)
    assert json.loads(os.environ["MAZE_MODEL_ROUTE"])["instance_id"] == "instance-1"
    assert os.environ.get("MAZE_MODEL_ENDPOINT") == "http://10.0.0.1:8100"

    _apply_model_route_env(None)
    assert os.environ.get("MAZE_MODEL_ROUTE") is None
    assert os.environ.get("MAZE_MODEL_ENDPOINT") is None


def test_scheduler_assigns_and_releases_model_route():
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.llm_instance_manager = LlmInstanceManager()
    scheduler.llm_instance_manager.register_instance(
        "instance-1",
        "qwen",
        "10.0.0.1",
        "node-a",
        0,
        "8100",
        {"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 1024},
    )
    task = TaskRuntime(
        workflow_id="run-1",
        task_id="task-1",
        task_input={"input_params": {}},
        task_output={"output_params": {}},
        resources={"cpu_num": 1, "gpu_mem": 1024, "io_num": 0},
        task_kind="gpu",
        model_anchor={"local_model": "qwen", "backend": "vllm"},
    )
    decision = {}

    route = Scheduler._assign_model_route(scheduler, task, decision)

    assert route["instance_id"] == "instance-1"
    assert decision["model_route"]["endpoint"] == "http://10.0.0.1:8100"
    assert task.model_route["instance_id"] == "instance-1"

    Scheduler._release_model_route(scheduler, task)
    assert task.model_route is None
    assert scheduler.llm_instance_manager.id_to_instance_metadata["instance-1"]["inflight_requests"] == 0


def test_scale_out_recommendation_uses_pending_model_demand():
    manager = LlmInstanceManager(scale_out_threshold=1.0)
    manager.record_model_demand({"local_model": "qwen", "backend": "vllm", "gpu_mem": 2048})

    recommendations = manager.scale_out_recommendations()

    assert recommendations[0]["model"] == "qwen"
    assert recommendations[0]["reason"] == "no_active_instance"
    assert recommendations[0]["gpu_mem"] == 2048

    manager.mark_model_deploying("qwen", "vllm")
    assert manager.scale_out_recommendations() == []


def test_lru_scale_in_candidates_ignore_busy_instances():
    manager = LlmInstanceManager(idle_scale_in_seconds=10)
    metadata = manager.register_instance(
        "instance-1",
        "qwen",
        "10.0.0.1",
        "node-a",
        0,
        "8100",
        {"cpu": 1, "cpu_mem": 0, "gpu": 1, "gpu_mem": 1024},
    )
    metadata["last_used_time"] = 100.0

    assert manager.lru_scale_in_candidates(now=120.0)[0]["instance_id"] == "instance-1"

    metadata["inflight_requests"] = 1
    assert manager.lru_scale_in_candidates(now=120.0) == []


def test_scheduler_enqueues_auto_scale_out_request():
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.llm_instance_manager = LlmInstanceManager(scale_out_threshold=1.0)
    scheduler.llm_instance_queue = queue.Queue()
    scheduler.last_llm_scaling_check = 0.0
    scheduler.resource_manager = None
    scheduler.llm_instance_manager.record_model_demand({
        "local_model": "qwen",
        "backend": "vllm",
        "gpu_mem": 2048,
    })

    Scheduler._manage_llm_instance_scaling(scheduler, now=10.0)

    message = scheduler.llm_instance_queue.get_nowait()
    assert isinstance(message, LlmInstanceMessage)
    assert message.message_type == "start_llm_instance"
    assert message.message_data["model"] == "qwen"
    assert message.message_data["auto_started"] is True
    assert message.message_data["gpu_mem"] == 2048
