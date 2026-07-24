"""Ascend-Maze port of the Maze tau-bench airline booking workflow."""

from __future__ import annotations

import json
import time

from ascend_maze import Workflow, task

from workflows._common import WorkflowSpec, edges, nodes, spec_inputs
from workflows.tbench._common import (
    estimate_tokens,
    extract_json_from_llm_output,
    inference_features,
    load_airline_backend_data,
)
from workflows.tbench.airline_tools import (
    BookReservation,
    GetUserDetails,
    SearchDirectFlight,
    SearchOnestopFlight,
)

SPEC = WorkflowSpec(
    name="maze-tbench-airline-book",
    source="tbench",
    kind="airline_book",
    nodes=nodes(
        (
            ("task0_init", "cpu", None),
            ("task1_llm_process", "npu", "qwen3-32b"),
            ("task2a_search_direct_flight", "cpu", None),
            ("task2b_search_onestop_flight", "cpu", None),
            ("task2c_get_user_details", "cpu", None),
            ("task3_llm_fuse_process_filter_and_decide", "npu", "qwen3-32b"),
            ("task4_book_reservation", "cpu", None),
        )
    ),
    edges=edges(
        (
            ("task0_init", "task1_llm_process"),
            ("task1_llm_process", "task2a_search_direct_flight"),
            ("task1_llm_process", "task2b_search_onestop_flight"),
            ("task1_llm_process", "task2c_get_user_details"),
            (
                "task2a_search_direct_flight",
                "task3_llm_fuse_process_filter_and_decide",
            ),
            (
                "task2b_search_onestop_flight",
                "task3_llm_fuse_process_filter_and_decide",
            ),
            (
                "task2c_get_user_details",
                "task3_llm_fuse_process_filter_and_decide",
            ),
            (
                "task3_llm_fuse_process_filter_and_decide",
                "task4_book_reservation",
            ),
        )
    ),
)

INPUTS = spec_inputs()


def _extract_prompt(instruction: str) -> str:
    return f"""
        You are a professional flight booking assistant. Please carefully read the user's flight booking instructions below and extract the key information.
        You need to return the extracted information in a strict JSON format, without any additional explanations or text.

        The fields to be extracted are as follows:
        - "user_id": User ID (e.g., "mia_jackson_2156").
        - "origin": Origin airport code (3 letters, e.g., "JFK", "SFO").
        - "destination": Destination airport code (3 letters, e.g., "SEA", "LAX").
        - "date": Departure date (format: "YYYY-MM-DD", default year is 2024).
        - "cabin": Cabin class (must be one of "basic_economy", "economy", "business").
        - "baggages": Number of baggage items (integer).
        - "insurance": Whether insurance is needed ("yes" or "no").
        - "constraints": A list of strings containing all other constraints and preferences.

        User instructions:
        "{instruction}"

        JSON output:
        """


def _decision_prompt(
    instruction: str,
    all_candidates: list[list[dict[str, object]]] | None = None,
) -> str:
    candidates = ""
    if all_candidates is not None:
        candidates = f"        {json.dumps(all_candidates, indent=2)}\n\n"
    return f"""
        You are a professional and meticulous flight booking decision assistant.
        Your task is to select the single most suitable itinerary from the list of candidate itineraries provided below, based on the user's original request and all constraints.

        # User's original request
        "{instruction}"

        # List of candidate itineraries (JSON format)
        Each itinerary is a list containing one or more flights.
{candidates}        # Your task
        1. Carefully read and understand each of the user's requirements, including but not limited to: time preferences, price preferences (e.g., "cheapest"), airline preferences, number of layovers, etc.
        2. Strictly filter the candidate itineraries according to these requirements.
        3. From the itineraries that meet all conditions, select the single best option.
        4. Return your chosen itinerary in strict JSON format, without any additional explanations, comments, or text. The returned JSON object should be one element from the candidate itinerary list (a list containing one or more flight dictionaries).
        5. If no itinerary can satisfy the user's core requirements, return an empty JSON list `[]`.

        # JSON output
        """


def _model_runtime_inputs(api_parameter: str) -> dict[str, object]:
    return {
        "use_online_model": False,
        "model_folder": "",
        "temperature": 0.0,
        "max_tokens": 4096,
        "top_p": 0.9,
        "repetition_penalty": 1.1,
        api_parameter: "",
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task0_init(
    dag_id: str,
    question: str,
    supplementary_files: object,
) -> dict[str, object]:
    start_time = time.time()
    if not question:
        raise ValueError(f"task {dag_id} question field is empty")
    backend_data = load_airline_backend_data(supplementary_files)
    prompt = _extract_prompt(question)
    features = inference_features(prompt)
    return {
        "backend_data": backend_data,
        "instruction": question,
        "dag_id": dag_id,
        "succ_task_feat": {
            "task1_llm_process": {
                "text_length": features["text_length"],
                "token_count": features["token_count"],
                "reason": 0,
            }
        },
        "curr_task_feat": None,
        "start_time": start_time,
        "end_time": time.time(),
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task1_llm_process(
    dag_id: str,
    instruction: str,
    use_online_model: bool,
    model_folder: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    repetition_penalty: float,
    task1_llm_process_request_api_url: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del (
        use_online_model,
        model_folder,
        top_p,
        repetition_penalty,
        task1_llm_process_request_api_url,
    )
    prompt = _extract_prompt(instruction)
    response = chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    json_text = extract_json_from_llm_output(response.text)
    parsed = json.loads(json_text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM booking extraction must return a JSON object")
    extracted_info = {str(key): value for key, value in parsed.items()}
    user_id = extracted_info["user_id"]
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("LLM booking extraction requires user_id")
    return {
        "extracted_info": extracted_info,
        "user_id": user_id,
        "dag_id": dag_id,
        "curr_task_feat": inference_features(prompt),
        "status": "",
        "start_time": start_time,
        "end_time": time.time(),
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task2a_search_direct_flight(
    dag_id: str,
    extracted_info: dict[str, object],
    backend_data: dict[str, object],
    instruction: str,
) -> dict[str, object]:
    start_time = time.time()
    direct_flights = json.loads(
        SearchDirectFlight.invoke(
            backend_data,
            extracted_info.get("origin"),
            extracted_info.get("destination"),
            extracted_info.get("date"),
        )
    )
    all_candidates = [[flight] for flight in direct_flights]
    prompt = _decision_prompt(instruction)
    serialized_candidates = json.dumps(all_candidates, indent=2)
    text1_feature = {
        "text1_length": len(serialized_candidates),
        "text1_token_count": estimate_tokens(serialized_candidates),
    }
    return {
        "direct_flights": direct_flights,
        "text1_feature": text1_feature,
        "dag_id": dag_id,
        "curr_task_feat": None,
        "succ_task_feat": {
            "task3_llm_fuse_process_filter_and_decide": {
                "prompt_length": len(prompt),
                "prompt_token_count": estimate_tokens(prompt),
                "text1_length": text1_feature["text1_length"],
                "text1_token_count": text1_feature["text1_token_count"],
                "reason": 0,
            }
        },
        "start_time": start_time,
        "end_time": time.time(),
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task2b_search_onestop_flight(
    dag_id: str,
    extracted_info: dict[str, object],
    backend_data: dict[str, object],
    instruction: str,
) -> dict[str, object]:
    start_time = time.time()
    onestop_flights = json.loads(
        SearchOnestopFlight.invoke(
            backend_data,
            extracted_info.get("origin"),
            extracted_info.get("destination"),
            extracted_info.get("date"),
        )
    )
    all_candidates = [[flight] for flight in onestop_flights]
    prompt = _decision_prompt(instruction)
    serialized_candidates = json.dumps(all_candidates, indent=2)
    text2_feature = {
        "text2_length": len(serialized_candidates),
        "text2_token_count": estimate_tokens(serialized_candidates),
    }
    return {
        "onestop_flights": onestop_flights,
        "text2_feature": text2_feature,
        "dag_id": dag_id,
        "curr_task_feat": None,
        "succ_task_feat": {
            "task3_llm_fuse_process_filter_and_decide": {
                "prompt_length": len(prompt),
                "prompt_token_count": estimate_tokens(prompt),
                "text2_length": text2_feature["text2_length"],
                "text2_token_count": text2_feature["text2_token_count"],
                "reason": 0,
            }
        },
        "start_time": start_time,
        "end_time": time.time(),
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task2c_get_user_details(
    dag_id: str,
    user_id: str,
    backend_data: dict[str, object],
) -> dict[str, object]:
    start_time = time.time()
    user_details = json.loads(GetUserDetails.invoke(backend_data, user_id))
    return {
        "user_details": user_details,
        "dag_id": dag_id,
        "start_time": start_time,
        "end_time": time.time(),
    }


@task(task_kind="npu", resources={"cpu_num": 1, "mem": 512}, max_retries=0)
def task3_llm_fuse_process_filter_and_decide(
    dag_id: str,
    instruction: str,
    text1_feature: dict[str, object],
    text2_feature: dict[str, object],
    use_online_model: bool,
    model_folder: str,
    temperature: float,
    max_tokens: int,
    top_p: float,
    repetition_penalty: float,
    direct_flights: list[dict[str, object]],
    onestop_flights: list[list[dict[str, object]]],
    task3_llm_fuse_process_filter_and_decide_request_api_url: str,
) -> dict[str, object]:
    from ascend_maze.inference import chat

    start_time = time.time()
    del (
        use_online_model,
        model_folder,
        top_p,
        repetition_penalty,
        task3_llm_fuse_process_filter_and_decide_request_api_url,
    )
    all_candidates = [[flight] for flight in direct_flights]
    all_candidates.extend(onestop_flights)
    if not all_candidates:
        raise ValueError("no candidate itineraries")
    prompt = _decision_prompt(instruction, all_candidates)
    response = chat(
        [{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    json_text = extract_json_from_llm_output(response.text)
    selected_journey = json.loads(json_text)
    if isinstance(selected_journey, dict):
        selected_journey = [selected_journey]
    if not isinstance(selected_journey, list):
        selected_journey = []
    prompt_features = inference_features(prompt)
    return {
        "selected_journey": selected_journey,
        "dag_id": dag_id,
        "curr_task_feat": {
            "prompt_length": prompt_features["text_length"],
            "prompt_token_count": prompt_features["token_count"],
            "text1_length": text1_feature["text1_length"],
            "text1_token_count": text1_feature["text1_token_count"],
            "text2_length": text2_feature["text2_length"],
            "text2_token_count": text2_feature["text2_token_count"],
            "reason": 0,
        },
        "start_time": start_time,
        "end_time": time.time(),
    }


@task(task_kind="cpu", resources={"cpu_num": 1, "mem": 1024})
def task4_book_reservation(
    dag_id: str,
    user_id: str,
    backend_data: dict[str, object],
    extracted_info: dict[str, object],
    selected_journey: list[dict[str, object]],
    user_details: dict[str, object],
) -> dict[str, object]:
    start_time = time.time()
    passengers = []
    num_passengers = extracted_info.get("num_passengers", 1)
    if "passengers" in extracted_info:
        passengers = extracted_info["passengers"]
    else:
        for _ in range(num_passengers):
            passengers.append(
                {
                    "first_name": user_details["name"]["first_name"],
                    "last_name": user_details["name"]["last_name"],
                    "dob": user_details["dob"],
                }
            )

    flights_for_booking = []
    for flight in selected_journey:
        flights_for_booking.append(
            {
                "flight_number": flight["flight_number"],
                "date": extracted_info["date"],
            }
        )

    cabin = extracted_info.get("cabin")
    total_price = sum(flight["prices"][cabin] for flight in selected_journey) * len(
        passengers
    )
    total_baggages = extracted_info.get("baggages", 0) * len(passengers)
    nonfree_baggages = (
        total_baggages - len(passengers)
        if total_baggages > len(passengers)
        else 0
    )
    total_price += 50 * nonfree_baggages
    if extracted_info.get("insurance") == "yes":
        total_price += 30 * len(passengers)

    payment_methods_for_booking = []
    remaining_balance = total_price
    user_payment_methods = user_details.get("payment_methods", {})
    for payment_id, payment_details in user_payment_methods.items():
        if payment_details["source"] == "certificate" and remaining_balance > 0:
            amount_to_use = min(remaining_balance, payment_details["amount"])
            payment_methods_for_booking.append(
                {"payment_id": payment_id, "amount": amount_to_use}
            )
            remaining_balance -= amount_to_use
    for payment_id, payment_details in user_payment_methods.items():
        if payment_details["source"] == "gift_card" and remaining_balance > 0:
            amount_to_use = min(remaining_balance, payment_details["amount"])
            payment_methods_for_booking.append(
                {"payment_id": payment_id, "amount": amount_to_use}
            )
            remaining_balance -= amount_to_use
    if remaining_balance > 0:
        credit_card_id = None
        for payment_id, payment_details in user_payment_methods.items():
            if payment_details["source"] == "credit_card":
                credit_card_id = payment_id
                break
        if credit_card_id:
            payment_methods_for_booking.append(
                {"payment_id": credit_card_id, "amount": remaining_balance}
            )
            remaining_balance = 0
    if remaining_balance > 0:
        raise ValueError("Payment failed: insufficient methods or balance.")

    booking_args = {
        "user_id": user_id,
        "origin": extracted_info.get("origin"),
        "destination": extracted_info.get("destination"),
        "flight_type": extracted_info.get("flight_type", "one_way"),
        "cabin": cabin,
        "flights": flights_for_booking,
        "passengers": passengers,
        "payment_methods": payment_methods_for_booking,
        "total_baggages": total_baggages,
        "nonfree_baggages": nonfree_baggages,
        "insurance": extracted_info.get("insurance", "no"),
    }
    result = BookReservation.invoke(backend_data, **booking_args)
    return {
        "booking_result": result,
        "dag_id": dag_id,
        "status": "done",
        "result": result,
        "start_time": start_time,
        "end_time": time.time(),
    }


def build() -> Workflow:
    workflow = Workflow(SPEC.name)
    dag_id = workflow.input("dag_id")
    question = workflow.input("question")
    workflow.input("answer")
    supplementary_files = workflow.input("supplementary_files")
    workflow.input("metadata")

    initialized = workflow.add_task(
        task0_init,
        task_name="task0_init",
        inputs={
            "dag_id": dag_id,
            "question": question,
            "supplementary_files": supplementary_files,
        },
    )
    extracted = workflow.add_task(
        task1_llm_process,
        task_name="task1_llm_process",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": dag_id,
            "instruction": initialized.outputs["instruction"],
            **_model_runtime_inputs("task1_llm_process_request_api_url"),
        },
    )
    direct = workflow.add_task(
        task2a_search_direct_flight,
        task_name="task2a_search_direct_flight",
        inputs={
            "dag_id": dag_id,
            "extracted_info": extracted.outputs["extracted_info"],
            "backend_data": initialized.outputs["backend_data"],
            "instruction": initialized.outputs["instruction"],
        },
    )
    onestop = workflow.add_task(
        task2b_search_onestop_flight,
        task_name="task2b_search_onestop_flight",
        inputs={
            "dag_id": dag_id,
            "extracted_info": extracted.outputs["extracted_info"],
            "backend_data": initialized.outputs["backend_data"],
            "instruction": initialized.outputs["instruction"],
        },
    )
    user = workflow.add_task(
        task2c_get_user_details,
        task_name="task2c_get_user_details",
        inputs={
            "dag_id": dag_id,
            "user_id": extracted.outputs["user_id"],
            "backend_data": initialized.outputs["backend_data"],
        },
    )
    decided = workflow.add_task(
        task3_llm_fuse_process_filter_and_decide,
        task_name="task3_llm_fuse_process_filter_and_decide",
        model_anchor={"model": "qwen3-32b", "mode": "service"},
        inputs={
            "dag_id": dag_id,
            "instruction": initialized.outputs["instruction"],
            "text1_feature": direct.outputs["text1_feature"],
            "text2_feature": onestop.outputs["text2_feature"],
            **_model_runtime_inputs(
                "task3_llm_fuse_process_filter_and_decide_request_api_url"
            ),
            "direct_flights": direct.outputs["direct_flights"],
            "onestop_flights": onestop.outputs["onestop_flights"],
        },
    )
    workflow.add_edge(user, decided)
    workflow.add_task(
        task4_book_reservation,
        task_name="task4_book_reservation",
        inputs={
            "dag_id": dag_id,
            "user_id": extracted.outputs["user_id"],
            "backend_data": initialized.outputs["backend_data"],
            "extracted_info": extracted.outputs["extracted_info"],
            "selected_journey": decided.outputs["selected_journey"],
            "user_details": user.outputs["user_details"],
        },
    )
    return workflow
