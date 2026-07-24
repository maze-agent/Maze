"""Ascend-Maze-native tau-bench airline cancellation/rebooking workflow."""

from __future__ import annotations

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.tbench._common import (
    airline_cancel_decision_prompt,
    airline_cancel_extract_prompt,
    book_airline_replacement_reservation,
    cancel_airline_reservation,
    get_airline_reservation_details,
    get_airline_user_details,
    inference_features,
    load_airline_backend_data,
    metadata_dict,
    parse_airline_cancel_request,
    parse_airline_selected_flights,
    search_airline_replacement_flights,
)

SPEC = WorkflowSpec(
    name="maze-tbench-airline-cancel",
    source="tbench",
    kind="airline_cancel",
    nodes=nodes(
        (
            ("task0_init", "io", None),
            ("task1_llm_process1", "npu", "qwen3-32b"),
            ("task2_get_user_and_reservation_details", "cpu", None),
            ("task3_cancel_reservation", "io", None),
            ("task4_search_new_flights", "cpu", None),
            ("task5_llm_process2", "npu", "qwen3-32b"),
            ("task6_book_new_reservation", "cpu", None),
        )
    ),
    edges=edges(
        (
            ("task0_init", "task1_llm_process1"),
            ("task1_llm_process1", "task2_get_user_and_reservation_details"),
            ("task2_get_user_and_reservation_details", "task3_cancel_reservation"),
            ("task3_cancel_reservation", "task4_search_new_flights"),
            ("task4_search_new_flights", "task5_llm_process2"),
            ("task5_llm_process2", "task6_book_new_reservation"),
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
    backend_data = load_airline_backend_data(supplementary_files)
    prompt = airline_cancel_extract_prompt(question)
    normalized_metadata = metadata_dict(metadata)
    features = inference_features(prompt)
    return {
        "dag_id": dag_id,
        "instruction": question,
        "answer": answer,
        "backend_data": backend_data,
        "metadata": normalized_metadata,
        "prompt": prompt,
        "succ_task_feat": {"task1_llm_process1": features},
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task1_llm_process1(
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
    override = metadata.get("cancel_extract_output_override")
    if not isinstance(override, str) or not override.strip():
        override = metadata.get("llm_output_override")
    if isinstance(override, str) and override.strip():
        llm_output = override
    else:
        llm_output = response.text
    cancel_request = parse_airline_cancel_request(llm_output)
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
        "cancel_request": cancel_request,
        "metadata": metadata,
        "curr_task_feat": features,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task2_get_user_and_reservation_details(
    dag_id: str,
    instruction: str,
    backend_data: dict[str, object],
    metadata: dict[str, object],
    cancel_request: dict[str, object],
) -> dict[str, object]:
    user_lookup = get_airline_user_details(
        backend_data,
        str(cancel_request.get("user_id", "")),
    )
    reservation_lookup = get_airline_reservation_details(
        backend_data,
        str(cancel_request.get("cancel_reservation_id", "")),
    )
    return {
        "dag_id": dag_id,
        "instruction": instruction,
        "metadata": metadata,
        "cancel_request": cancel_request,
        "user_lookup": user_lookup,
        "reservation_lookup": reservation_lookup,
    }


@task(task_kind="io", resources={"cpu_num": 1, "mem": 1024, "io_num": 1})
def task3_cancel_reservation(
    dag_id: str,
    instruction: str,
    backend_data: dict[str, object],
    metadata: dict[str, object],
    cancel_request: dict[str, object],
    user_lookup: dict[str, object],
    reservation_lookup: dict[str, object],
) -> dict[str, object]:
    if reservation_lookup.get("status") != "success":
        cancel_result = {
            "status": "error",
            "details": reservation_lookup.get(
                "details",
                "Error: reservation lookup failed",
            ),
        }
    else:
        cancel_result = cancel_airline_reservation(
            backend_data,
            str(cancel_request.get("cancel_reservation_id", "")),
        )
    return {
        "dag_id": dag_id,
        "instruction": instruction,
        "metadata": metadata,
        "cancel_request": cancel_request,
        "user_lookup": user_lookup,
        "cancel_result": cancel_result,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task4_search_new_flights(
    dag_id: str,
    instruction: str,
    backend_data: dict[str, object],
    metadata: dict[str, object],
    cancel_request: dict[str, object],
    user_lookup: dict[str, object],
    cancel_result: dict[str, object],
) -> dict[str, object]:
    flight_search = search_airline_replacement_flights(
        backend_data,
        cancel_request,
    )
    outbound = flight_search["outbound_flights"]
    inbound = flight_search["return_flights"]
    assert isinstance(outbound, list)
    assert isinstance(inbound, list)
    prompt = airline_cancel_decision_prompt(cancel_request, outbound, inbound)
    features = inference_features(prompt)
    return {
        "dag_id": dag_id,
        "instruction": instruction,
        "metadata": metadata,
        "cancel_request": cancel_request,
        "user_lookup": user_lookup,
        "cancel_result": cancel_result,
        "outbound_flights": outbound,
        "return_flights": inbound,
        "prompt": prompt,
        "succ_task_feat": {"task5_llm_process2": features},
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task5_llm_process2(
    dag_id: str,
    metadata: dict[str, object],
    cancel_request: dict[str, object],
    user_lookup: dict[str, object],
    cancel_result: dict[str, object],
    outbound_flights: list[dict[str, object]],
    return_flights: list[dict[str, object]],
    prompt: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    response = chat(
        [{"role": "user", "content": prompt}],
        max_tokens=4096,
        temperature=0.0,
    )
    override = metadata.get("flight_selection_output_override")
    if isinstance(override, str) and override.strip():
        llm_output = override
    else:
        llm_output = response.text
    selected_flights = parse_airline_selected_flights(llm_output)
    user_details = user_lookup.get("user_details", {})
    if not isinstance(user_details, dict):
        user_details = {}
    features = {
        "text_length": len(prompt),
        "token_count": len(prompt.split()),
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
    }
    return {
        "dag_id": dag_id,
        "llm_output": llm_output,
        "raw_model_output": response.text,
        "cancel_request": cancel_request,
        "cancel_result": cancel_result,
        "selected_flights": selected_flights,
        "outbound_flights": outbound_flights,
        "return_flights": return_flights,
        "user_details": user_details,
        "curr_task_feat": features,
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task6_book_new_reservation(
    dag_id: str,
    backend_data: dict[str, object],
    cancel_request: dict[str, object],
    cancel_result: dict[str, object],
    selected_flights: dict[str, object],
    outbound_flights: list[dict[str, object]],
    return_flights: list[dict[str, object]],
    user_details: dict[str, object],
) -> dict[str, object]:
    booking_result = book_airline_replacement_reservation(
        backend_data,
        cancel_request,
        selected_flights,
        outbound_flights,
        return_flights,
        user_details,
    )
    affected_user_reservations = []
    user_id = str(cancel_request.get("user_id", ""))
    users = backend_data.get("users", {})
    if isinstance(users, dict):
        user = users.get(user_id)
        if isinstance(user, dict):
            reservations = user.get("reservations", [])
            if isinstance(reservations, list):
                affected_user_reservations = reservations
    return {
        "dag_id": dag_id,
        "status": booking_result.get("status", "error"),
        "cancel_result": cancel_result,
        "booking_result": booking_result,
        "result": {
            "cancel_result": cancel_result,
            "booking_result": booking_result,
        },
        "affected_user_reservations": affected_user_reservations,
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
        task1_llm_process1,
        task_name="task1_llm_process1",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": initialized.outputs["dag_id"],
            "instruction": initialized.outputs["instruction"],
            "prompt": initialized.outputs["prompt"],
            "metadata": initialized.outputs["metadata"],
            "backend_data": initialized.outputs["backend_data"],
        },
    )
    details = workflow.add_task(
        task2_get_user_and_reservation_details,
        task_name="task2_get_user_and_reservation_details",
        inputs={
            "dag_id": extracted.outputs["dag_id"],
            "instruction": extracted.outputs["instruction"],
            "backend_data": initialized.outputs["backend_data"],
            "metadata": extracted.outputs["metadata"],
            "cancel_request": extracted.outputs["cancel_request"],
        },
    )
    cancelled = workflow.add_task(
        task3_cancel_reservation,
        task_name="task3_cancel_reservation",
        inputs={
            "dag_id": details.outputs["dag_id"],
            "instruction": details.outputs["instruction"],
            "backend_data": initialized.outputs["backend_data"],
            "metadata": details.outputs["metadata"],
            "cancel_request": details.outputs["cancel_request"],
            "user_lookup": details.outputs["user_lookup"],
            "reservation_lookup": details.outputs["reservation_lookup"],
        },
    )
    searched = workflow.add_task(
        task4_search_new_flights,
        task_name="task4_search_new_flights",
        inputs={
            "dag_id": cancelled.outputs["dag_id"],
            "instruction": cancelled.outputs["instruction"],
            "backend_data": initialized.outputs["backend_data"],
            "metadata": cancelled.outputs["metadata"],
            "cancel_request": cancelled.outputs["cancel_request"],
            "user_lookup": cancelled.outputs["user_lookup"],
            "cancel_result": cancelled.outputs["cancel_result"],
        },
    )
    selected = workflow.add_task(
        task5_llm_process2,
        task_name="task5_llm_process2",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": searched.outputs["dag_id"],
            "metadata": searched.outputs["metadata"],
            "cancel_request": searched.outputs["cancel_request"],
            "user_lookup": searched.outputs["user_lookup"],
            "cancel_result": searched.outputs["cancel_result"],
            "outbound_flights": searched.outputs["outbound_flights"],
            "return_flights": searched.outputs["return_flights"],
            "prompt": searched.outputs["prompt"],
        },
    )
    workflow.add_task(
        task6_book_new_reservation,
        task_name="task6_book_new_reservation",
        inputs={
            "dag_id": selected.outputs["dag_id"],
            "backend_data": initialized.outputs["backend_data"],
            "cancel_request": selected.outputs["cancel_request"],
            "cancel_result": selected.outputs["cancel_result"],
            "selected_flights": selected.outputs["selected_flights"],
            "outbound_flights": selected.outputs["outbound_flights"],
            "return_flights": selected.outputs["return_flights"],
            "user_details": selected.outputs["user_details"],
        },
    )
    return workflow
