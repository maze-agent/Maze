"""Ascend-Maze-native tau-bench retail return workflow."""

from __future__ import annotations

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.tbench._common import (
    execute_retail_return,
    find_retail_user,
    format_retail_return_result,
    get_retail_order_details,
    inference_features,
    load_retail_backend_data,
    metadata_dict,
    parse_return_request,
    retail_return_prompt,
)

SPEC = WorkflowSpec(
    name="maze-tbench-retail-return",
    source="tbench",
    kind="retail_return",
    nodes=nodes(
        (
            ("task0_init", "io", None),
            ("task1_llm_process", "npu", "qwen3-32b"),
            ("task2_find_user", "cpu", None),
            ("task3_get_order_details", "cpu", None),
            ("task4_execute_return", "cpu", None),
            ("task5_output_result", "io", None),
        )
    ),
    edges=edges(
        (
            ("task0_init", "task1_llm_process"),
            ("task1_llm_process", "task2_find_user"),
            ("task2_find_user", "task3_get_order_details"),
            ("task3_get_order_details", "task4_execute_return"),
            ("task4_execute_return", "task5_output_result"),
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
    prompt = retail_return_prompt(question)
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
    return_request = parse_return_request(llm_output)
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
        "return_request": return_request,
        "backend_data": backend_data,
        "curr_task_feat": features,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task2_find_user(
    dag_id: str,
    backend_data: dict[str, object],
    return_request: dict[str, object],
) -> dict[str, object]:
    user_lookup = find_retail_user(backend_data, return_request)
    return {
        "dag_id": dag_id,
        "backend_data": backend_data,
        "return_request": return_request,
        "user_lookup": user_lookup,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task3_get_order_details(
    dag_id: str,
    backend_data: dict[str, object],
    return_request: dict[str, object],
    user_lookup: dict[str, object],
) -> dict[str, object]:
    order_id = return_request.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        order_lookup = {
            "status": "error",
            "details": "Error: order_id missing",
            "order_details": {},
        }
    else:
        order_lookup = get_retail_order_details(backend_data, order_id)
    return {
        "dag_id": dag_id,
        "backend_data": backend_data,
        "return_request": return_request,
        "user_lookup": user_lookup,
        "order_lookup": order_lookup,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task4_execute_return(
    dag_id: str,
    backend_data: dict[str, object],
    return_request: dict[str, object],
    user_lookup: dict[str, object],
    order_lookup: dict[str, object],
) -> dict[str, object]:
    if user_lookup.get("status") != "success":
        final_result = {
            "action": "return",
            "status": "error",
            "details": user_lookup.get("details", "Error: user lookup failed"),
        }
    elif order_lookup.get("status") != "success":
        final_result = {
            "action": "return",
            "status": "error",
            "details": order_lookup.get("details", "Error: order lookup failed"),
        }
    else:
        user_details = user_lookup.get("user_details", {})
        order_details = order_lookup.get("order_details", {})
        if not isinstance(user_details, dict) or not isinstance(order_details, dict):
            final_result = {
                "action": "return",
                "status": "error",
                "details": "Error: malformed user or order details",
            }
        else:
            final_result = execute_retail_return(
                backend_data,
                return_request,
                user_details,
                order_details,
            )
    return {
        "dag_id": dag_id,
        "status": final_result.get("status", "error"),
        "backend_data": backend_data,
        "final_result": final_result,
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task5_output_result(
    dag_id: str,
    final_result: dict[str, object],
) -> dict[str, object]:
    final_output = format_retail_return_result(final_result)
    return {
        "dag_id": dag_id,
        "status": final_result.get("status", "error"),
        "result": final_output,
        "final_result": final_result,
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
    user = workflow.add_task(
        task2_find_user,
        task_name="task2_find_user",
        inputs={
            "dag_id": extracted.outputs["dag_id"],
            "backend_data": extracted.outputs["backend_data"],
            "return_request": extracted.outputs["return_request"],
        },
    )
    order = workflow.add_task(
        task3_get_order_details,
        task_name="task3_get_order_details",
        inputs={
            "dag_id": user.outputs["dag_id"],
            "backend_data": user.outputs["backend_data"],
            "return_request": user.outputs["return_request"],
            "user_lookup": user.outputs["user_lookup"],
        },
    )
    executed = workflow.add_task(
        task4_execute_return,
        task_name="task4_execute_return",
        inputs={
            "dag_id": order.outputs["dag_id"],
            "backend_data": order.outputs["backend_data"],
            "return_request": order.outputs["return_request"],
            "user_lookup": order.outputs["user_lookup"],
            "order_lookup": order.outputs["order_lookup"],
        },
    )
    workflow.add_task(
        task5_output_result,
        task_name="task5_output_result",
        inputs={
            "dag_id": executed.outputs["dag_id"],
            "final_result": executed.outputs["final_result"],
        },
    )
    return workflow
