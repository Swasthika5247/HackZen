from datetime import datetime, time, timedelta
from hashlib import sha256
from itertools import permutations
from math import atan2, ceil, cos, radians, sin, sqrt

from app.data import DATA_NOTICE, ROUTES
from app.schemas import JourneyOption, JourneyRequest, JourneyResponse
from app.services.simulation import estimated_passengers_per_hour

CRITERIA = {
    "crowding": "less_crowding",
    "waiting": "shorter_wait",
    "travel": "faster_travel",
    "seats": "seat_availability",
}


def _minutes_between(stops: list[str], start: str, end: str, mode: str) -> int:
    distance = stops.index(end) - stops.index(start)
    return distance * (7 if mode == "Bus" else 5)


def _waiting_minutes(route_index: int, departure: time) -> int:
    headway = ROUTES[route_index].headway_minutes
    hour = departure.hour
    peak_adjustment = 2 if hour in range(7, 10) or hour in range(16, 19) else 0
    return max(1, round(headway / 2 + peak_adjustment))


def _crowding(route_index: int, departure: time) -> int:
    route_base = [64, 48, 55, 42][route_index]
    is_peak = departure.hour in range(7, 10) or departure.hour in range(16, 19)
    return min(96, route_base + (19 if is_peak else 0))


def _candidates(request: JourneyRequest) -> list[dict]:
    options = []
    origin = request.origin.strip()
    destination = request.destination.strip()

    for route_index, route in enumerate(ROUTES):
        if origin in route.stops and destination in route.stops:
            if route.stops.index(origin) < route.stops.index(destination):
                options.append({
                    "route_indexes": [route_index],
                    "waypoints": [origin, destination],
                    "ride": _minutes_between(route.stops, origin, destination, route.mode),
                    "crowding": _crowding(route_index, request.departure_time),
                })

    for first_index, second_index in permutations(range(len(ROUTES)), 2):
        first, second = ROUTES[first_index], ROUTES[second_index]
        for transfer_stop in sorted(set(first.stops) & set(second.stops)):
            if (
                origin in first.stops
                and destination in second.stops
                and first.stops.index(origin) < first.stops.index(transfer_stop)
                and second.stops.index(transfer_stop) < second.stops.index(destination)
            ):
                first_ride = _minutes_between(first.stops, origin, transfer_stop, first.mode)
                second_ride = _minutes_between(second.stops, transfer_stop, destination, second.mode)
                options.append({
                    "route_indexes": [first_index, second_index],
                    "waypoints": [origin, transfer_stop, destination],
                    "ride": first_ride + second_ride,
                    "transfer_wait": max(2, round(second.headway_minutes * 0.45)),
                    "crowding": round((_crowding(first_index, request.departure_time) + _crowding(second_index, request.departure_time)) / 2),
                })

    return options


def _simulated_fallback(request: JourneyRequest) -> JourneyOption | None:
    if not request.origin_coordinates or not request.destination_coordinates:
        return None

    origin_longitude, origin_latitude = request.origin_coordinates
    destination_longitude, destination_latitude = request.destination_coordinates
    latitude_delta = radians(destination_latitude - origin_latitude)
    longitude_delta = radians(destination_longitude - origin_longitude)
    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(radians(origin_latitude))
        * cos(radians(destination_latitude))
        * sin(longitude_delta / 2) ** 2
    )
    distance_km = round(6371 * 2 * atan2(sqrt(haversine), sqrt(1 - haversine)), 1)
    waiting_minutes = 7
    travel_minutes = max(5, ceil(distance_km * 1.5 + 3))
    departure = datetime.combine(request.travel_date, request.departure_time)
    arrival = departure + timedelta(minutes=waiting_minutes + travel_minutes)
    if request.arrival_deadline is not None and arrival.time() > request.arrival_deadline:
        return None

    fallback_id = "SIM-FALLBACK-" + "-".join(
        part[:24] for part in (request.origin.strip(), request.destination.strip())
    ).replace(" ", "-")
    service_seed = f"{request.origin.strip().casefold()}|{request.destination.strip().casefold()}"
    service_id = f"CR-{sha256(service_seed.encode('utf-8')).hexdigest()[:8].upper()}"
    demand = estimated_passengers_per_hour(fallback_id, request.departure_time.hour)
    passengers_per_departure = demand * 14 / 60
    return JourneyOption(
        id=fallback_id,
        journey_kind="simulated_fallback",
        service_id=service_id,
        service_name="Regional Connector",
        service_type="simulated",
        route_ids=[fallback_id],
        route_names=["Simulated fallback estimate"],
        waypoints=[request.origin.strip(), request.destination.strip()],
        transfer_stops=[],
        data_source="simulated",
        geometry=[(origin_longitude, origin_latitude), (destination_longitude, destination_latitude)],
        geometry_source="schematic",
        stop_coordinates=[(origin_longitude, origin_latitude), (destination_longitude, destination_latitude)],
        distance_km=distance_km,
        estimated_demand_per_hour=demand,
        departure_at=(departure + timedelta(minutes=waiting_minutes)).time(),
        arrival_at=arrival.time(),
        travel_minutes=travel_minutes,
        waiting_minutes=waiting_minutes,
        transfer_count=0,
        crowding_percent=min(100, round(passengers_per_departure / 60 * 100)),
        seat_available=passengers_per_departure < 49,
        score=1,
        explanations=[
            "Rank 1 of 1: only eligible simulated estimate; no relative comparison is available.",
            "No configured transit route matched this request.",
            "Distance is straight-line and timing, waiting, demand, and seat values are generated assumptions, not transit or road directions.",
        ],
    )


async def recommend_journeys(request: JourneyRequest) -> JourneyResponse:
    preferences = request.preferences.model_dump()
    total_weight = sum(preferences.values())
    weights = {key: value / total_weight for key, value in preferences.items()} if total_weight else {
        key: 1 / len(preferences) for key in preferences
    }

    candidates = _candidates(request)
    raw_options = []
    for candidate in candidates:
        route_indexes = candidate["route_indexes"]
        transfers = len(route_indexes) - 1
        initial_wait = _waiting_minutes(route_indexes[0], request.departure_time)
        waiting = initial_wait + candidate.get("transfer_wait", 0)
        total = candidate["ride"] + waiting
        arrival = (datetime.combine(request.travel_date, request.departure_time) + timedelta(minutes=total)).time()
        if request.arrival_deadline is not None and arrival > request.arrival_deadline:
            continue

        raw_options.append({
            **candidate,
            "waiting": waiting,
            "total": total,
            "ride": candidate["ride"],
            "arrival": arrival,
            "transfers": transfers,
            "data_source": "simulated",
            "id": "-".join(ROUTES[index].id for index in route_indexes),
            "route_ids": [ROUTES[index].id for index in route_indexes],
            "route_names": [ROUTES[index].name for index in route_indexes],
            "distance_km": None,
            "geometry": [],
            "road_alternative_index": 0,
        })

        demand = sum(
            estimated_passengers_per_hour(ROUTES[index].id, request.departure_time.hour)
            for index in route_indexes
        )
        capacity = min(ROUTES[index].capacity for index in route_indexes)
        passengers_per_departure = demand * ROUTES[route_indexes[0]].headway_minutes / 60
        raw_options[-1].update({
            "demand": demand,
            "crowding": min(100, round(passengers_per_departure / capacity * 100)),
            "seats": passengers_per_departure < capacity * 0.82,
        })

    if not raw_options:
        fallback = _simulated_fallback(request)
        if fallback is not None:
            return JourneyResponse(
                journeys=[fallback],
                normalized_weights=weights,
                data_notice=(
                    f"{DATA_NOTICE} A coordinate-based simulated fallback is shown when no configured route matches; "
                    "its time, wait, demand, and seat estimates are illustrative only."
                ),
                availability_message=(
                    "No configured demo transit journey matched. This simulated fallback is generated from assumptions, "
                    "not a verified transit itinerary."
                ),
            )
        if candidates:
            availability_message = "No configured demo transit option meets the selected arrival deadline. The current network contains simulated route patterns only."
        elif request.arrival_deadline is not None and request.origin_coordinates and request.destination_coordinates:
            availability_message = "No configured journey or illustrative fallback estimate meets the selected arrival deadline."
        else:
            availability_message = "Journey availability could not be verified for these cities in the configured demo transit network. No transit route is shown."
        return JourneyResponse(
            journeys=[],
            normalized_weights=weights,
            data_notice=DATA_NOTICE,
            availability_message=availability_message,
        )

    criterion_values = {
        "crowding": [option["crowding"] for option in raw_options],
        "waiting": [option["waiting"] for option in raw_options],
        "travel": [option["total"] for option in raw_options],
    }

    def normalized(value: float, values: list[float]) -> float:
        low, high = min(values), max(values)
        return 1.0 if high == low else 1 - (value - low) / (high - low)

    scored = []
    for option in raw_options:
        quality = {
            "crowding": normalized(option["crowding"], criterion_values["crowding"]),
            "waiting": normalized(option["waiting"], criterion_values["waiting"]),
            "travel": normalized(option["total"], criterion_values["travel"]),
            # Seat availability is a benefit: a route with seats should score higher.
            "seats": float(option["seats"]),
        }
        score = sum(quality[criterion] * weights[preference] for criterion, preference in CRITERIA.items())
        scored.append((option, score, quality))

    scored.sort(key=lambda item: (-item[1], item[0]["total"], item[0]["route_indexes"]))
    journeys = []
    for rank, (option, score, quality) in enumerate(scored, start=1):
        route_indexes = option["route_indexes"]
        journey_departure = (datetime.combine(request.travel_date, request.departure_time) + timedelta(minutes=option["waiting"] - option.get("transfer_wait", 0))).time()
        strongest = max(quality, key=quality.get)
        weakest = min(quality, key=quality.get)
        explanations = [
            f"Rank {rank} of {len(scored)}: overall score {score:.3f} using your normalized preference weights.",
        ]
        if len(scored) == 1:
            explanations.append("Only eligible alternative for this request; no relative comparison is available.")
        else:
            explanations.extend([
                f"Strongest relative fit: {'lower ' if strongest != 'seats' else ''}{strongest} ({quality[strongest] * 100:.0f}% among eligible options).",
                f"Lowest relative fit: {'lower ' if weakest != 'seats' else ''}{weakest} ({quality[weakest] * 100:.0f}% among eligible options).",
            ])
        journeys.append(JourneyOption(
            id=option["id"],
            service_id=" + ".join(ROUTES[index].service_id for index in route_indexes),
            service_name=" + ".join(ROUTES[index].service_name for index in route_indexes),
            service_type="verified" if all(ROUTES[index].data_source == "gtfs" for index in route_indexes) else "simulated",
            route_ids=option["route_ids"],
            route_names=option["route_names"],
            waypoints=option["waypoints"],
            transfer_stops=option["waypoints"][1:-1],
            data_source=option["data_source"],
            estimated_demand_per_hour=option["demand"],
            distance_km=option["distance_km"],
            geometry=option["geometry"],
                geometry_source=option.get("geometry_source", "osrm"),
            stop_coordinates=option.get("stop_coordinates", []),
            road_alternative_index=option["road_alternative_index"],
            departure_at=journey_departure,
            arrival_at=option["arrival"],
            travel_minutes=option["ride"],
            waiting_minutes=option["waiting"],
            transfer_count=option["transfers"],
            crowding_percent=option["crowding"],
            seat_available=option["seats"],
            score=round(score, 3),
            explanations=explanations + (["Simulated bus service; no Bay Area GTFS schedule is configured."] if option["data_source"] == "simulated" else []),
        ))

    return JourneyResponse(journeys=journeys, normalized_weights=weights, data_notice=DATA_NOTICE)
