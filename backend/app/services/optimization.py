from datetime import date
from math import ceil

from fastapi import HTTPException

from app.data import DATA_NOTICE, ROUTES
from app.schemas import (
    DemandRequest,
    OptimizationRequest,
    OptimizationResponse,
    ServiceScenario,
)
from app.services.demand import PERIOD_HOURS, predict_demand


def _scenario(headway: int, demand_per_hour: float, capacity: int, round_trip_minutes: int, fleet_available: int | None) -> ServiceScenario:
    passengers_per_departure = demand_per_hour * headway / 60
    load_factor = passengers_per_departure / capacity * 100
    required_vehicles = max(1, ceil(round_trip_minutes / headway))
    return ServiceScenario(
        headway_minutes=headway,
        average_waiting_minutes=round(headway / 2, 1),
        passengers_per_departure=round(passengers_per_departure, 1),
        load_factor_percent=round(load_factor, 1),
        required_vehicles=required_vehicles,
        capacity_feasible=load_factor <= 100,
        fleet_feasible=fleet_available is None or required_vehicles <= fleet_available,
    )


def optimize_scenario(request: OptimizationRequest) -> OptimizationResponse:
    route_index = next((index for index, route in enumerate(ROUTES) if route.id == request.route_id), None)
    if route_index is None:
        raise HTTPException(status_code=404, detail=f"Unknown route '{request.route_id}'.")

    route = ROUTES[route_index]
    demand = predict_demand(DemandRequest(
        route_id=request.route_id,
        travel_date=request.travel_date,
        period=request.period,
    ))
    demand_per_hour = sum(point.estimated_passengers for point in demand.estimates) / len(PERIOD_HOURS[request.period])
    capacity = request.vehicle_capacity or route.capacity
    round_trip_minutes = max(20, (len(route.stops) - 1) * (14 if route.mode == "Bus" else 10))
    current = _scenario(route.headway_minutes, demand_per_hour, capacity, round_trip_minutes, request.fleet_available)
    proposed = _scenario(request.proposed_headway_minutes, demand_per_hour, capacity, round_trip_minutes, request.fleet_available)
    target_headway = max(3, min(60, round(60 * capacity * 0.8 / max(demand_per_hour, 1))))

    reasons = []
    if not proposed.capacity_feasible:
        reasons.append("the proposed passengers per departure exceed the stated vehicle capacity")
    if not proposed.fleet_feasible:
        reasons.append(f"the proposal needs {proposed.required_vehicles} vehicles but only {request.fleet_available} are available")
    if reasons:
        recommendation = "Not feasible: " + "; ".join(reasons) + ". Consider a headway near " + str(target_headway) + " minutes, then recheck fleet availability."
    elif request.proposed_headway_minutes < route.headway_minutes:
        recommendation = f"The shorter headway reduces estimated average wait by {route.headway_minutes / 2 - proposed.average_waiting_minutes:.1f} minutes. It is within the stated capacity and fleet limits."
    elif request.proposed_headway_minutes > route.headway_minutes:
        recommendation = f"The longer headway increases estimated average wait by {proposed.average_waiting_minutes - route.headway_minutes / 2:.1f} minutes while reducing required vehicles."
    else:
        recommendation = "The proposed headway matches the current sample service. No schedule change is indicated by this scenario."

    return OptimizationResponse(
        route_id=route.id,
        route_name=route.name,
        period=request.period,
        vehicle_capacity=capacity,
        fleet_available=request.fleet_available,
        estimated_demand_per_hour=round(demand_per_hour, 1),
        current=current,
        proposed=proposed,
        target_headway_minutes=target_headway,
        feasible=proposed.capacity_feasible and proposed.fleet_feasible,
        recommendation=recommendation,
        data_notice=DATA_NOTICE,
    )