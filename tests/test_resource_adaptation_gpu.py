"""Opt in with MAZE_GPU_CORE_URL=http://127.0.0.1:8000 on a local GPU Head."""

import os

import pytest
from maze import MaClient, task, workflow


@task(task_kind="gpu", resources={"gpu_mem": 128}, max_retries=1)
def allocate_after_oom(marker: str):
    import torch
    from pathlib import Path

    path = Path(marker)
    if not path.exists():
        path.write_text("first attempt")
        # Exercise the real CUDA allocator with a small per-process limit.
        torch.cuda.set_per_process_memory_fraction(0.001)
    tensor = torch.empty(64 * 1024 * 1024, dtype=torch.uint8, device="cuda")
    torch.cuda.synchronize()
    return {"allocated_bytes": tensor.numel(), "device": str(tensor.device)}


@workflow
def allocation_workflow(marker: str):
    result = allocate_after_oom(marker)
    return {"allocated_bytes": result.allocated_bytes, "device": result.device}


@task(task_kind="gpu", resources={"gpu_mem": 128}, max_retries=1)
def always_oom():
    import torch

    torch.cuda.set_per_process_memory_fraction(0.001)
    tensor = torch.empty(64 * 1024 * 1024, dtype=torch.uint8, device="cuda")
    return {"allocated_bytes": tensor.numel()}


@workflow
def failing_workflow():
    result = always_oom()
    return {"allocated_bytes": result.allocated_bytes}


@task(task_kind="gpu", resources={"gpu_mem": 128 * 1024})
def impossible_task():
    return {"should_not_run": True}


@workflow
def impossible_workflow():
    result = impossible_task()
    return {"should_not_run": result.should_not_run}


@task(task_kind="gpu", resources={"gpu_mem": 8192}, max_retries=1)
def routed_recovery_probe(marker: str, question: str):
    import json
    import os
    from pathlib import Path
    from urllib.request import Request, urlopen
    from maze.core.scheduler.error import ModelInferenceError

    route = json.loads(os.environ["MAZE_MODEL_ROUTE"])
    path = Path(marker)
    if not path.exists():
        path.write_text(route["instance_id"])
        raise ModelInferenceError(
            "CUDA out of memory. Tried to allocate 64.00 MiB",
            {
                "scope": "model_instance",
                "device_type": "cuda",
                "instance_id": route["instance_id"],
                "attempted_allocation_bytes": 64 * 1024 * 1024,
            },
        )
    request = Request(
        route["endpoint"] + "/chat/completions",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "model": route["served_model"],
                "messages": [{"role": "user", "content": question}],
                "max_tokens": 16,
                "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode(),
    )
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
    return {
        "answer": payload["choices"][0]["message"]["content"],
        "instance_id": route["instance_id"],
    }


@workflow
def routed_workflow(marker: str, question: str):
    result = routed_recovery_probe(marker, question)
    return {"answer": result.answer, "instance_id": result.instance_id}


pytestmark = pytest.mark.skipif(
    not os.environ.get("MAZE_GPU_CORE_URL"), reason="requires local GPU Head"
)


def test_real_cuda_oom_recovers_and_future_run_learns(tmp_path):
    client = MaClient(os.environ["MAZE_GPU_CORE_URL"])
    draft = client.create_workflow_from(
        allocation_workflow, inputs={"marker": str(tmp_path / "attempt")}
    )
    run = client.wait_run(draft.run(), timeout=120)
    assert run["status"] == "succeeded", run
    assert run["result_summary"]["allocated_bytes"] == 64 * 1024 * 1024
    node = next(iter(run["task_nodes"].values()))
    assert node["attempt"] == 2
    assert node["resources"]["gpu_mem"] > 128
    second = client.wait_run(draft.run(), timeout=120)
    assert second["status"] == "succeeded"
    assert next(iter(second["task_nodes"].values()))["attempt"] == 1
    assert next(iter(second["task_nodes"].values()))["resources"]["gpu_mem"] > 128


def test_repeated_real_oom_stops_at_retry_budget():
    client = MaClient(os.environ["MAZE_GPU_CORE_URL"])
    run_id = client.create_workflow_from(failing_workflow).run()
    result = client.wait_run(run_id, timeout=120)
    assert result["status"] == "failed", result
    assert next(iter(result["task_nodes"].values()))["attempt"] == 2


def test_impossible_gpu_demand_fails_without_execution():
    client = MaClient(os.environ["MAZE_GPU_CORE_URL"])
    result = client.wait_run(
        client.create_workflow_from(impossible_workflow).run(), timeout=30
    )
    assert result["status"] == "failed", result
    assert "resource_capacity_exceeded" in str(result["error_summary"])


def test_dynamic_cuda_recovery_reports_effective_request(tmp_path):
    client = MaClient(os.environ["MAZE_GPU_CORE_URL"])
    run = client.create_dynamic_run(timeout_seconds=120)
    invocation = run.append_task(
        allocate_after_oom, inputs={"marker": str(tmp_path / "dynamic-attempt")}
    )
    event = run.wait_for_task(invocation, timeout=120)
    assert event["data"]["result"]["allocated_bytes"] == 64 * 1024 * 1024
    snapshot = run.status()["task_nodes"][invocation.task_id]
    assert snapshot["resources"]["gpu_mem"] > 128
    assert snapshot["declared_resources"]["gpu_mem"] == 128
    assert snapshot["fault_tolerance"]["status"] == "recovered"
    run.finalize({"passed": True})


@pytest.mark.skipif(
    not os.environ.get("MAZE_GPU_MODEL_DIR"), reason="requires a downloaded model"
)
def test_routed_oom_redeploys_real_qwen_and_answers(tmp_path):
    from pathlib import Path
    import requests

    client = MaClient(os.environ["MAZE_GPU_CORE_URL"])
    marker = tmp_path / "instance"
    draft = client.create_workflow_from(
        routed_workflow,
        inputs={"marker": str(marker), "question": "1+1等于多少？只回答数字。"},
    )
    next(iter(draft._nodes.values()))["model_anchor"] = {
        "local_model": Path(os.environ["MAZE_GPU_MODEL_DIR"]).name,
        "backend": "transformers",
        "model_scope": "head",
    }
    result = client.wait_run(draft.run(), timeout=240)
    assert result["status"] == "succeeded", result
    instance = result["result_summary"]["instance_id"]
    try:
        assert result["result_summary"]["answer"].strip() == "2"
        assert marker.read_text() != instance
        node = next(iter(result["task_nodes"].values()))
        assert node["attempt"] == 2
        assert node["resources"]["gpu_mem"] > 8192
        repair = node["fault_tolerance"]["attempts"][0]["repair_action"]
        assert repair["scope"] == "model_instance"
    finally:
        session = requests.Session()
        session.trust_env = False
        response = session.post(
            os.environ["MAZE_GPU_CORE_URL"] + "/stop_llm_instance",
            json={"instance_id": instance},
            timeout=120,
        )
        response.raise_for_status()
