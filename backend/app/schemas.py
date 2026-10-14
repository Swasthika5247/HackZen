from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Route(StrictModel):
    id: str
    name: str
    service_id: str
    service_name: str
    mode: str
    origin: str
    destination: str
    stops: list[str]
    headway_minutes: int = Field(gt=0)
    capacity: int = Field(gt=0)
    operating_hours: str
    data_source: Literal["gtfs", "simulated"] = "simulated"


class RouteListResponse(StrictModel):
    routes: list[Route]
    data_notice: str


class LocationListResponse(StrictModel):
    locations: list[str]
    data_notice: str


class LocationSuggestion(StrictModel):
    name: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LocationSearchResponse(StrictModel):
    locations: list[LocationSuggestion]


class HealthResponse(StrictModel):
    status: str
    service: str


class JourneyPreferences(StrictModel):
    less_crowding: float = Field(default=0.25, ge=0, le=1)
    shorter_wait: float = Field(default=0.25, ge=0, le=1)
    faster_travel: float = Field(default=0.25, ge=0, le=1)
    seat_availability: float = Field(default=0.25, ge=0, le=1)


class JourneyRequest(StrictModel):
    origin: str = Field(min_length=2, max_length=80)
    destination: str = Field(min_length=2, max_length=80)
    travel_date: date
    departure_time: time
    arrival_deadline: time | None = None
    preferences: JourneyPreferences = Field(default_factory=JourneyPreferences)
    origin_coordinates: tuple[float, float] | None = None
    destination_coordinates: tuple[float, float] | None = None

    @model_validator(mode="after")
    def validate_trip(self) -> "JourneyRequest":
        if self.origin.casefold() == self.destination.casefold():
            raise ValueError("Origin and destination must be different stops.")
        if self.arrival_deadline is not None and self.arrival_deadline <= self.departure_time:
            raise ValueError("Arrival deadline must be later than departure time.")
        return self


class JourneyOption(StrictModel):
    id: str
    journey_kind: Literal["configured_demo", "simulated_fallback"] = "configured_demo"
    service_id: str
    service_name: str
    service_type: Literal["verified", "simulated"]
    route_ids: list[str]
    route_names: list[str]
    waypoints: list[str]
    transfer_stops: list[str] = Field(default_factory=list)
    data_source: Literal["gtfs", "simulated"] = "simulated"
    geometry: list[tuple[float, float]] = Field(default_factory=list)
    geometry_source: Literal["osrm", "schematic"] = "osrm"
    stop_coordinates: list[tuple[float, float]] = Field(default_factory=list)
    distance_km: float | None = Field(default=None, ge=0)
    estimated_demand_per_hour: int = Field(default=0, ge=0)
    road_alternative_index: int = Field(default=0, ge=0, le=3)
    departure_at: time
    arrival_at: time
    travel_minutes: int
    waiting_minutes: int
    transfer_count: int
    crowding_percent: int = Field(ge=0, le=100)
    seat_available: bool
    score: float = Field(ge=0, le=1)
    explanations: list[str]


class JourneyResponse(StrictModel):
    journeys: list[JourneyOption]
    normalized_weights: dict[str, float]
    data_notice: str
    availability_message: str | None = None


class RoadJourneyPath(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    waypoints: list[str] = Field(min_length=2, max_length=5)
    include_alternatives: bool = False
    alternative_index: int = Field(default=0, ge=0, le=3)
    stop_coordinates: list[tuple[float, float]] | None = None


class RoadAlternativeRequest(StrictModel):
    journeys: list[RoadJourneyPath] = Field(min_length=1, max_length=12)


class RoadAlternative(StrictModel):
    id: str
    distance_km: float = Field(ge=0)
    road_duration_minutes: int = Field(ge=0)
    geometry: list[tuple[float, float]]
    geometry_source: Literal["osrm", "schematic"] = "osrm"
    stop_names: list[str] = Field(default_factory=list)
    stop_coordinates: list[tuple[float, float]] = Field(default_factory=list)


class RoadAlternativeResponse(StrictModel):
    alternatives: list[RoadAlternative]
    data_notice: str


TimePeriod = Literal["all_day", "morning", "midday", "evening"]


class DemandRequest(StrictModel):
    route_id: str | None = None
    travel_date: date
    period: TimePeriod = "all_day"


class CrowdPredictionRequest(StrictModel):
    route: str | None = Field(default=None, max_length=120)
    stop_id: str | None = Field(default=None, max_length=120)
    route_id: str | None = Field(default=None, max_length=120)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    event_time: datetime


class CrowdPredictionResponse(StrictModel):
    predicted_class: str
    class_probabilities: dict[str, float]
    experimental: Literal[True] = True
    data_notice: str


class DemandPoint(StrictModel):
    route_id: str
    route_name: str
    hour: str
    estimated_passengers: int = Field(ge=0)
    passengers_per_departure: float = Field(ge=0)
    crowding_percent: int = Field(ge=0, le=100)
    capacity_utilization_percent: float = Field(ge=0)
    waiting_minutes: float = Field(ge=0)


class DemandResponse(StrictModel):
    travel_date: date
    period: TimePeriod
    estimates: list[DemandPoint]
    data_notice: str


class RouteDemandSummary(StrictModel):
    route_id: str
    route_name: str
    estimated_passengers: int = Field(ge=0)
    peak_crowding_percent: int = Field(ge=0, le=100)
    capacity_utilization_percent: float = Field(ge=0)
    average_waiting_minutes: float = Field(ge=0)


class HourlySummary(StrictModel):
    hour: str
    estimated_passengers: int = Field(ge=0)
    crowding_percent: int = Field(ge=0, le=100)
    capacity_utilization_percent: float = Field(ge=0)
    waiting_minutes: float = Field(ge=0)


class PeakHour(StrictModel):
    hour: str
    estimated_passengers: int = Field(ge=0)


class AnalyticsSummaryResponse(StrictModel):
    route_id: str | None
    location: str | None
    period: TimePeriod
    routes: list[RouteDemandSummary]
    hourly: list[HourlySummary]
    peak_hours: list[PeakHour]
    data_notice: str


class OptimizationRequest(StrictModel):
    route_id: str
    proposed_headway_minutes: int = Field(ge=3, le=60)
    travel_date: date
    period: TimePeriod = "all_day"
    vehicle_capacity: int | None = Field(default=None, ge=10, le=300)
    fleet_available: int | None = Field(default=None, ge=1, le=500)


class ServiceScenario(StrictModel):
    headway_minutes: int = Field(gt=0)
    average_waiting_minutes: float = Field(ge=0)
    passengers_per_departure: float = Field(ge=0)
    load_factor_percent: float = Field(ge=0)
    required_vehicles: int = Field(ge=1)
    capacity_feasible: bool
    fleet_feasible: bool


class OptimizationResponse(StrictModel):
    route_id: str
    route_name: str
    period: TimePeriod
    vehicle_capacity: int
    fleet_available: int | None
    estimated_demand_per_hour: float = Field(ge=0)
    current: ServiceScenario
    proposed: ServiceScenario
    target_headway_minutes: int = Field(gt=0)
    feasible: bool
    recommendation: str
    data_notice: str
