import pytest

from maze.core.scheduler.runtime import TaskRuntime
from maze.core.workflow.dag_spec import (
    DagSpecError,
    build_dag_workflow,
    dag_spec_from_payload,
)


def _node(node_id, code, outputs, **extra):
    node = {"id": node_id, "code": code, "outputs": outputs}
    node.update(extra)
    return node


PRIMARY_CODE = "def run(x):\n    return {'y': x}"
FALLBACK_CODE = "def run(x):\n    return {'y': x}"


def test_dag_spec_parses_fallback_and_policy_defaults():
    spec = dag_spec_from_payload(
        {
            "name": "fb",
            "nodes": [
                _node(
                    "a",
                    PRIMARY_CODE,
                    ["y"],
                    resources={"gpu": 2},
                    fallback={"code": FALLBACK_CODE, "resources": {"cpu": 4}},
                )
            ],
        }
    )
    node = spec["nodes"][0]
    assert node["fallback"]["code_str"] == FALLBACK_CODE
    assert node["fallback"]["resources"]["cpu"] == 4
    assert node["fallback_policy"]["trigger"] == "resource_unavailable"
    assert node["fallback_policy"]["pending_timeout_s"] == 10.0


def test_dag_spec_custom_pending_timeout():
    spec = dag_spec_from_payload(
        {
            "name": "fb",
            "nodes": [
                _node(
                    "a",
                    PRIMARY_CODE,
                    ["y"],
                    fallback={"code": FALLBACK_CODE},
                    fallback_policy={"pending_timeout_s": 3},
                )
            ],
        }
    )
    assert spec["nodes"][0]["fallback_policy"]["pending_timeout_s"] == 3.0


def test_fallback_requires_code():
    with pytest.raises(DagSpecError, match="fallback requires"):
        dag_spec_from_payload(
            {
                "name": "fb",
                "nodes": [_node("a", PRIMARY_CODE, ["y"], fallback={"resources": {"cpu": 1}})],
            }
        )


def test_fallback_outputs_must_match_primary():
    with pytest.raises(DagSpecError, match="fallback outputs must match"):
        dag_spec_from_payload(
            {
                "name": "fb",
                "nodes": [
                    _node(
                        "a",
                        PRIMARY_CODE,
                        ["y"],
                        fallback={"code": FALLBACK_CODE, "outputs": ["z"]},
                    )
                ],
            }
        )


def test_invalid_trigger_rejected():
    with pytest.raises(DagSpecError, match="fallback_policy.trigger"):
        dag_spec_from_payload(
            {
                "name": "fb",
                "nodes": [
                    _node(
                        "a",
                        PRIMARY_CODE,
                        ["y"],
                        fallback={"code": FALLBACK_CODE},
                        fallback_policy={"trigger": "cluster_busy"},
                    )
                ],
            }
        )


def test_build_workflow_carries_fallback_into_task():
    spec = dag_spec_from_payload(
        {
            "name": "fb",
            "nodes": [
                _node(
                    "a",
                    PRIMARY_CODE,
                    ["y"],
                    resources={"gpu": 2},
                    fallback={"code": FALLBACK_CODE, "resources": {"cpu": 4}},
                )
            ],
        }
    )
    workflow = build_dag_workflow("wf-1", spec)
    task = workflow.tasks["a"]
    assert task.fallback["code_str"] == FALLBACK_CODE
    assert task.fallback_policy["pending_timeout_s"] == 10.0
    payload = task.to_json()
    assert payload["fallback"]["resources"]["cpu"] == 4
    assert payload["fallback_policy"]["trigger"] == "resource_unavailable"


def _task_runtime_with_fallback(pending_timeout=10.0):
    return TaskRuntime(
        workflow_id="wf-1",
        task_id="a",
        task_input={"input_params": {}},
        task_output={"output_params": {}},
        resources={"cpu": 1, "cpu_mem": 0, "gpu": 2, "gpu_mem": 0},
        code_str=PRIMARY_CODE,
        fallback={"code_str": FALLBACK_CODE, "resources": {"cpu": 4, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}},
        fallback_policy={"trigger": "resource_unavailable", "pending_timeout_s": pending_timeout},
    )


def test_task_runtime_switch_to_fallback_swaps_code_and_resources():
    task = _task_runtime_with_fallback()
    assert task.can_degrade() is True
    assert task.variant == "primary"

    task.switch_to_fallback()

    assert task.variant == "fallback"
    assert task.degraded is True
    assert task.resources["gpu"] == 0
    assert task.resources["cpu"] == 4
    assert task.can_degrade() is False  # cannot degrade twice


def test_task_runtime_without_fallback_cannot_degrade():
    task = TaskRuntime(
        workflow_id="wf-1",
        task_id="a",
        task_input={"input_params": {}},
        task_output={"output_params": {}},
        resources={"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0},
        code_str=PRIMARY_CODE,
    )
    assert task.can_degrade() is False
    task.switch_to_fallback()
    assert task.variant == "primary"


def test_fallback_pending_timeout_default():
    task = TaskRuntime(
        workflow_id="wf-1",
        task_id="a",
        task_input={"input_params": {}},
        task_output={"output_params": {}},
        resources={"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0},
        code_str=PRIMARY_CODE,
        fallback={"code_str": FALLBACK_CODE},
    )
    assert task.fallback_pending_timeout() == 10.0
