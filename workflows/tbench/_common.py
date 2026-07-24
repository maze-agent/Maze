"""Shared helpers for Ascend-Maze-native tau-bench workflow ports."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import json
import re
from typing import Any


def estimate_tokens(text: str) -> int:
    """Estimate mixed Chinese/English prompt tokens without importing tokenizers."""

    cjk_chars = sum(1 for char in text if "\u4E00" <= char <= "\u9FFF")
    non_cjk_text = re.sub(r"[\u4E00-\u9FFF]", " ", text).replace("\n", " ")
    non_cjk_words_count = len(non_cjk_text.split())
    return cjk_chars + int(non_cjk_words_count * 1.3)


def metadata_dict(metadata: object) -> dict[str, object]:
    if isinstance(metadata, Mapping):
        return {str(key): value for key, value in metadata.items()}
    return {}


def load_json_payload(files: object, filename: str) -> Any:
    if not isinstance(files, Mapping):
        raise ValueError("supplementary_files must be a mapping")
    if filename not in files:
        raise ValueError(f"supplementary_files missing {filename!r}")
    payload = files[filename]
    if isinstance(payload, str):
        return json.loads(payload)
    return copy.deepcopy(payload)


def load_retail_backend_data(supplementary_files: object) -> dict[str, object]:
    return {
        "products": load_json_payload(supplementary_files, "products.json"),
        "users": load_json_payload(supplementary_files, "users.json"),
        "orders": load_json_payload(supplementary_files, "orders.json"),
    }


def load_airline_backend_data(supplementary_files: object) -> dict[str, object]:
    return {
        "flights": load_json_payload(supplementary_files, "flights.json"),
        "users": load_json_payload(supplementary_files, "users.json"),
        "reservations": load_json_payload(supplementary_files, "reservations.json"),
    }


def airline_booking_extract_prompt(instruction: str) -> str:
    return f"""
You are a professional flight booking assistant. Carefully read the user's flight
booking instructions and extract the key information in strict JSON format.

Required fields:
- "user_id": user ID, such as "mia_jackson_2156".
- "origin": origin airport code, three letters.
- "destination": destination airport code, three letters.
- "date": departure date in YYYY-MM-DD format.
- "cabin": one of "basic_economy", "economy", "business".
- "baggages": number of baggage items per passenger.
- "insurance": "yes" or "no".
- "constraints": list of all other preferences.

Optional fields:
- "num_passengers": integer, default 1.
- "passengers": list of passenger objects.
- "flight_type": "one_way" or "round_trip", default "one_way".

User instructions:
{instruction}

JSON output:
""".strip()


def airline_booking_decision_prompt(
    instruction: str,
    candidates: list[list[dict[str, object]]],
) -> str:
    return f"""
You are a professional and meticulous flight booking decision assistant.
Select the single most suitable itinerary from the candidate itineraries based
on the user's original request and constraints.

User's original request:
{instruction}

Candidate itineraries:
{json.dumps(candidates, ensure_ascii=False, indent=2, sort_keys=True)}

Return the chosen itinerary in strict JSON format. The returned value must be one
candidate itinerary: a list containing one or more flight dictionaries. If no
candidate satisfies the request, return [].
""".strip()


def airline_cancel_extract_prompt(instruction: str) -> str:
    return f"""
You are a professional flight booking assistant. Carefully read the user's
instructions and extract cancellation plus replacement-booking information in
strict JSON format.

Required fields:
- "user_id": user ID.
- "cancel_reservation_id": reservation ID to cancel.
- "origin": new booking origin airport code.
- "destination": new booking destination airport code.
- "departure_date": new outbound departure date in YYYY-MM-DD format.
- "return_date": optional return date in YYYY-MM-DD format.
- "cabin": one of "basic_economy", "economy", "business".
- "baggages": number of baggage items.
- "insurance": "yes" or "no".
- "payment_preference": optional payment preference.
- "constraints": list of all other preferences.

Optional fields:
- "num_passengers": integer, default 1.
- "passengers": list of passenger objects.

User instructions:
{instruction}

JSON output:
""".strip()


def airline_cancel_decision_prompt(
    cancel_request: dict[str, object],
    outbound_flights: list[dict[str, object]],
    return_flights: list[dict[str, object]],
) -> str:
    return f"""
You are a professional flight booking decision assistant. Select the most
suitable replacement flights from the candidate options.

User preferences:
{json.dumps(cancel_request, ensure_ascii=False, indent=2, sort_keys=True)}

Outbound flights:
{json.dumps(outbound_flights, ensure_ascii=False, indent=2, sort_keys=True)}

Return flights:
{json.dumps(return_flights, ensure_ascii=False, indent=2, sort_keys=True)}

Return strict JSON in this format:
{{
  "outbound_flight_number": "xxx",
  "return_flight_number": "xxx"
}}

Use null for a return flight if this is a one-way booking.
""".strip()


def retail_cancel_prompt(instruction: str) -> str:
    return f"""
You are a professional retail order processing assistant. Please carefully review
the user's instructions and extract all orders that need to be canceled in strict
JSON format. If the user mentions multiple orders to cancel, return a JSON list
containing multiple objects.

Each JSON object must include the following fields:
- "order_id": string, the order ID the user wants to cancel, starting with "#".
- "reason": string, optional cancellation reason from the user.

The only valid cancellation reasons for the backend tool are:
- "no longer needed"
- "ordered by mistake"

User instructions:
{instruction}

JSON output:
""".strip()


def retail_return_prompt(instruction: str) -> str:
    return f"""
You are a professional retail order processing assistant. Please carefully review
the user's instructions and extract all key information required for returns in
strict JSON format.

Required fields to extract:
- "order_id": string, the order ID the user wants to process, starting with "#".
- "items": list of strings, product names the user wants to return; use ["all"]
  if all items should be returned.
- "reason": string, optional reason provided by the user.
- "user_name": string, optional user's full name.
- "zip_code": string, optional user's postal code.
- "email": string, optional user's email address.
- "payment_method_id": string, optional refund method ID explicitly specified by
  the user.

User instructions:
{instruction}

JSON output:
""".strip()


def retail_modify_prompt(instruction: str) -> str:
    return f"""
You are a premier retail order processing assistant. Carefully review the user's
instructions and return a single JSON object containing all modification details
in strict JSON format.

The JSON object may include one or more optional fields:
- "user_info": object with "email", "user_name", "zip_code".
- "item_modification": object with "order_id", "items_to_modify",
  "new_items_spec", and optional "payment_method_id".
- "payment_modification": object with "order_id" and "payment_method_id".
- "order_address_modification": object with "order_id", "address1",
  "address2", "city", "state", "country", and "zip".
- "user_address_modification": object with "user_id", "address1",
  "address2", "city", "state", "country", and "zip".

User instructions:
{instruction}

JSON output:
""".strip()


def retail_cancel_modify_prompt(instruction: str) -> str:
    return f"""
You are a top-tier retail order processing assistant. Carefully review the user's
instructions and return a single JSON object containing all operation details in
strict JSON format.

The JSON object may include these optional fields:
- "user_info": object with "email", "user_name", "zip_code".
- "cancellation": object or list of objects with "order_id" and optional
  "reason".
- "modification": object or list of objects with "order_id", "item_to_modify",
  "new_item_spec", and optional "payment_method_id".

For "modification", "item_to_modify" must include "name" and optional
"attributes"; "new_item_spec" only includes changed "attributes".

User instructions:
{instruction}

JSON output:
""".strip()


def extract_json_from_llm_output(llm_output: str) -> str:
    """Extract a JSON object or array from raw LLM output."""

    stripped = llm_output.strip()
    if not stripped:
        return ""
    try:
        json.loads(stripped)
        return stripped
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    candidates: list[str] = []
    for left, right in (("{", "}"), ("[", "]")):
        start = stripped.find(left)
        end = stripped.rfind(right)
        if start != -1 and end != -1 and start < end:
            candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            continue
    return ""


def parse_cancellation_requests(llm_output: str) -> list[dict[str, object]]:
    json_payload = extract_json_from_llm_output(llm_output)
    if not json_payload:
        raise ValueError("LLM output does not contain a JSON object or array")
    parsed = json.loads(json_payload)
    if isinstance(parsed, dict):
        for field in ("cancellations", "orders", "requests"):
            nested = parsed.get(field)
            if isinstance(nested, list):
                return _request_list(nested)
        return _request_list([parsed])
    if isinstance(parsed, list):
        return _request_list(parsed)
    raise ValueError("LLM cancellation payload must be a JSON object or array")


def parse_return_request(llm_output: str) -> dict[str, object]:
    json_payload = extract_json_from_llm_output(llm_output)
    if not json_payload:
        raise ValueError("LLM output does not contain a JSON object")
    parsed = json.loads(json_payload)
    if isinstance(parsed, list):
        if len(parsed) != 1:
            raise ValueError("retail_return expects exactly one return request")
        parsed = parsed[0]
    if not isinstance(parsed, Mapping):
        raise ValueError("retail_return payload must be a JSON object")
    request = {str(key): value for key, value in parsed.items()}
    order_id = request.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("retail_return request requires order_id")
    items = request.get("items")
    if not isinstance(items, list):
        raise ValueError("retail_return request requires items as a list")
    normalized_items: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item:
            raise ValueError("retail_return items must be non-empty strings")
        normalized_items.append(item)
    request["items"] = normalized_items
    for optional in (
        "reason",
        "user_name",
        "zip_code",
        "email",
        "payment_method_id",
    ):
        value = request.get(optional, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"retail_return {optional} must be a string")
        request[optional] = value
    return request


def parse_modify_request(llm_output: str) -> dict[str, object]:
    json_payload = extract_json_from_llm_output(llm_output)
    if not json_payload:
        raise ValueError("LLM output does not contain a JSON object")
    parsed = json.loads(json_payload)
    if not isinstance(parsed, Mapping):
        raise ValueError("retail_modify payload must be a JSON object")
    request = {str(key): value for key, value in parsed.items()}
    for field in (
        "user_info",
        "item_modification",
        "payment_modification",
        "order_address_modification",
        "user_address_modification",
    ):
        value = request.get(field)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            raise ValueError(f"retail_modify {field} must be an object")
        request[field] = {str(key): item for key, item in value.items()}
    if "item_modification" in request:
        item = request["item_modification"]
        if isinstance(item, dict):
            if "items_to_modify" in item:
                item["items_to_modify"] = _object_list(item["items_to_modify"])
            if "new_items_spec" in item:
                item["new_items_spec"] = _object_list(item["new_items_spec"])
    return request


def parse_cancel_modify_request(llm_output: str) -> dict[str, object]:
    json_payload = extract_json_from_llm_output(llm_output)
    if not json_payload:
        raise ValueError("LLM output does not contain a JSON object")
    parsed = json.loads(json_payload)
    if not isinstance(parsed, Mapping):
        raise ValueError("retail_cancel_modify payload must be a JSON object")
    request = {str(key): value for key, value in parsed.items()}
    user_info = request.get("user_info")
    if user_info is not None:
        if not isinstance(user_info, Mapping):
            raise ValueError("retail_cancel_modify user_info must be an object")
        request["user_info"] = {str(key): item for key, item in user_info.items()}
    cancellation = request.get("cancellation")
    if cancellation is not None:
        request["cancellation"] = _operation_list(
            cancellation,
            "retail_cancel_modify cancellation",
        )
    modification = request.get("modification")
    if modification is not None:
        request["modification"] = _operation_list(
            modification,
            "retail_cancel_modify modification",
        )
    return request


def parse_airline_booking_request(llm_output: str) -> dict[str, object]:
    json_payload = extract_json_from_llm_output(llm_output)
    if not json_payload:
        raise ValueError("LLM output does not contain a JSON object")
    parsed = json.loads(json_payload)
    if not isinstance(parsed, Mapping):
        raise ValueError("airline booking payload must be a JSON object")
    request = {str(key): value for key, value in parsed.items()}
    for field in ("user_id", "origin", "destination", "date", "cabin"):
        value = request.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"airline booking request requires {field}")
    baggages = request.get("baggages", 0)
    if isinstance(baggages, bool) or not isinstance(baggages, int) or baggages < 0:
        raise ValueError("airline booking baggages must be a non-negative integer")
    insurance = request.get("insurance", "no")
    if insurance not in {"yes", "no"}:
        raise ValueError("airline booking insurance must be yes or no")
    constraints = request.get("constraints", [])
    if not isinstance(constraints, list):
        constraints = []
    request["constraints"] = [str(item) for item in constraints]
    num_passengers = request.get("num_passengers", 1)
    if (
        isinstance(num_passengers, bool)
        or not isinstance(num_passengers, int)
        or num_passengers <= 0
    ):
        num_passengers = 1
    request["num_passengers"] = num_passengers
    passengers = request.get("passengers", [])
    if isinstance(passengers, list):
        request["passengers"] = [
            {str(key): item for key, item in passenger.items()}
            for passenger in passengers
            if isinstance(passenger, Mapping)
        ]
    else:
        request["passengers"] = []
    flight_type = request.get("flight_type", "one_way")
    request["flight_type"] = flight_type if flight_type in {"one_way", "round_trip"} else "one_way"
    return request


def parse_selected_airline_journey(llm_output: str) -> list[dict[str, object]]:
    json_payload = extract_json_from_llm_output(llm_output)
    if not json_payload:
        return []
    parsed = json.loads(json_payload)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    journey: list[dict[str, object]] = []
    for item in parsed:
        if isinstance(item, Mapping):
            journey.append({str(key): value for key, value in item.items()})
    return journey


def parse_airline_cancel_request(llm_output: str) -> dict[str, object]:
    json_payload = extract_json_from_llm_output(llm_output)
    if not json_payload:
        raise ValueError("LLM output does not contain a JSON object")
    parsed = json.loads(json_payload)
    if not isinstance(parsed, Mapping):
        raise ValueError("airline cancel payload must be a JSON object")
    request = {str(key): value for key, value in parsed.items()}
    for field in (
        "user_id",
        "cancel_reservation_id",
        "origin",
        "destination",
        "departure_date",
        "cabin",
    ):
        value = request.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"airline cancel request requires {field}")
    baggages = request.get("baggages", 0)
    if isinstance(baggages, bool) or not isinstance(baggages, int) or baggages < 0:
        raise ValueError("airline cancel baggages must be a non-negative integer")
    insurance = request.get("insurance", "no")
    if insurance not in {"yes", "no"}:
        raise ValueError("airline cancel insurance must be yes or no")
    constraints = request.get("constraints", [])
    if not isinstance(constraints, list):
        constraints = []
    request["constraints"] = [str(item) for item in constraints]
    return_date = request.get("return_date", "")
    request["return_date"] = return_date if isinstance(return_date, str) else ""
    payment_preference = request.get("payment_preference", "")
    request["payment_preference"] = (
        payment_preference if isinstance(payment_preference, str) else ""
    )
    num_passengers = request.get("num_passengers", 1)
    if (
        isinstance(num_passengers, bool)
        or not isinstance(num_passengers, int)
        or num_passengers <= 0
    ):
        num_passengers = 1
    request["num_passengers"] = num_passengers
    passengers = request.get("passengers", [])
    if isinstance(passengers, list):
        request["passengers"] = [
            {str(key): item for key, item in passenger.items()}
            for passenger in passengers
            if isinstance(passenger, Mapping)
        ]
    else:
        request["passengers"] = []
    request["flight_type"] = "round_trip" if request["return_date"] else "one_way"
    request["date"] = request["departure_date"]
    return request


def parse_airline_selected_flights(llm_output: str) -> dict[str, object]:
    json_payload = extract_json_from_llm_output(llm_output)
    if not json_payload:
        return {
            "outbound_flight_number": "",
            "return_flight_number": "",
        }
    parsed = json.loads(json_payload)
    if not isinstance(parsed, Mapping):
        return {
            "outbound_flight_number": "",
            "return_flight_number": "",
        }
    outbound = parsed.get("outbound_flight_number", "")
    inbound = parsed.get("return_flight_number", "")
    return {
        "outbound_flight_number": outbound if isinstance(outbound, str) else "",
        "return_flight_number": inbound if isinstance(inbound, str) else "",
    }


def _request_list(items: list[object]) -> list[dict[str, object]]:
    requests: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("each cancellation request must be a JSON object")
        request = {str(key): value for key, value in item.items()}
        order_id = request.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("each cancellation request requires order_id")
        reason = request.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            raise ValueError("cancellation reason must be a string")
        request["reason"] = reason
        requests.append(request)
    return requests


def inference_features(prompt: str) -> dict[str, int]:
    return {
        "text_length": len(prompt),
        "token_count": estimate_tokens(prompt),
    }


def search_direct_flights(
    backend_data: dict[str, object],
    *,
    origin: str,
    destination: str,
    date: str,
) -> list[dict[str, object]]:
    flights = _mapping(backend_data, "flights")
    results: list[dict[str, object]] = []
    for flight in flights.values():
        if not isinstance(flight, dict):
            continue
        if flight.get("origin") != origin or flight.get("destination") != destination:
            continue
        dates = flight.get("dates", {})
        if not isinstance(dates, dict):
            continue
        date_info = dates.get(date)
        if not isinstance(date_info, dict) or date_info.get("status") != "available":
            continue
        result = {str(key): value for key, value in flight.items() if key != "dates"}
        result.update(copy.deepcopy(date_info))
        results.append(result)
    return results


def search_onestop_flights(
    backend_data: dict[str, object],
    *,
    origin: str,
    destination: str,
    date: str,
) -> list[list[dict[str, object]]]:
    flights = _mapping(backend_data, "flights")
    results: list[list[dict[str, object]]] = []
    for flight1 in flights.values():
        if not isinstance(flight1, dict) or flight1.get("origin") != origin:
            continue
        for flight2 in flights.values():
            if not isinstance(flight2, dict):
                continue
            if flight2.get("destination") != destination:
                continue
            if flight1.get("destination") != flight2.get("origin"):
                continue
            date2 = _next_day(date) if "+1" in str(flight1.get("scheduled_arrival_time_est", "")) else date
            if str(flight1.get("scheduled_arrival_time_est", "")) > str(
                flight2.get("scheduled_departure_time_est", "")
            ):
                continue
            dates1 = flight1.get("dates", {})
            dates2 = flight2.get("dates", {})
            if not isinstance(dates1, dict) or not isinstance(dates2, dict):
                continue
            info1 = dates1.get(date)
            info2 = dates2.get(date2)
            if not isinstance(info1, dict) or not isinstance(info2, dict):
                continue
            if info1.get("status") != "available" or info2.get("status") != "available":
                continue
            result1 = {str(key): value for key, value in flight1.items() if key != "dates"}
            result1.update(copy.deepcopy(info1))
            result1["date"] = date
            result2 = {str(key): value for key, value in flight2.items() if key != "dates"}
            result2.update(copy.deepcopy(info2))
            result2["date"] = date2
            results.append([result1, result2])
    return results


def get_airline_user_details(
    backend_data: dict[str, object],
    user_id: str,
) -> dict[str, object]:
    users = _mapping(backend_data, "users")
    user = users.get(user_id)
    if not isinstance(user, dict):
        return {
            "status": "error",
            "details": "Error: user not found",
            "user_details": {},
        }
    return {
        "status": "success",
        "details": "",
        "user_details": copy.deepcopy(user),
    }


def get_airline_reservation_details(
    backend_data: dict[str, object],
    reservation_id: str,
) -> dict[str, object]:
    reservations = _mapping(backend_data, "reservations")
    reservation = reservations.get(reservation_id)
    if not isinstance(reservation, dict):
        return {
            "status": "error",
            "details": "Error: reservation not found",
            "reservation_details": {},
        }
    return {
        "status": "success",
        "details": "",
        "reservation_details": copy.deepcopy(reservation),
    }


def cancel_airline_reservation(
    backend_data: dict[str, object],
    reservation_id: str,
) -> dict[str, object]:
    reservations = _mapping(backend_data, "reservations")
    reservation = reservations.get(reservation_id)
    if not isinstance(reservation, dict):
        return {"status": "error", "details": "Error: reservation not found"}
    refunds = []
    payment_history = reservation.get("payment_history", [])
    if isinstance(payment_history, list):
        for payment in payment_history:
            if not isinstance(payment, dict):
                continue
            refunds.append(
                {
                    "payment_id": payment.get("payment_id"),
                    "amount": -payment.get("amount", 0),
                }
            )
        payment_history.extend(refunds)
    reservation["status"] = "cancelled"
    return {"status": "success", "details": "", "result": copy.deepcopy(reservation)}


def search_airline_replacement_flights(
    backend_data: dict[str, object],
    cancel_request: dict[str, object],
) -> dict[str, object]:
    outbound = search_direct_flights(
        backend_data,
        origin=str(cancel_request.get("origin", "")),
        destination=str(cancel_request.get("destination", "")),
        date=str(cancel_request.get("departure_date", "")),
    )
    return_date = cancel_request.get("return_date")
    if isinstance(return_date, str) and return_date:
        inbound = search_direct_flights(
            backend_data,
            origin=str(cancel_request.get("destination", "")),
            destination=str(cancel_request.get("origin", "")),
            date=return_date,
        )
    else:
        inbound = []
    return {
        "outbound_flights": outbound,
        "return_flights": inbound,
    }


def airline_candidate_journeys(
    direct_flights: list[dict[str, object]],
    onestop_flights: list[list[dict[str, object]]],
) -> list[list[dict[str, object]]]:
    candidates = [[flight] for flight in direct_flights]
    candidates.extend(onestop_flights)
    return candidates


def book_airline_reservation(
    backend_data: dict[str, object],
    booking_request: dict[str, object],
    selected_journey: list[dict[str, object]],
    user_details: dict[str, object],
) -> dict[str, object]:
    if not selected_journey:
        return {"status": "error", "details": "No selected itinerary."}
    user_id = str(booking_request.get("user_id", ""))
    cabin = str(booking_request.get("cabin", ""))
    passengers = _airline_passengers(booking_request, user_details)
    flights_for_booking = [
        {
            "flight_number": str(flight.get("flight_number", "")),
            "date": str(flight.get("date", booking_request.get("date", ""))),
        }
        for flight in selected_journey
    ]
    total_baggages = int(booking_request.get("baggages", 0)) * len(passengers)
    nonfree_baggages = (
        total_baggages - len(passengers)
        if total_baggages > len(passengers)
        else 0
    )
    total_price = 0.0
    for flight in selected_journey:
        prices = flight.get("prices", {})
        if isinstance(prices, dict) and isinstance(prices.get(cabin), (int, float)):
            total_price += float(prices[cabin]) * len(passengers)
    total_price += 50 * nonfree_baggages
    if booking_request.get("insurance") == "yes":
        total_price += 30 * len(passengers)
    payment_methods = _airline_payment_methods(user_details, total_price)
    if sum(float(item["amount"]) for item in payment_methods) != total_price:
        return {
            "status": "error",
            "details": "Payment failed: insufficient methods or balance.",
        }
    result = _book_reservation_tool(
        backend_data,
        user_id=user_id,
        origin=str(booking_request.get("origin", "")),
        destination=str(booking_request.get("destination", "")),
        flight_type=str(booking_request.get("flight_type", "one_way")),
        cabin=cabin,
        flights=flights_for_booking,
        passengers=passengers,
        payment_methods=payment_methods,
        total_baggages=total_baggages,
        nonfree_baggages=nonfree_baggages,
        insurance=str(booking_request.get("insurance", "no")),
    )
    return result


def book_airline_replacement_reservation(
    backend_data: dict[str, object],
    cancel_request: dict[str, object],
    selected_flights: dict[str, object],
    outbound_flights: list[dict[str, object]],
    return_flights: list[dict[str, object]],
    user_details: dict[str, object],
) -> dict[str, object]:
    outbound_number = selected_flights.get("outbound_flight_number")
    if not isinstance(outbound_number, str) or not outbound_number:
        return {"status": "error", "details": "No outbound flight selected."}
    outbound = _flight_by_number(outbound_flights, outbound_number)
    if outbound is None:
        return {
            "status": "error",
            "details": f"Selected outbound flight not found: {outbound_number}",
        }
    outbound = copy.deepcopy(outbound)
    outbound["date"] = cancel_request.get("departure_date")
    selected_journey = [outbound]
    return_number = selected_flights.get("return_flight_number")
    if isinstance(return_number, str) and return_number:
        inbound = _flight_by_number(return_flights, return_number)
        if inbound is None:
            return {
                "status": "error",
                "details": f"Selected return flight not found: {return_number}",
            }
        inbound = copy.deepcopy(inbound)
        inbound["date"] = cancel_request.get("return_date")
        selected_journey.append(inbound)
    booking_request = dict(cancel_request)
    booking_request["date"] = cancel_request.get("departure_date")
    booking_request["flight_type"] = "round_trip" if len(selected_journey) > 1 else "one_way"
    return book_airline_reservation(
        backend_data,
        booking_request,
        selected_journey,
        user_details,
    )


def cancel_pending_order(
    backend_data: dict[str, object],
    *,
    order_id: str,
    reason: str,
) -> dict[str, object]:
    orders = _mapping(backend_data, "orders")
    users = _mapping(backend_data, "users")
    if order_id not in orders:
        return {
            "order_id": order_id,
            "status": "error",
            "details": "Error: order not found",
        }
    order = orders[order_id]
    if not isinstance(order, dict):
        return {
            "order_id": order_id,
            "status": "error",
            "details": "Error: malformed order",
        }
    if order.get("status") != "pending":
        return {
            "order_id": order_id,
            "status": "error",
            "details": "Error: non-pending order cannot be cancelled",
        }
    if reason not in {"no longer needed", "ordered by mistake"}:
        return {
            "order_id": order_id,
            "status": "error",
            "details": "Error: invalid reason",
        }

    refunds: list[dict[str, object]] = []
    for payment in list(order.get("payment_history", [])):
        if not isinstance(payment, dict):
            continue
        payment_id = str(payment.get("payment_method_id", ""))
        amount = payment.get("amount", 0)
        refunds.append(
            {
                "transaction_type": "refund",
                "amount": amount,
                "payment_method_id": payment_id,
            }
        )
        if "gift_card" in payment_id:
            user = users.get(order.get("user_id"))
            if isinstance(user, dict):
                payment_methods = user.get("payment_methods", {})
                if isinstance(payment_methods, dict):
                    payment_method = payment_methods.get(payment_id)
                    if isinstance(payment_method, dict):
                        balance = payment_method.get("balance", 0)
                        if isinstance(balance, int | float) and isinstance(
                            amount, int | float
                        ):
                            payment_method["balance"] = round(balance + amount, 2)

    order["status"] = "cancelled"
    order["cancel_reason"] = reason
    history = order.setdefault("payment_history", [])
    if isinstance(history, list):
        history.extend(refunds)
    return {
        "order_id": order_id,
        "status": "success",
        "result": copy.deepcopy(order),
    }


def execute_retail_cancellations(
    backend_data: dict[str, object],
    cancellation_requests: list[dict[str, object]],
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for request in cancellation_requests:
        order_id = str(request.get("order_id", ""))
        reason = str(request.get("reason", ""))
        results.append(
            cancel_pending_order(
                backend_data,
                order_id=order_id,
                reason=reason,
            )
        )
    return results


def find_retail_user_for_modify(
    backend_data: dict[str, object],
    modify_request: dict[str, object],
) -> dict[str, object]:
    user_info = modify_request.get("user_info")
    if not isinstance(user_info, dict) or not user_info:
        return {
            "status": "skipped",
            "details": "Instruction lacks user hints; skipping user lookup.",
            "user_id": "",
            "user_details": {},
        }
    user_id = ""
    email = user_info.get("email")
    if isinstance(email, str) and email:
        user_id = find_user_id_by_email(backend_data, email)
    if not user_id:
        user_name = user_info.get("user_name")
        zip_code = user_info.get("zip_code")
        if isinstance(user_name, str) and isinstance(zip_code, str):
            if user_name and zip_code:
                parts = user_name.split()
                user_id = find_user_id_by_name_zip(
                    backend_data,
                    first_name=parts[0],
                    last_name=" ".join(parts[1:]) if len(parts) > 1 else "",
                    zip_code=zip_code,
                )
    if not user_id:
        return {
            "status": "error",
            "details": "Error: user not found",
            "user_id": "",
            "user_details": {},
        }
    users = _mapping(backend_data, "users")
    user = users.get(user_id)
    if not isinstance(user, dict):
        return {
            "status": "error",
            "details": "Error: malformed user",
            "user_id": user_id,
            "user_details": {},
        }
    user_details = copy.deepcopy(user)
    user_details["id"] = user_id
    return {
        "status": "success",
        "details": "",
        "user_id": user_id,
        "user_details": user_details,
    }


def find_retail_user(
    backend_data: dict[str, object],
    request: dict[str, object],
) -> dict[str, object]:
    users = _mapping(backend_data, "users")
    user_id = ""
    email = request.get("email")
    if isinstance(email, str) and email:
        user_id = find_user_id_by_email(backend_data, email)
    if not user_id:
        user_name = request.get("user_name")
        zip_code = request.get("zip_code")
        if isinstance(user_name, str) and isinstance(zip_code, str):
            if user_name and zip_code:
                parts = user_name.split()
                first_name = parts[0]
                last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
                user_id = find_user_id_by_name_zip(
                    backend_data,
                    first_name=first_name,
                    last_name=last_name,
                    zip_code=zip_code,
                )
    if not user_id:
        return {
            "status": "error",
            "details": "Error: user not found",
            "user_id": "",
            "user_details": {},
        }
    user = users.get(user_id)
    if not isinstance(user, dict):
        return {
            "status": "error",
            "details": "Error: malformed user",
            "user_id": user_id,
            "user_details": {},
        }
    return {
        "status": "success",
        "details": "",
        "user_id": user_id,
        "user_details": copy.deepcopy(user),
    }


def find_user_id_by_email(backend_data: dict[str, object], email: str) -> str:
    users = _mapping(backend_data, "users")
    for user_id, profile in users.items():
        if not isinstance(user_id, str) or not isinstance(profile, dict):
            continue
        profile_email = profile.get("email")
        if isinstance(profile_email, str) and profile_email.lower() == email.lower():
            return user_id
    return ""


def find_user_id_by_name_zip(
    backend_data: dict[str, object],
    *,
    first_name: str,
    last_name: str,
    zip_code: str,
) -> str:
    users = _mapping(backend_data, "users")
    for user_id, profile in users.items():
        if not isinstance(user_id, str) or not isinstance(profile, dict):
            continue
        name = profile.get("name", {})
        address = profile.get("address", {})
        if not isinstance(name, dict) or not isinstance(address, dict):
            continue
        if (
            str(name.get("first_name", "")).lower() == first_name.lower()
            and str(name.get("last_name", "")).lower() == last_name.lower()
            and str(address.get("zip", "")) == zip_code
        ):
            return user_id
    return ""


def get_retail_order_details_map(
    backend_data: dict[str, object],
    modify_request: dict[str, object],
) -> dict[str, object]:
    order_ids: set[str] = set()
    for field in (
        "item_modification",
        "payment_modification",
        "order_address_modification",
    ):
        value = modify_request.get(field)
        if isinstance(value, dict):
            order_id = value.get("order_id")
            if isinstance(order_id, str) and order_id:
                order_ids.add(order_id)
    orders = _mapping(backend_data, "orders")
    details: dict[str, object] = {}
    errors: dict[str, object] = {}
    for order_id in sorted(order_ids):
        order = orders.get(order_id)
        if isinstance(order, dict):
            details[order_id] = copy.deepcopy(order)
        else:
            errors[order_id] = "Error: order not found"
    return {
        "status": "success" if not errors else "partial",
        "order_details_map": details,
        "errors": errors,
    }


def get_retail_cancel_modify_order_details_map(
    backend_data: dict[str, object],
    request: dict[str, object],
) -> dict[str, object]:
    order_ids: set[str] = set()
    for field in ("cancellation", "modification"):
        operations = request.get(field)
        if isinstance(operations, list):
            for operation in operations:
                if isinstance(operation, dict):
                    order_id = operation.get("order_id")
                    if isinstance(order_id, str) and order_id:
                        order_ids.add(order_id)
    orders = _mapping(backend_data, "orders")
    details: dict[str, object] = {}
    errors: dict[str, object] = {}
    for order_id in sorted(order_ids):
        order = orders.get(order_id)
        if isinstance(order, dict):
            details[order_id] = copy.deepcopy(order)
        else:
            errors[order_id] = "Error: order not found"
    return {
        "status": "success" if not errors else "partial",
        "order_details_map": details,
        "errors": errors,
    }


def get_retail_order_details(
    backend_data: dict[str, object],
    order_id: str,
) -> dict[str, object]:
    orders = _mapping(backend_data, "orders")
    order = orders.get(order_id)
    if not isinstance(order, dict):
        return {
            "status": "error",
            "details": "Error: order not found",
            "order_details": {},
        }
    return {
        "status": "success",
        "details": "",
        "order_details": copy.deepcopy(order),
    }


def execute_retail_cancel_modify_operations(
    backend_data: dict[str, object],
    request: dict[str, object],
    user_lookup: dict[str, object],
    order_details_map: dict[str, object],
) -> dict[str, object]:
    final_results: dict[str, object] = {}

    cancellation_ops = request.get("cancellation")
    if isinstance(cancellation_ops, list) and cancellation_ops:
        cancellation_results = []
        for operation in cancellation_ops:
            if not isinstance(operation, dict):
                cancellation_results.append(
                    {
                        "status": "error",
                        "details": "Malformed cancellation operation.",
                    }
                )
                continue
            order_id = str(operation.get("order_id", ""))
            if not order_id:
                cancellation_results.append(
                    {
                        "status": "skipped",
                        "details": "No order_id for cancellation.",
                    }
                )
                continue
            cancellation_results.append(
                cancel_pending_order(
                    backend_data,
                    order_id=order_id,
                    reason=str(operation.get("reason", "")),
                )
            )
        final_results["cancellation_result"] = _single_or_list(cancellation_results)

    modification_ops = request.get("modification")
    if isinstance(modification_ops, list) and modification_ops:
        modification_results = []
        for operation in modification_ops:
            if not isinstance(operation, dict):
                modification_results.append(
                    {
                        "status": "error",
                        "details": "Malformed modification operation.",
                    }
                )
                continue
            modification_results.append(
                _execute_cancel_modify_item_modification(
                    backend_data,
                    operation,
                    user_lookup,
                    order_details_map,
                )
            )
        final_results["modification_result"] = _single_or_list(modification_results)

    if not final_results:
        return {
            "status": "skipped",
            "details": "Workflow finished without side effects.",
            "results": {},
        }
    leaf_results: list[dict[str, object]] = []
    for value in final_results.values():
        if isinstance(value, dict):
            leaf_results.append(value)
        elif isinstance(value, list):
            leaf_results.extend(item for item in value if isinstance(item, dict))
    overall = (
        "success"
        if leaf_results
        and all(result.get("status") == "success" for result in leaf_results)
        else "partial"
    )
    return {
        "status": overall,
        "details": "",
        "results": final_results,
    }


def execute_retail_modifications(
    backend_data: dict[str, object],
    modify_request: dict[str, object],
    user_lookup: dict[str, object],
    order_details_map: dict[str, object],
) -> dict[str, object]:
    final_results: dict[str, object] = {}

    item_op = modify_request.get("item_modification")
    if isinstance(item_op, dict) and item_op:
        final_results["item_modification_result"] = _execute_item_modification(
            backend_data,
            item_op,
            user_lookup,
            order_details_map,
        )

    payment_op = modify_request.get("payment_modification")
    if isinstance(payment_op, dict) and payment_op:
        order_id = str(payment_op.get("order_id", ""))
        payment_method_id = str(payment_op.get("payment_method_id", ""))
        final_results["payment_modification_result"] = modify_pending_order_payment(
            backend_data,
            order_id=order_id,
            payment_method_id=payment_method_id,
        )

    order_address_op = modify_request.get("order_address_modification")
    if isinstance(order_address_op, dict) and order_address_op:
        final_results["order_address_modification_result"] = (
            modify_pending_order_address(
                backend_data,
                order_id=str(order_address_op.get("order_id", "")),
                address=_address_from_operation(order_address_op),
            )
        )

    user_address_op = modify_request.get("user_address_modification")
    if isinstance(user_address_op, dict) and user_address_op:
        user_id = str(user_address_op.get("user_id", ""))
        if not user_id:
            user_id = str(user_lookup.get("user_id", ""))
        final_results["user_address_modification_result"] = modify_user_address(
            backend_data,
            user_id=user_id,
            address=_address_from_operation(user_address_op),
        )

    if not final_results:
        return {
            "status": "skipped",
            "details": "Workflow finished without side effects.",
            "results": {},
        }
    overall = (
        "success"
        if all(
            isinstance(result, dict) and result.get("status") == "success"
            for result in final_results.values()
        )
        else "partial"
    )
    return {
        "status": overall,
        "details": "",
        "results": final_results,
    }


def modify_pending_order_address(
    backend_data: dict[str, object],
    *,
    order_id: str,
    address: dict[str, object],
) -> dict[str, object]:
    orders = _mapping(backend_data, "orders")
    order = orders.get(order_id)
    if not isinstance(order, dict):
        return {"status": "error", "details": "Error: order not found"}
    if order.get("status") != "pending":
        return {
            "status": "error",
            "details": "Error: non-pending order cannot be modified",
        }
    order["address"] = address
    return {"status": "success", "details": "", "result": copy.deepcopy(order)}


def modify_pending_order_payment(
    backend_data: dict[str, object],
    *,
    order_id: str,
    payment_method_id: str,
) -> dict[str, object]:
    orders = _mapping(backend_data, "orders")
    users = _mapping(backend_data, "users")
    order = orders.get(order_id)
    if not isinstance(order, dict):
        return {"status": "error", "details": "Error: order not found"}
    if order.get("status") != "pending":
        return {
            "status": "error",
            "details": "Error: non-pending order cannot be modified",
        }
    user = users.get(order.get("user_id"))
    if not isinstance(user, dict):
        return {"status": "error", "details": "Error: user not found"}
    payment_methods = user.get("payment_methods", {})
    if not isinstance(payment_methods, dict) or payment_method_id not in payment_methods:
        return {"status": "error", "details": "Error: payment method not found"}
    history = order.get("payment_history", [])
    if (
        not isinstance(history, list)
        or len(history) != 1
        or not isinstance(history[0], dict)
        or history[0].get("transaction_type") != "payment"
    ):
        return {
            "status": "error",
            "details": "Error: there should be exactly one payment for a pending order",
        }
    old_payment_method_id = str(history[0].get("payment_method_id", ""))
    if old_payment_method_id == payment_method_id:
        return {
            "status": "error",
            "details": "Error: the new payment method should be different",
        }
    amount = history[0].get("amount", 0)
    payment_method = payment_methods[payment_method_id]
    if not isinstance(payment_method, dict):
        return {"status": "error", "details": "Error: malformed payment method"}
    if (
        payment_method.get("source") == "gift_card"
        and isinstance(amount, (int, float))
        and payment_method.get("balance", 0) < amount
    ):
        return {
            "status": "error",
            "details": "Error: insufficient gift card balance to pay for the order",
        }
    history.extend(
        [
            {
                "transaction_type": "payment",
                "amount": amount,
                "payment_method_id": payment_method_id,
            },
            {
                "transaction_type": "refund",
                "amount": amount,
                "payment_method_id": old_payment_method_id,
            },
        ]
    )
    if payment_method.get("source") == "gift_card" and isinstance(amount, (int, float)):
        payment_method["balance"] = round(payment_method.get("balance", 0) - amount, 2)
    old_payment = payment_methods.get(old_payment_method_id)
    if (
        isinstance(old_payment, dict)
        and "gift_card" in old_payment_method_id
        and isinstance(amount, (int, float))
    ):
        old_payment["balance"] = round(old_payment.get("balance", 0) + amount, 2)
    return {"status": "success", "details": "", "result": copy.deepcopy(order)}


def modify_user_address(
    backend_data: dict[str, object],
    *,
    user_id: str,
    address: dict[str, object],
) -> dict[str, object]:
    users = _mapping(backend_data, "users")
    user = users.get(user_id)
    if not isinstance(user, dict):
        return {"status": "error", "details": "Error: user not found"}
    user["address"] = address
    return {"status": "success", "details": "", "result": copy.deepcopy(user)}


def modify_pending_order_items(
    backend_data: dict[str, object],
    *,
    order_id: str,
    item_ids: list[str],
    new_item_ids: list[str],
    payment_method_id: str,
) -> dict[str, object]:
    products = _mapping(backend_data, "products")
    orders = _mapping(backend_data, "orders")
    users = _mapping(backend_data, "users")
    order = orders.get(order_id)
    if not isinstance(order, dict):
        return {"status": "error", "details": "Error: order not found"}
    if order.get("status") != "pending":
        return {
            "status": "error",
            "details": "Error: non-pending order cannot be modified",
        }
    order_items = order.get("items", [])
    if not isinstance(order_items, list):
        return {"status": "error", "details": "Error: malformed order items"}
    all_item_ids = [
        item.get("item_id")
        for item in order_items
        if isinstance(item, dict)
    ]
    for item_id in item_ids:
        if item_ids.count(item_id) > all_item_ids.count(item_id):
            return {"status": "error", "details": f"Error: {item_id} not found"}
    if len(item_ids) != len(new_item_ids):
        return {
            "status": "error",
            "details": "Error: the number of items to be exchanged should match",
        }
    diff_price = 0.0
    for item_id, new_item_id in zip(item_ids, new_item_ids):
        item = _order_item_by_id(order_items, item_id)
        if item is None:
            return {"status": "error", "details": f"Error: {item_id} not found"}
        product = products.get(item.get("product_id"))
        if not isinstance(product, dict):
            return {"status": "error", "details": "Error: product not found"}
        variants = product.get("variants", {})
        if not isinstance(variants, dict):
            return {"status": "error", "details": "Error: malformed variants"}
        variant = variants.get(new_item_id)
        if not isinstance(variant, dict) or not variant.get("available"):
            return {
                "status": "error",
                "details": f"Error: new item {new_item_id} not found or available",
            }
        old_price = item.get("price", 0)
        new_price = variant.get("price", 0)
        if isinstance(old_price, (int, float)) and isinstance(new_price, (int, float)):
            diff_price += float(new_price) - float(old_price)

    user = users.get(order.get("user_id"))
    if not isinstance(user, dict):
        return {"status": "error", "details": "Error: user not found"}
    payment_methods = user.get("payment_methods", {})
    if not isinstance(payment_methods, dict) or payment_method_id not in payment_methods:
        return {"status": "error", "details": "Error: payment method not found"}
    payment_method = payment_methods[payment_method_id]
    if not isinstance(payment_method, dict):
        return {"status": "error", "details": "Error: malformed payment method"}
    if (
        payment_method.get("source") == "gift_card"
        and payment_method.get("balance", 0) < diff_price
    ):
        return {
            "status": "error",
            "details": "Error: insufficient gift card balance to pay for the new item",
        }
    history = order.setdefault("payment_history", [])
    if isinstance(history, list):
        history.append(
            {
                "transaction_type": "payment" if diff_price > 0 else "refund",
                "amount": abs(round(diff_price, 2)),
                "payment_method_id": payment_method_id,
            }
        )
    if payment_method.get("source") == "gift_card":
        payment_method["balance"] = round(
            payment_method.get("balance", 0) - diff_price,
            2,
        )
    for item_id, new_item_id in zip(item_ids, new_item_ids):
        item = _order_item_by_id(order_items, item_id)
        if item is None:
            continue
        product = products.get(item.get("product_id"))
        if not isinstance(product, dict):
            continue
        variants = product.get("variants", {})
        if not isinstance(variants, dict):
            continue
        variant = variants.get(new_item_id)
        if not isinstance(variant, dict):
            continue
        item["item_id"] = new_item_id
        item["price"] = variant.get("price")
        item["options"] = copy.deepcopy(variant.get("options", {}))
    order["status"] = "pending (item modified)"
    return {"status": "success", "details": "", "result": copy.deepcopy(order)}


def execute_retail_return(
    backend_data: dict[str, object],
    request: dict[str, object],
    user_details: dict[str, object],
    order_details: dict[str, object],
) -> dict[str, object]:
    payment_method_id = request.get("payment_method_id")
    if not isinstance(payment_method_id, str) or not payment_method_id:
        payment_methods = user_details.get("payment_methods")
        if not isinstance(payment_methods, dict) or not payment_methods:
            return {
                "action": "return",
                "status": "error",
                "details": "No payment method available for refund.",
            }
        first_method = next(iter(payment_methods.values()))
        if not isinstance(first_method, dict):
            return {
                "action": "return",
                "status": "error",
                "details": "No payment method available for refund.",
            }
        selected = first_method.get("id")
        if not isinstance(selected, str) or not selected:
            return {
                "action": "return",
                "status": "error",
                "details": "No payment method available for refund.",
            }
        payment_method_id = selected

    item_ids = _return_item_ids(order_details, request)
    if not item_ids:
        return {
            "action": "return",
            "status": "error",
            "details": "Return requires matching items.",
        }
    order_id = order_details.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        return {
            "action": "return",
            "status": "error",
            "details": "Order details are missing order_id.",
        }
    result = return_delivered_order_items(
        backend_data,
        order_id=order_id,
        item_ids=item_ids,
        payment_method_id=payment_method_id,
    )
    if result["status"] != "success":
        return {
            "action": "return",
            "status": "error",
            "details": result["details"],
        }
    return {
        "action": "return",
        "status": "success",
        "result": result["result"],
    }


def return_delivered_order_items(
    backend_data: dict[str, object],
    *,
    order_id: str,
    item_ids: list[str],
    payment_method_id: str,
) -> dict[str, object]:
    orders = _mapping(backend_data, "orders")
    users = _mapping(backend_data, "users")
    order = orders.get(order_id)
    if not isinstance(order, dict):
        return {"status": "error", "details": "Error: order not found"}
    if order.get("status") != "delivered":
        return {
            "status": "error",
            "details": "Error: non-delivered order cannot be returned",
        }
    user = users.get(order.get("user_id"))
    if not isinstance(user, dict):
        return {"status": "error", "details": "Error: user not found"}
    payment_methods = user.get("payment_methods", {})
    if not isinstance(payment_methods, dict) or payment_method_id not in payment_methods:
        return {"status": "error", "details": "Error: payment method not found"}
    payment_history = order.get("payment_history", [])
    first_payment_method = ""
    if (
        isinstance(payment_history, list)
        and payment_history
        and isinstance(payment_history[0], dict)
    ):
        first_payment_method = str(payment_history[0].get("payment_method_id", ""))
    if "gift_card" not in payment_method_id and payment_method_id != first_payment_method:
        return {
            "status": "error",
            "details": (
                "Error: payment method should be either the original payment "
                "method or a gift card"
            ),
        }

    all_item_ids = [
        item.get("item_id")
        for item in order.get("items", [])
        if isinstance(item, dict)
    ]
    for item_id in item_ids:
        if item_ids.count(item_id) > all_item_ids.count(item_id):
            return {"status": "error", "details": "Error: some item not found"}

    order["status"] = "return requested"
    order["return_items"] = sorted(item_ids)
    order["return_payment_method_id"] = payment_method_id
    return {
        "status": "success",
        "details": "",
        "result": copy.deepcopy(order),
    }


def format_retail_return_result(final_result: dict[str, object]) -> str:
    return "final outcome:\n" + json.dumps(
        final_result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def format_retail_modify_result(final_result: dict[str, object]) -> str:
    return "final outcome:\n" + json.dumps(
        final_result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def format_retail_cancel_modify_result(final_result: dict[str, object]) -> str:
    return "final outcome:\n" + json.dumps(
        final_result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def format_retail_cancel_result(cancel_results: list[dict[str, object]]) -> str:
    return "final outcome:\n" + json.dumps(
        cancel_results,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _mapping(parent: dict[str, object], name: str) -> dict[str, object]:
    value = parent.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"retail backend data field {name!r} must be a mapping")
    return value


def _return_item_ids(
    order_details: dict[str, object],
    request: dict[str, object],
) -> list[str]:
    raw_items = request.get("items")
    if not isinstance(raw_items, list):
        return []
    lowered = [str(item).lower() for item in raw_items]
    order_items = order_details.get("items", [])
    if not isinstance(order_items, list):
        return []
    if "all" in lowered:
        return [
            str(item.get("item_id"))
            for item in order_items
            if isinstance(item, dict) and item.get("item_id") is not None
        ]
    return [
        str(item.get("item_id"))
        for item in order_items
        if (
            isinstance(item, dict)
            and str(item.get("name", "")).lower() in lowered
            and item.get("item_id") is not None
        )
    ]


def _execute_item_modification(
    backend_data: dict[str, object],
    item_op: dict[str, object],
    user_lookup: dict[str, object],
    order_details_map: dict[str, object],
) -> dict[str, object]:
    order_id = str(item_op.get("order_id", ""))
    order_details = order_details_map.get(order_id)
    if not isinstance(order_details, dict):
        return {
            "status": "error",
            "details": f"Could not fetch order {order_id} details.",
        }
    items_to_modify = item_op.get("items_to_modify", [])
    new_items_spec = item_op.get("new_items_spec", [])
    if not isinstance(items_to_modify, list) or not isinstance(new_items_spec, list):
        return {
            "status": "error",
            "details": "Item modification specs must be lists.",
        }
    if len(items_to_modify) != len(new_items_spec):
        return {
            "status": "error",
            "details": "Item list and new spec counts mismatch.",
        }
    item_ids: list[str] = []
    new_item_ids: list[str] = []
    for index, item_spec in enumerate(items_to_modify):
        if not isinstance(item_spec, dict):
            return {"status": "error", "details": "Malformed item spec."}
        original = _find_item_details_in_order(order_details, item_spec)
        if original is None:
            return {"status": "error", "details": f"item not found: {item_spec}"}
        new_spec = new_items_spec[index]
        if not isinstance(new_spec, dict):
            return {"status": "error", "details": "Malformed new item spec."}
        product_id = original.get("product_id")
        if not isinstance(product_id, str) or not product_id:
            return {"status": "error", "details": "product id not found"}
        new_item_id = _find_new_product_variant_id(
            backend_data,
            product_id,
            original.get("options", {}),
            new_spec,
        )
        if not new_item_id:
            return {
                "status": "error",
                "details": f"No catalog variant matches requested spec: {new_spec}",
            }
        item_ids.append(str(original.get("item_id", "")))
        new_item_ids.append(new_item_id)
    payment_method_id = item_op.get("payment_method_id")
    if not isinstance(payment_method_id, str) or not payment_method_id:
        user_details = user_lookup.get("user_details", {})
        if isinstance(user_details, dict):
            payment_methods = user_details.get("payment_methods", {})
            if isinstance(payment_methods, dict) and payment_methods:
                first = next(iter(payment_methods.values()))
                if isinstance(first, dict):
                    candidate = first.get("id")
                    if isinstance(candidate, str):
                        payment_method_id = candidate
    if not isinstance(payment_method_id, str) or not payment_method_id:
        return {
            "status": "error",
            "details": "payment_method_id is required for item modification",
        }
    return modify_pending_order_items(
        backend_data,
        order_id=order_id,
        item_ids=item_ids,
        new_item_ids=new_item_ids,
        payment_method_id=payment_method_id,
    )


def _execute_cancel_modify_item_modification(
    backend_data: dict[str, object],
    operation: dict[str, object],
    user_lookup: dict[str, object],
    order_details_map: dict[str, object],
) -> dict[str, object]:
    order_id = str(operation.get("order_id", ""))
    if not order_id:
        return {
            "status": "skipped",
            "details": "No order_id for modification.",
        }
    item_op = {
        "order_id": order_id,
        "items_to_modify": _object_list(operation.get("item_to_modify", {})),
        "new_items_spec": _object_list(operation.get("new_item_spec", {})),
    }
    payment_method_id = operation.get("payment_method_id")
    if isinstance(payment_method_id, str) and payment_method_id:
        item_op["payment_method_id"] = payment_method_id
    return _execute_item_modification(
        backend_data,
        item_op,
        user_lookup,
        order_details_map,
    )


def _find_item_details_in_order(
    order_details: dict[str, object],
    item_spec: dict[str, object],
) -> dict[str, object] | None:
    name = item_spec.get("name")
    if not isinstance(name, str) or not name:
        return None
    attributes = item_spec.get("attributes", {})
    if not isinstance(attributes, dict):
        attributes = {}
    target_attributes = {
        str(key).lower(): str(value).lower()
        for key, value in attributes.items()
    }
    for item in order_details.get("items", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")).lower() != name.lower():
            continue
        options = item.get("options", {})
        if not isinstance(options, dict):
            options = {}
        normalized_options = {
            str(key).lower(): str(value).lower()
            for key, value in options.items()
        }
        if all(normalized_options.get(key) == value for key, value in target_attributes.items()):
            return item
    return None


def _find_new_product_variant_id(
    backend_data: dict[str, object],
    product_id: str,
    original_item_options: object,
    new_item_spec: dict[str, object],
) -> str:
    products = _mapping(backend_data, "products")
    product = products.get(product_id)
    if not isinstance(product, dict):
        return ""
    target_options = (
        dict(original_item_options) if isinstance(original_item_options, dict) else {}
    )
    attributes = new_item_spec.get("attributes", {})
    if isinstance(attributes, dict):
        target_options.update(attributes)
    normalized_target = {
        str(key).lower(): str(value).lower()
        for key, value in target_options.items()
    }
    variants = product.get("variants", {})
    if not isinstance(variants, dict):
        return ""
    for variant in variants.values():
        if not isinstance(variant, dict):
            continue
        options = variant.get("options", {})
        if not isinstance(options, dict):
            continue
        normalized_variant = {
            str(key).lower(): str(value).lower()
            for key, value in options.items()
        }
        if normalized_target == normalized_variant:
            item_id = variant.get("item_id")
            return item_id if isinstance(item_id, str) else ""
    return ""


def _order_item_by_id(items: list[object], item_id: str) -> dict[str, object] | None:
    for item in items:
        if isinstance(item, dict) and item.get("item_id") == item_id:
            return item
    return None


def _address_from_operation(operation: dict[str, object]) -> dict[str, object]:
    return {
        "address1": str(operation.get("address1", "")),
        "address2": str(operation.get("address2", "")),
        "city": str(operation.get("city", "")),
        "state": str(operation.get("state", "")),
        "country": str(operation.get("country", "")),
        "zip": str(operation.get("zip", "")),
    }


def _object_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _operation_list(value: object, label: str) -> list[dict[str, object]]:
    raw = _object_list(value)
    operations: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError(f"{label} entries must be objects")
        operations.append({str(key): entry for key, entry in item.items()})
    return operations


def _single_or_list(values: list[dict[str, object]]) -> dict[str, object] | list[dict[str, object]]:
    if len(values) == 1:
        return values[0]
    return values


def _next_day(date: str) -> str:
    parts = date.split("-")
    if len(parts) != 3:
        return date
    try:
        day = int(parts[2]) + 1
    except ValueError:
        return date
    return f"{parts[0]}-{parts[1]}-{day:02d}"


def _airline_passengers(
    booking_request: dict[str, object],
    user_details: dict[str, object],
) -> list[dict[str, object]]:
    passengers = booking_request.get("passengers")
    if isinstance(passengers, list) and passengers:
        normalized: list[dict[str, object]] = []
        for passenger in passengers:
            if isinstance(passenger, Mapping):
                normalized.append({str(key): value for key, value in passenger.items()})
        if normalized:
            return normalized
    count = booking_request.get("num_passengers", 1)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        count = 1
    name = user_details.get("name", {})
    if not isinstance(name, dict):
        name = {}
    passenger = {
        "first_name": str(name.get("first_name", "")),
        "last_name": str(name.get("last_name", "")),
        "dob": str(user_details.get("dob", "")),
    }
    return [copy.deepcopy(passenger) for _ in range(count)]


def _airline_payment_methods(
    user_details: dict[str, object],
    total_price: float,
) -> list[dict[str, object]]:
    payment_methods = user_details.get("payment_methods", {})
    if not isinstance(payment_methods, dict):
        return []
    remaining = total_price
    selected: list[dict[str, object]] = []
    for source in ("certificate", "gift_card"):
        for payment_id, details in payment_methods.items():
            if not isinstance(payment_id, str) or not isinstance(details, dict):
                continue
            if details.get("source") != source or remaining <= 0:
                continue
            balance = details.get("amount", 0)
            if not isinstance(balance, (int, float)):
                continue
            amount = min(remaining, float(balance))
            if amount > 0:
                selected.append({"payment_id": payment_id, "amount": amount})
                remaining = round(remaining - amount, 2)
    if remaining > 0:
        for payment_id, details in payment_methods.items():
            if not isinstance(payment_id, str) or not isinstance(details, dict):
                continue
            if details.get("source") == "credit_card":
                selected.append({"payment_id": payment_id, "amount": remaining})
                remaining = 0
                break
    return selected


def _book_reservation_tool(
    backend_data: dict[str, object],
    *,
    user_id: str,
    origin: str,
    destination: str,
    flight_type: str,
    cabin: str,
    flights: list[dict[str, object]],
    passengers: list[dict[str, object]],
    payment_methods: list[dict[str, object]],
    total_baggages: int,
    nonfree_baggages: int,
    insurance: str,
) -> dict[str, object]:
    reservations = _mapping(backend_data, "reservations")
    users = _mapping(backend_data, "users")
    all_flights = _mapping(backend_data, "flights")
    user = users.get(user_id)
    if not isinstance(user, dict):
        return {"status": "error", "details": "Error: user not found"}
    reservation_id = "HATHAT"
    if reservation_id in reservations:
        reservation_id = "HATHAU"
        if reservation_id in reservations:
            reservation_id = "HATHAV"
    reservation = {
        "reservation_id": reservation_id,
        "user_id": user_id,
        "origin": origin,
        "destination": destination,
        "flight_type": flight_type,
        "cabin": cabin,
        "flights": copy.deepcopy(flights),
        "passengers": passengers,
        "payment_history": payment_methods,
        "created_at": "2024-05-15T15:00:00",
        "total_baggages": total_baggages,
        "nonfree_baggages": nonfree_baggages,
        "insurance": insurance,
    }

    total_price = 0.0
    for flight in reservation["flights"]:
        if not isinstance(flight, dict):
            return {"status": "error", "details": "Error: malformed flight"}
        flight_number = str(flight.get("flight_number", ""))
        flight_data = all_flights.get(flight_number)
        if not isinstance(flight_data, dict):
            return {
                "status": "error",
                "details": f"Error: flight {flight_number} not found",
            }
        dates = flight_data.get("dates", {})
        if not isinstance(dates, dict) or flight.get("date") not in dates:
            return {
                "status": "error",
                "details": f"Error: flight {flight_number} not found on date {flight.get('date')}",
            }
        flight_date_data = dates[flight["date"]]
        if not isinstance(flight_date_data, dict):
            return {"status": "error", "details": "Error: malformed flight date"}
        if flight_date_data.get("status") != "available":
            return {
                "status": "error",
                "details": f"Error: flight {flight_number} not available",
            }
        seats = flight_date_data.get("available_seats", {})
        if not isinstance(seats, dict) or seats.get(cabin, 0) < len(passengers):
            return {
                "status": "error",
                "details": f"Error: not enough seats on flight {flight_number}",
            }
        prices = flight_date_data.get("prices", {})
        if not isinstance(prices, dict) or not isinstance(prices.get(cabin), (int, float)):
            return {"status": "error", "details": "Error: cabin price not found"}
        flight["price"] = prices[cabin]
        flight["origin"] = flight_data.get("origin")
        flight["destination"] = flight_data.get("destination")
        total_price += float(flight["price"]) * len(passengers)

    if insurance == "yes":
        total_price += 30 * len(passengers)
    total_price += 50 * nonfree_baggages

    user_payment_methods = user.get("payment_methods", {})
    if not isinstance(user_payment_methods, dict):
        return {"status": "error", "details": "Error: payment method not found"}
    for payment_method in payment_methods:
        payment_id = str(payment_method.get("payment_id", ""))
        amount = payment_method.get("amount", 0)
        if payment_id not in user_payment_methods:
            return {
                "status": "error",
                "details": f"Error: payment method {payment_id} not found",
            }
        user_payment_method = user_payment_methods[payment_id]
        if not isinstance(user_payment_method, dict):
            return {"status": "error", "details": "Error: malformed payment method"}
        if user_payment_method.get("source") in {"gift_card", "certificate"}:
            balance = user_payment_method.get("amount", 0)
            if isinstance(balance, (int, float)) and isinstance(amount, (int, float)):
                if balance < amount:
                    return {
                        "status": "error",
                        "details": f"Error: not enough balance in {payment_id}",
                    }
    paid = sum(
        float(payment.get("amount", 0))
        for payment in payment_methods
        if isinstance(payment.get("amount", 0), (int, float))
    )
    if paid != total_price:
        return {
            "status": "error",
            "details": f"Error: payment amount does not add up, total price is {total_price}, but paid {paid}",
        }

    for payment_method in payment_methods:
        payment_id = str(payment_method.get("payment_id", ""))
        amount = payment_method.get("amount", 0)
        user_payment_method = user_payment_methods[payment_id]
        if (
            isinstance(user_payment_method, dict)
            and user_payment_method.get("source") == "gift_card"
            and isinstance(amount, (int, float))
        ):
            user_payment_method["amount"] = round(
                user_payment_method.get("amount", 0) - amount,
                2,
            )
        elif (
            isinstance(user_payment_method, dict)
            and user_payment_method.get("source") == "certificate"
        ):
            del user_payment_methods[payment_id]

    reservations[reservation_id] = reservation
    user_reservations = user.setdefault("reservations", [])
    if isinstance(user_reservations, list):
        user_reservations.append(reservation_id)
    return {"status": "success", "details": "", "result": copy.deepcopy(reservation)}


def _flight_by_number(
    flights: list[dict[str, object]],
    flight_number: str,
) -> dict[str, object] | None:
    for flight in flights:
        if flight.get("flight_number") == flight_number:
            return flight
    return None
