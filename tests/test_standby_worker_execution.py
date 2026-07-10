from __future__ import annotations

import base64

import cloudpickle

from maze.core.files.lineage import TASK_RESULT_ENVELOPE
from maze.core.scheduler import runtime as runtime_module
from maze.core.scheduler.runner import (
    execute_code_task_in_worker,
    execute_langgraph_task_in_worker,
)
from maze.core.scheduler.runtime import (
    LanggraphTaskRuntime,
    SelectedNode,
    TaskRuntime,
    WorkflowRuntimeManager,
)
from maze.core.scheduler.standby_worker import StandbyWorkerPoolManager


VALID_NODE_ID = "a" * 56


class FakeRemoteMethod:
    def __init__(self, result_ref):
        self.result_ref = result_ref
        self.calls = []

    def remote(self, **kwargs):
        self.calls.append(kwargs)
        return self.result_ref


class FakeActor:
    def __init__(self, result_ref):
        self.execute_code_task = FakeRemoteMethod(result_ref)
        self.execute_langgraph_task = FakeRemoteMethod(result_ref)


class FakeRemoteRunner:
    def __init__(self, result_ref):
        self.result_ref = result_ref
        self.options_kwargs = None
        self.remote_kwargs = None

    def options(self, **kwargs):
        self.options_kwargs = kwargs
        return self

    def remote(self, **kwargs):
        self.remote_kwargs = kwargs
        return self.result_ref


def make_task_runtime(task_kind="cpu"):
    return TaskRuntime(
        workflow_id="workflow-1",
        task_id="task-1",
        task_input={
            "input_params": {
                "x": {
                    "input_schema": "from_user",
                    "key": "x",
                    "value": 2,
                },
            },
        },
        task_output={},
        resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
        task_kind=task_kind,
        code_str="def add(x):\n    return {'y': x + 1}\n",
    )


def make_standby_pool(actor):
    manager = StandbyWorkerPoolManager(
        pool_sizes={"cpu": 1},
        execution_enabled=True,
        actor_factory=lambda node_id, worker_type: actor,
    )
    manager.ensure_for_nodes({VALID_NODE_ID: object()})
    return manager


def test_workflow_runtime_uses_standby_worker_when_available():
    actor = FakeActor(result_ref="standby-ref")
    pool = make_standby_pool(actor)
    manager = WorkflowRuntimeManager(standby_worker_pool=pool)
    task = make_task_runtime()
    manager.add_task(task)

    manager.run_task(task, SelectedNode(VALID_NODE_ID, "127.0.0.1"))

    assert task.execution_backend == "standby_worker"
    assert task.object_ref == "standby-ref"
    assert manager.ref_to_workflow_id["standby-ref"] == "workflow-1"
    assert actor.execute_code_task.calls[0]["task_input_data"] == {"x": 2}
    assert pool.snapshot()["execution"]["nodes"][VALID_NODE_ID]["cpu"]["busy"] == 1

    manager.clear_task_ref(task)
    assert pool.snapshot()["execution"]["nodes"][VALID_NODE_ID]["cpu"]["busy"] == 0


def test_workflow_runtime_falls_back_when_no_standby_worker(monkeypatch):
    fallback_runner = FakeRemoteRunner(result_ref="fallback-ref")
    monkeypatch.setattr(runtime_module, "remote_task_runner", fallback_runner)

    pool = StandbyWorkerPoolManager(pool_sizes={"cpu": 0}, execution_enabled=True)
    manager = WorkflowRuntimeManager(standby_worker_pool=pool)
    task = make_task_runtime()
    manager.add_task(task)

    manager.run_task(task, SelectedNode(VALID_NODE_ID, "127.0.0.1"))

    assert task.execution_backend == "ray_task"
    assert task.object_ref == "fallback-ref"
    assert fallback_runner.remote_kwargs["task_input_data"] == {"x": 2}
    assert fallback_runner.remote_kwargs["cuda_visible_devices"] is None


def test_workflow_runtime_uses_standby_worker_for_langgraph_tasks():
    actor = FakeActor(result_ref="langgraph-ref")
    pool = make_standby_pool(actor)
    manager = WorkflowRuntimeManager(standby_worker_pool=pool)
    task = LanggraphTaskRuntime(
        workflow_id="workflow-1",
        task_id="task-1",
        code_ser="encoded-code",
        args="encoded-args",
        kwargs="encoded-kwargs",
        resources={"cpu_num": 1, "gpu_mem": 0, "io_num": 0},
        task_kind="cpu",
    )
    manager.add_task(task)

    manager.run_task(task, SelectedNode(VALID_NODE_ID, "127.0.0.1"))

    assert task.execution_backend == "standby_worker"
    assert task.object_ref == "langgraph-ref"
    assert actor.execute_langgraph_task.calls[0]["code_ser"] == "encoded-code"
    manager.clear_task_ref(task)
    assert pool.snapshot()["execution"]["nodes"][VALID_NODE_ID]["cpu"]["busy"] == 0


def test_cancel_workflow_releases_standby_worker(monkeypatch):
    cancelled_refs = []
    monkeypatch.setattr(runtime_module.ray, "cancel", lambda ref, force=True: cancelled_refs.append((ref, force)))

    actor = FakeActor(result_ref="standby-ref")
    pool = make_standby_pool(actor)
    manager = WorkflowRuntimeManager(standby_worker_pool=pool)
    task = make_task_runtime()
    manager.add_task(task)
    manager.run_task(task, SelectedNode(VALID_NODE_ID, "127.0.0.1"))

    assert pool.snapshot()["execution"]["nodes"][VALID_NODE_ID]["cpu"]["busy"] == 1

    running_tasks = manager.cancel_workflow("workflow-1")

    assert running_tasks == [task]
    assert cancelled_refs == [("standby-ref", True)]
    assert pool.snapshot()["execution"]["nodes"][VALID_NODE_ID]["cpu"]["busy"] == 0


def test_standby_code_execution_returns_remote_runner_envelope():
    result = execute_code_task_in_worker(
        code_str="def add(x):\n    return {'y': x + 1}\n",
        task_input_data={"x": 2},
    )

    assert result[TASK_RESULT_ENVELOPE] is True
    assert result["result"] == {"y": 3}
    assert isinstance(result["duration_ms"], int)


def test_standby_langgraph_execution_matches_existing_raw_result_path():
    def add(x):
        return {"y": x + 1}

    result = execute_langgraph_task_in_worker(
        code_ser=base64.b64encode(cloudpickle.dumps(add)).decode("utf-8"),
        args=base64.b64encode(cloudpickle.dumps((2,))).decode("utf-8"),
        kwargs=base64.b64encode(cloudpickle.dumps({})).decode("utf-8"),
    )

    assert result == {"y": 3}
