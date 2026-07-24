"""tau-bench airline tools used by the migrated workflows."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any


class SearchDirectFlight:
    @staticmethod
    def invoke(
        data: dict[str, Any],
        origin: str,
        destination: str,
        date: str,
    ) -> str:
        flights = data["flights"]
        results = []
        for flight in flights.values():
            if flight["origin"] == origin and flight["destination"] == destination:
                if (
                    date in flight["dates"]
                    and flight["dates"][date]["status"] == "available"
                ):
                    results.append({key: value for key, value in flight.items() if key != "dates"})
                    results[-1].update(flight["dates"][date])
        return json.dumps(results)


class SearchOnestopFlight:
    @staticmethod
    def invoke(
        data: dict[str, Any],
        origin: str,
        destination: str,
        date: str,
    ) -> str:
        flights = data["flights"]
        results = []
        for flight1 in flights.values():
            if flight1["origin"] != origin:
                continue
            for flight2 in flights.values():
                if (
                    flight2["destination"] != destination
                    or flight1["destination"] != flight2["origin"]
                ):
                    continue
                date2 = (
                    f"2024-05-{int(date[-2:]) + 1}"
                    if "+1" in flight1["scheduled_arrival_time_est"]
                    else date
                )
                if (
                    flight1["scheduled_arrival_time_est"]
                    > flight2["scheduled_departure_time_est"]
                ):
                    continue
                if date not in flight1["dates"] or date2 not in flight2["dates"]:
                    continue
                if (
                    flight1["dates"][date]["status"] != "available"
                    or flight2["dates"][date2]["status"] != "available"
                ):
                    continue
                result1 = {
                    key: value for key, value in flight1.items() if key != "dates"
                }
                result1.update(flight1["dates"][date])
                result1["date"] = date
                result2 = {
                    key: value for key, value in flight2.items() if key != "dates"
                }
                result2.update(flight2["dates"][date])
                result2["date"] = date2
                results.append([result1, result2])
        return json.dumps(results)


class GetUserDetails:
    @staticmethod
    def invoke(data: dict[str, Any], user_id: str) -> str:
        users = data["users"]
        if user_id in users:
            return json.dumps(users[user_id])
        return "Error: user not found"


class BookReservation:
    @staticmethod
    def invoke(
        data: dict[str, Any],
        user_id: str,
        origin: str,
        destination: str,
        flight_type: str,
        cabin: str,
        flights: list[dict[str, Any]],
        passengers: list[dict[str, Any]],
        payment_methods: list[dict[str, Any]],
        total_baggages: int,
        nonfree_baggages: int,
        insurance: str,
    ) -> str:
        reservations, users = data["reservations"], data["users"]
        if user_id not in users:
            return "Error: user not found"
        user = users[user_id]

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
            "flights": deepcopy(flights),
            "passengers": passengers,
            "payment_history": payment_methods,
            "created_at": "2024-05-15T15:00:00",
            "total_baggages": total_baggages,
            "nonfree_baggages": nonfree_baggages,
            "insurance": insurance,
        }

        total_price = 0
        for flight in reservation["flights"]:
            flight_number = flight["flight_number"]
            if flight_number not in data["flights"]:
                return f"Error: flight {flight_number} not found"
            flight_data = data["flights"][flight_number]
            if flight["date"] not in flight_data["dates"]:
                return f"Error: flight {flight_number} not found on date {flight['date']}"
            flight_date_data = flight_data["dates"][flight["date"]]
            if flight_date_data["status"] != "available":
                return f"Error: flight {flight_number} not available on date {flight['date']}"
            if flight_date_data["available_seats"][cabin] < len(passengers):
                return f"Error: not enough seats on flight {flight_number}"
            flight["price"] = flight_date_data["prices"][cabin]
            flight["origin"] = flight_data["origin"]
            flight["destination"] = flight_data["destination"]
            total_price += flight["price"] * len(passengers)

        if insurance == "yes":
            total_price += 30 * len(passengers)
        total_price += 50 * nonfree_baggages

        for payment_method in payment_methods:
            payment_id = payment_method["payment_id"]
            amount = payment_method["amount"]
            if payment_id not in user["payment_methods"]:
                return f"Error: payment method {payment_id} not found"
            if user["payment_methods"][payment_id]["source"] in {
                "gift_card",
                "certificate",
            } and user["payment_methods"][payment_id]["amount"] < amount:
                return f"Error: not enough balance in payment method {payment_id}"
        paid = sum(payment["amount"] for payment in payment_methods)
        if paid != total_price:
            return (
                "Error: payment amount does not add up, total price is "
                f"{total_price}, but paid {paid}"
            )

        for payment_method in payment_methods:
            payment_id = payment_method["payment_id"]
            amount = payment_method["amount"]
            if user["payment_methods"][payment_id]["source"] == "gift_card":
                user["payment_methods"][payment_id]["amount"] -= amount
            elif user["payment_methods"][payment_id]["source"] == "certificate":
                del user["payment_methods"][payment_id]

        reservations[reservation_id] = reservation
        user["reservations"].append(reservation_id)
        return json.dumps(reservation)
