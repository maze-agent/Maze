import asyncio
import importlib

import pytest
import ray

from maze.core.application.spec import AppSpecError, app_spec_from_payload, build_app_workflow
from maze.core.scheduler.resource import ResourceManager
from maze.core.scheduler.scheduler import _ray_execution_error_type
from maze.core.workflow.dag_spec import DagSpecError, build_dag_workflow, dag_spec_from_payload


def _dag_node(resources):
    return {
        "id": "probe",
        "code_str": "def probe():\n    return {'result': 'ok'}",
        "inputs": {},
        "outputs": [{"name": "result", "data_type": "str"}],
        "resources": resources,
    }


def test_app_resource_hints_survive_normalization_and_workflow_build():
    spec = app_spec_from_payload({
        "name": "probe",
        "command": ["python", "-c", "print('ok')"],
        "resources": {
            "cpu": 2,
            "node_id": "worker-1",
            "required_capability": "workspace_sandbox",
            "avoid_node_ids": ["worker-2", "worker-2"],
        },
    })

    assert spec["resources"] == {
        "cpu": 2,
        "cpu_mem": 128,
        "gpu": 0,
        "gpu_mem": 0,
        "target_node_id": "worker-1",
        "required_capability": "workspace_sandbox",
        "avoid_node_ids": ["worker-2"],
    }
    workflow = build_app_workflow("workflow", spec)
    assert workflow.tasks["app"].resources == spec["resources"]


def test_dag_resource_hints_survive_normalization_and_workflow_build():
    payload = {
        "name": "probe-dag",
        "nodes": [_dag_node({
            "target_node_id": "worker-1",
            "required_capability": "workspace_sandbox",
            "avoid_node_ids": ["worker-2"],
        })],
        "edges": [],
    }

    spec = dag_spec_from_payload(payload)
    resources = spec["nodes"][0]["resources"]
    assert resources["target_node_id"] == "worker-1"
    assert resources["required_capability"] == "workspace_sandbox"
    assert resources["avoid_node_ids"] == ["worker-2"]
    workflow = build_dag_workflow("workflow", spec)
    assert workflow.tasks["probe"].resources == resources


@pytest.mark.parametrize(
    ("builder", "payload", "error_type"),
    [
        (
            app_spec_from_payload,
            {
                "name": "probe",
                "command": "true",
                "resources": {"target_node_id": "worker-1", "avoid_node_ids": ["worker-1"]},
            },
            AppSpecError,
        ),
        (
            dag_spec_from_payload,
            {
                "nodes": [_dag_node({"target_node_id": "worker-1", "avoid_node_ids": "worker-2"})],
                "edges": [],
            },
            DagSpecError,
        ),
    ],
)
def test_invalid_resource_hint_combinations_are_rejected(builder, payload, error_type):
    with pytest.raises(error_type):
        builder(payload)


def test_worker_crash_is_classified_as_retryable_node_loss():
    assert _ray_execution_error_type(ray.exceptions.WorkerCrashedError("worker crashed")) == "node_lost"
    assert _ray_execution_error_type(ray.exceptions.TaskUnschedulableError("no resources")) == "resource_unavailable"


@pytest.mark.parametrize(
    ("builder", "payload", "error_type"),
    [
        (
            app_spec_from_payload,
            {"name": "probe", "command": "true", "resources": {"gpu": 2}},
            AppSpecError,
        ),
        (
            dag_spec_from_payload,
            {"nodes": [_dag_node({"gpu": 2})], "edges": []},
            DagSpecError,
        ),
    ],
)
def test_specs_reject_gpu_requests_above_scheduler_limit(builder, payload, error_type):
    with pytest.raises(error_type, match="at most 1"):
        builder(payload)


def test_resource_manager_rejects_gpu_overflow_without_querying_or_leasing_ray():
    manager = ResourceManager()
    manager._ray_node_index = lambda: pytest.fail("invalid GPU request queried Ray")

    selection = manager.select_node({
        "cpu": 1,
        "cpu_mem": 128,
        "gpu": 2,
        "gpu_mem": 0,
    })

    assert not selection
    assert selection.decision["reason"] == "unsupported_gpu_count"
    assert manager.active_leases == {}


def test_gpu_limit_validation_endpoints_return_400(monkeypatch, tmp_path):
    monkeypatch.setenv("MAZE_WORKSPACE_DIR", str(tmp_path))
    server = importlib.import_module("maze.core.server")

    class Request:
        def __init__(self, payload):
            self.payload = payload

        async def json(self):
            return self.payload

    cases = [
        (
            server.validate_app_spec,
            {"name": "probe", "command": "true", "resources": {"gpu": 2}},
        ),
        (
            server.validate_dag_workflow,
            {"nodes": [_dag_node({"gpu": 2})], "edges": []},
        ),
    ]
    for handler, payload in cases:
        with pytest.raises(server.HTTPException) as exc_info:
            asyncio.run(handler(Request(payload)))
        assert exc_info.value.status_code == 400
        assert "at most 1" in exc_info.value.detail
