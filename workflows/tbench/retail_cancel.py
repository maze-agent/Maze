"""Ascend-Maze-native tau-bench retail cancellation workflow."""

from __future__ import annotations

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.tbench._common import (
    execute_retail_cancellations,
    format_retail_cancel_result,
    inference_features,
    load_retail_backend_data,
    metadata_dict,
    parse_cancellation_requests,
    retail_cancel_prompt,
)

SPEC = WorkflowSpec(
    name="maze-tbench-retail-cancel",
    source="tbench",
    kind="retail_cancel",
    nodes=nodes(
        (
            ("task0_init", "io", None),
            ("task1_llm_process", "npu", "qwen3-32b"),
            ("task2_execute_cancel", "cpu", None),
            ("task3_output_result", "io", None),
        )
    ),
    edges=edges(
        (
            ("task0_init", "task1_llm_process"),
            ("task1_llm_process", "task2_execute_cancel"),
            ("task2_execute_cancel", "task3_output_result"),
        )
    ),
)

INPUTS = spec_inputs()


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task0_init(
    dag_id: str,
    question: str,
    answer: str = "",
    supplementary_files: object = None,
    metadata: object = None,
) -> dict[str, object]:
    if not question:
        raise ValueError(f"task {dag_id} question field is empty")
    backend_data = load_retail_backend_data(supplementary_files)
    prompt = retail_cancel_prompt(question)
    normalized_metadata = metadata_dict(metadata)
    features = inference_features(prompt)
    return {
        "dag_id": dag_id,
        "instruction": question,
        "answer": answer,
        "backend_data": backend_data,
        "metadata": normalized_metadata,
        "prompt": prompt,
        "succ_task_feat": {"task1_llm_process": features},
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task1_llm_process(
    dag_id: str,
    instruction: str,
    prompt: str,
    metadata: dict[str, object],
    backend_data: dict[str, object],
) -> dict[str, object]:
    from ascend_maze.inference import chat

    response = chat(
        [{"role": "user", "content": prompt}],
        max_tokens=4096,
        temperature=0.0,
    )
    override = metadata.get("llm_output_override")
    if isinstance(override, str) and override.strip():
        llm_output = override
    else:
        llm_output = response.text
    cancellation_requests = parse_cancellation_requests(llm_output)
    features = {
        "text_length": len(prompt),
        "token_count": len(prompt.split()),
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
    }
    return {
        "dag_id": dag_id,
        "instruction": instruction,
        "llm_output": llm_output,
        "raw_model_output": response.text,
        "cancellation_requests": cancellation_requests,
        "curr_task_feat": features,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task2_execute_cancel(
    dag_id: str,
    backend_data: dict[str, object],
    cancellation_requests: list[dict[str, object]],
) -> dict[str, object]:
    cancel_results = execute_retail_cancellations(
        backend_data,
        cancellation_requests,
    )
    affected_orders = []
    affected_user_payment_methods: dict[str, object] = {}
    users = backend_data.get("users", {})
    if not isinstance(users, dict):
        users = {}
    for cancel_result in cancel_results:
        order_result = cancel_result.get("result")
        if not isinstance(order_result, dict):
            continue
        affected_orders.append(order_result)
        user_id = order_result.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            continue
        user = users.get(user_id)
        if not isinstance(user, dict):
            continue
        payment_methods = user.get("payment_methods", {})
        if isinstance(payment_methods, dict):
            affected_user_payment_methods[user_id] = {
                str(payment_id): payment_method
                for payment_id, payment_method in payment_methods.items()
            }
    return {
        "dag_id": dag_id,
        "status": "done",
        "cancel_results": cancel_results,
        "affected_orders": affected_orders,
        "affected_user_payment_methods": affected_user_payment_methods,
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task3_output_result(
    dag_id: str,
    cancel_results: list[dict[str, object]],
) -> dict[str, object]:
    final_output = format_retail_cancel_result(cancel_results)
    return {
        "dag_id": dag_id,
        "status": "done",
        "result": final_output,
        "cancel_results": cancel_results,
    }


def build() -> Workflow:
    workflow = Workflow(SPEC.name)
    dag_id = workflow.input("dag_id")
    question = workflow.input("question")
    answer = workflow.input("answer")
    supplementary_files = workflow.input("supplementary_files")
    metadata = workflow.input("metadata")

    initialized = workflow.add_task(
        task0_init,
        task_name="task0_init",
        inputs={
            "dag_id": dag_id,
            "question": question,
            "answer": answer,
            "supplementary_files": supplementary_files,
            "metadata": metadata,
        },
    )
    extracted = workflow.add_task(
        task1_llm_process,
        task_name="task1_llm_process",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": initialized.outputs["dag_id"],
            "instruction": initialized.outputs["instruction"],
            "prompt": initialized.outputs["prompt"],
            "metadata": initialized.outputs["metadata"],
            "backend_data": initialized.outputs["backend_data"],
        },
    )
    executed = workflow.add_task(
        task2_execute_cancel,
        task_name="task2_execute_cancel",
        inputs={
            "dag_id": extracted.outputs["dag_id"],
            "backend_data": initialized.outputs["backend_data"],
            "cancellation_requests": extracted.outputs["cancellation_requests"],
        },
    )
    workflow.add_task(
        task3_output_result,
        task_name="task3_output_result",
        inputs={
            "dag_id": executed.outputs["dag_id"],
            "cancel_results": executed.outputs["cancel_results"],
        },
    )
    return workflow
