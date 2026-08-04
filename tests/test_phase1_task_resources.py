import pytest

from maze import get_task_metadata, task
from maze.core.resource_history import ResourceHistoryStore
from maze.core.scheduler.runtime import TaskRuntime
from maze.core.scheduler.scheduler import Scheduler
from maze.core.workflow.dag_spec import DagSpecError, dag_spec_from_payload
from maze.core.workflow.resources import (
    ResourceSpecError,
    normalize_task_semantics,
    require_schedulable_resources,
    to_internal_scheduler_resources,
)


def _node(**overrides):
    node = {
        "id": "task_a",
        "code": "def run():\n    return {'ok': True}",
        "outputs": ["ok"],
    }
    node.update(overrides)
    return node


def _spec(node):
    return {"nodes": [node], "edges": []}


def test_dag_normalizes_public_resources_and_default_kind():
    spec = dag_spec_from_payload(_spec(_node(resources={"cpu": 2, "gpu": 1, "gpu_mem": 0})))
    node = spec["nodes"][0]

    assert node["task_kind"] == "cpu"
    assert node["resources"] == {"cpu_num": 2, "gpu_mem": 0, "io_num": 0}
    assert "gpu" not in node["resources"]


def test_task_decorator_preserves_scheduler_resource_hints():
    @task(resources={"target_node_id": "head-node", "required_capability": "workspace_sandbox"})
    def pinned_task():
        return {"ok": True}

    assert get_task_metadata(pinned_task).resources == {
        "cpu_num": 1,
        "gpu_mem": 0,
        "io_num": 0,
        "target_node_id": "head-node",
        "required_capability": "workspace_sandbox",
    }


def test_dag_infers_gpu_from_model_anchor():
    spec = dag_spec_from_payload(_spec(_node(model_anchor={"local_model": "qwen", "estimated_gpu_mem_mb": 2048})))

    assert spec["nodes"][0]["task_kind"] == "gpu"
    assert spec["nodes"][0]["resources"]["gpu_mem"] == 2048


def test_dag_rejects_llm_task_kind():
    with pytest.raises(DagSpecError, match="task_kind"):
        dag_spec_from_payload(_spec(_node(task_kind="llm")))


def test_cpu_and_io_tasks_reject_gpu_semantics():
    with pytest.raises(DagSpecError, match="cpu tasks must not request gpu_mem"):
        dag_spec_from_payload(_spec(_node(task_kind="cpu", resources={"gpu_mem": 1024})))

    with pytest.raises(DagSpecError, match="io tasks must not include model_anchor"):
        dag_spec_from_payload(_spec(_node(task_kind="io", model_anchor={"local_model": "qwen"})))


def test_internal_scheduler_adapter_is_not_public_gpu_count():
    task_kind, resources = normalize_task_semantics(
        resources={"cpu_num": 3, "gpu_mem": 4096, "io_num": 1},
        model_anchor={"local_model": "qwen"},
    )
    internal = to_internal_scheduler_resources(resources, task_kind=task_kind)

    assert resources == {"cpu_num": 3, "gpu_mem": 4096, "io_num": 1}
    assert internal["cpu"] == 3
    assert internal["gpu"] == 1
    assert internal["gpu_mem"] == 4096


def test_gpu_task_without_memory_does_not_create_internal_gpu_slot():
    task_kind, resources = normalize_task_semantics(
        task_kind="gpu",
        resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
    )
    internal = to_internal_scheduler_resources(resources, task_kind=task_kind)

    assert internal["gpu"] == 0
    with pytest.raises(ResourceSpecError, match="gpu tasks must declare resources.gpu_mem"):
        require_schedulable_resources(task_kind, resources)


def test_runtime_rejects_gpu_task_without_memory_anchor():
    with pytest.raises(ResourceSpecError, match="gpu tasks must declare resources.gpu_mem"):
        TaskRuntime(
            workflow_id="run-1",
            task_id="task-gpu",
            task_input={"input_params": {}},
            task_output={"output_params": {}},
            resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
            task_kind="gpu",
        )


def test_model_anchor_estimate_materializes_gpu_memory():
    task_kind, resources = normalize_task_semantics(
        resources={"cpu_num": 2},
        model_anchor={
            "local_model": "qwen",
            "estimated_weight_memory_bytes": 1024 * 1024 * 100,
        },
    )
    task = TaskRuntime(
        workflow_id="run-1",
        task_id="task-model",
        task_input={"input_params": {}},
        task_output={"output_params": {}},
        resources=resources,
        task_kind=task_kind,
        model_anchor={"local_model": "qwen"},
    )

    assert task_kind == "gpu"
    assert resources["gpu_mem"] == 120
    assert task.scheduler_resources["gpu"] == 1


def test_resource_history_does_not_emit_gpu_count(tmp_path):
    store = ResourceHistoryStore(tmp_path / "history.json")
    store.record(
        run_id="run-1",
        task_id="task-1",
        task_name="infer",
        status="failed",
        requested_resources={"cpu_num": 1, "gpu_mem": 1000, "io_num": 0},
        error={"error_type": "resource_insufficient"},
    )

    applied = store.apply(
        {"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
        task_name="infer",
    )
    assert applied["gpu_mem"] == 1250
    assert "gpu" not in applied


def test_queue_snapshot_contains_task_kind():
    scheduler = Scheduler.__new__(Scheduler)
    task = TaskRuntime(
        workflow_id="run-1",
        task_id="task-1",
        task_input={"input_params": {}},
        task_output={"output_params": {}},
        resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
        task_kind="cpu",
    )

    item = Scheduler._task_queue_snapshot_item(scheduler, task, task.created_time)
    assert item["task_kind"] == "cpu"
    assert item["resources"] == {"cpu_num": 1, "gpu_mem": 0, "io_num": 0}
    assert "gpu" not in item["resources"]
