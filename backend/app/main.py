from datetime import date

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from app.data import CALIFORNIA_LOCALITIES, DATA_NOTICE, ROUTES
from app.schemas import (
    AnalyticsSummaryResponse,
    CrowdPredictionRequest,
    CrowdPredictionResponse,
    DemandRequest,
    DemandResponse,
    HealthResponse,
    JourneyRequest,
    JourneyResponse,
    LocationListResponse,
    LocationSearchResponse,
    OptimizationRequest,
    OptimizationResponse,
    RoadAlternativeRequest,
    RoadAlternativeResponse,
    RouteListResponse,
    TimePeriod,
)
from app.services.demand import analytics_summary, predict_demand
from app.services.crowd_model import predict_crowd
from app.services.journeys import recommend_journeys
from app.services.optimization import optimize_scenario
from app.services.road_routes import road_alternatives, search_california_locations

app = FastAPI(
    title="ComfortRoute API",
    description="Local public transport planning and simulated demand optimization prototype.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="comfortroute-api")


@app.get("/api/v1/routes", response_model=RouteListResponse)
def list_routes() -> RouteListResponse:
    return RouteListResponse(routes=ROUTES, data_notice=DATA_NOTICE)


@app.get("/api/v1/locations", response_model=LocationListResponse)
def list_locations() -> LocationListResponse:
    route_stops = {stop for route in ROUTES for stop in route.stops}
    locations = sorted(route_stops | set(CALIFORNIA_LOCALITIES), key=str.casefold)
    return LocationListResponse(
        locations=locations,
        data_notice="Curated San Francisco Bay Area locality suggestions plus configured demo-route stops. Entered locations are verified within the supported California boundary by OpenStreetMap before routing.",
    )


@app.get("/api/v1/locations/search", response_model=LocationSearchResponse)
async def search_locations(q: str = Query(min_length=2, max_length=100)) -> LocationSearchResponse:
    return await search_california_locations(q)


@app.post("/api/v1/journeys/recommend", response_model=JourneyResponse)
async def journey_recommendations(request: JourneyRequest) -> JourneyResponse:
    return await recommend_journeys(request)



@app.post("/api/v1/roads/alternatives", response_model=RoadAlternativeResponse)
async def road_route_alternatives(request: RoadAlternativeRequest) -> RoadAlternativeResponse:
    return await road_alternatives(request)


@app.post("/api/v1/predictions/demand", response_model=DemandResponse)
def demand_prediction(request: DemandRequest) -> DemandResponse:
    return predict_demand(request)


@app.post("/api/v1/predictions/crowd", response_model=CrowdPredictionResponse)
def crowd_prediction(request: CrowdPredictionRequest) -> CrowdPredictionResponse:
    return predict_crowd(request)


@app.get("/api/v1/analytics/summary", response_model=AnalyticsSummaryResponse)
def demand_analytics(
    route_id: str | None = None,
    location: str | None = Query(default=None, min_length=2, max_length=80),
    period: TimePeriod = "all_day",
    travel_date: date = Query(default_factory=date.today),
) -> AnalyticsSummaryResponse:
    return analytics_summary(route_id, location, period, travel_date)


@app.post("/api/v1/optimization/scenarios", response_model=OptimizationResponse)
def optimization_scenario(request: OptimizationRequest) -> OptimizationResponse:
    return optimize_scenario(request)
