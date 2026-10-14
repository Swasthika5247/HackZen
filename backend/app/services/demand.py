from datetime import date

from fastapi import HTTPException

from app.data import CALIFORNIA_LOCALITIES, DATA_NOTICE, ROUTES
from app.schemas import (
    AnalyticsSummaryResponse,
    DemandPoint,
    DemandRequest,
    DemandResponse,
    HourlySummary,
    PeakHour,
    RouteDemandSummary,
    TimePeriod,
)
from app.services.simulation import estimated_passengers_per_hour

PERIOD_HOURS: dict[TimePeriod, range] = {
    "all_day": range(6, 22),
    "morning": range(6, 11),
    "midday": range(11, 16),
    "evening": range(16, 22),
}


def _route_index(route_id: str) -> int:
    for index, route in enumerate(ROUTES):
        if route.id == route_id:
            return index
    raise HTTPException(status_code=404, detail=f"Unknown route '{route_id}'.")


def _peak_factor(hour: int) -> float:
    return {7: 1.35, 8: 1.55, 9: 1.3, 16: 1.3, 17: 1.55, 18: 1.35}.get(hour, 0.72 if hour in (6, 20, 21) else 0.9)


def _point(route_index: int, hour: int) -> DemandPoint:
    route = ROUTES[route_index]
    estimated_passengers = estimated_passengers_per_hour(route.id, hour)
    passengers_per_departure = round(estimated_passengers * route.headway_minutes / 60, 1)
    utilization = passengers_per_departure / route.capacity * 100
    crowding = min(100, round(utilization * 1.2))
    wait = round(route.headway_minutes / 2 * (1 + max(0, utilization - 60) / 250), 1)
    return DemandPoint(
        route_id=route.id,
        route_name=route.name,
        hour=f"{hour:02d}:00",
        estimated_passengers=estimated_passengers,
        passengers_per_departure=passengers_per_departure,
        crowding_percent=crowding,
        capacity_utilization_percent=round(utilization, 1),
        waiting_minutes=wait,
    )


def predict_demand(request: DemandRequest) -> DemandResponse:
    route_indexes = [_route_index(request.route_id)] if request.route_id else list(range(len(ROUTES)))
    points = [_point(index, hour) for index in route_indexes for hour in PERIOD_HOURS[request.period]]
    return DemandResponse(
        travel_date=request.travel_date,
        period=request.period,
        estimates=points,
        data_notice=DATA_NOTICE,
    )


def analytics_summary(
    route_id: str | None,
    location: str | None,
    period: TimePeriod,
    travel_date: date,
) -> AnalyticsSummaryResponse:
    route_indexes = [_route_index(route_id)] if route_id else list(range(len(ROUTES)))
    location_name = None
    if location is not None:
        known_locations = {
            name.casefold(): name
            for name in [*CALIFORNIA_LOCALITIES, *(stop for route in ROUTES for stop in route.stops)]
        }
        location_name = known_locations.get(location.strip().casefold())
        if location_name is None:
            raise HTTPException(status_code=404, detail=f"Unknown California locality '{location}'.")
        location_name = next(
            (stop for route in ROUTES for stop in route.stops if stop.casefold() == location_name.casefold()),
            location_name,
        )
        route_indexes = [
            index for index in route_indexes
            if any(stop.casefold() == location_name.casefold() for stop in ROUTES[index].stops)
        ]
        if not route_indexes:
            raise HTTPException(status_code=422, detail=f"No configured demo corridor serves {location_name}; choose All California locations or a supported route stop.")

    route_points = {index: [_point(index, hour) for hour in PERIOD_HOURS[period]] for index in route_indexes}
    summaries = []
    for index, points in route_points.items():
        route = ROUTES[index]
        capacity_utilization = sum(point.capacity_utilization_percent for point in points) / len(points)
        summaries.append(RouteDemandSummary(
            route_id=route.id,
            route_name=route.name,
            estimated_passengers=sum(point.estimated_passengers for point in points),
            peak_crowding_percent=max(point.crowding_percent for point in points),
            capacity_utilization_percent=round(capacity_utilization, 1),
            average_waiting_minutes=round(sum(point.waiting_minutes for point in points) / len(points), 1),
        ))

    hourly = []
    for hour_index, hour in enumerate(PERIOD_HOURS[period]):
        points = [values[hour_index] for values in route_points.values()]
        hourly.append(HourlySummary(
            hour=f"{hour:02d}:00",
            estimated_passengers=sum(point.estimated_passengers for point in points),
            crowding_percent=round(sum(point.crowding_percent for point in points) / len(points)),
            capacity_utilization_percent=round(sum(point.capacity_utilization_percent for point in points) / len(points), 1),
            waiting_minutes=round(sum(point.waiting_minutes for point in points) / len(points), 1),
        ))

    peak_hours = sorted(hourly, key=lambda point: point.estimated_passengers, reverse=True)[:3]
    return AnalyticsSummaryResponse(
        route_id=route_id,
        location=location_name,
        period=period,
        routes=summaries,
        hourly=hourly,
        peak_hours=[PeakHour(hour=point.hour, estimated_passengers=point.estimated_passengers) for point in peak_hours],
        data_notice=DATA_NOTICE,
    )