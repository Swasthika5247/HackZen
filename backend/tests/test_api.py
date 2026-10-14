from fastapi.testclient import TestClient
from datetime import datetime, time, timedelta
import pytest

from app.main import app
from app.data import CALIFORNIA_LOCALITIES, ROUTES
from app.schemas import RoadAlternative, RoadAlternativeResponse
from app.services import road_routes
from app.services import crowd_model

client = TestClient(app)


def test_crowd_model_loads_and_metadata_matches_artifact() -> None:
    assert crowd_model.MODEL is not None, crowd_model.MODEL_LOAD_ERROR
    assert crowd_model.MODEL.feature_names_ == crowd_model.EXPECTED_FEATURES
    assert set(crowd_model.MODEL.classes_) == set(crowd_model.METADATA["classes"])


def test_crowd_prediction_returns_model_classes_and_probabilities() -> None:
    response = client.post("/api/v1/predictions/crowd", json={
        "route": "1T",
        "stop_id": "12345",
        "route_id": "1T",
        "latitude": 37.8,
        "longitude": -122.2,
        "event_time": "2019-02-11T08:30:00",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["predicted_class"] in crowd_model.MODEL.classes_
    assert set(body["class_probabilities"]) == set(crowd_model.MODEL.classes_)
    assert sum(body["class_probabilities"].values()) == pytest.approx(1)
    assert body["experimental"] is True
    assert "54.73%" in body["data_notice"]


def test_crowd_prediction_is_registered_in_swagger_schema() -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert "/api/v1/predictions/crowd" in spec["paths"]
    assert client.get("/docs").status_code == 200


def test_health_reports_ready_service() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "comfortroute-api"}


def test_routes_return_validated_sample_records() -> None:
    response = client.get("/api/v1/routes")

    assert response.status_code == 200
    body = response.json()
    assert len(body["routes"]) == 4
    assert body["routes"][0]["origin"] == "SFO Airport"
    assert body["routes"][0]["id"] == "DEMO-SFO-OAK"
    assert "SFO Airport - Oakland" in body["routes"][0]["name"]
    assert "demo corridor" in body["routes"][0]["name"].lower()
    assert all(stop in CALIFORNIA_LOCALITIES for route in body["routes"] for stop in route["stops"])
    assert "simulated" in body["data_notice"].lower()


def test_locations_catalog_includes_curated_localities_and_demo_stops() -> None:
    response = client.get("/api/v1/locations")

    assert response.status_code == 200
    locations = set(response.json()["locations"])
    assert {"San Francisco", "Oakland", "Berkeley", "Daly City", "San Mateo", "Redwood City", "Palo Alto", "San Jose"} <= locations
    assert {stop for route in ROUTES for stop in route.stops} <= locations


def test_location_search_uses_california_bounded_geocoder(monkeypatch) -> None:
    class MockResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"name": "San Francisco", "lat": "37.7749", "lon": "-122.4194"},
                {"name": "Las Vegas", "lat": "36.1699", "lon": "-115.1398", "address": {"state": "Nevada"}},
            ]

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            assert params["countrycodes"] == "us"
            assert params["bounded"] == 1
            assert params["viewbox"] == "-124.5,42.1,-114.1,32.4"
            return MockResponse()

    monkeypatch.setattr(road_routes.httpx, "AsyncClient", lambda **kwargs: MockClient())
    response = client.get("/api/v1/locations/search?q=San%20Francisco")

    assert response.status_code == 200
    assert response.json()["locations"] == [{
        "name": "San Francisco", "latitude": 37.7749, "longitude": -122.4194,
    }]


def test_journey_recommendations_rank_options_and_normalize_weights() -> None:
    response = client.post("/api/v1/journeys/recommend", json={
        "origin": "SFO Airport",
        "destination": "San Bruno",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
        "preferences": {"faster_travel": 0.8, "less_crowding": 0.2},
    })

    assert response.status_code == 200
    body = response.json()
    assert body["journeys"]
    assert body["journeys"][0]["waypoints"] == ["SFO Airport", "San Bruno"]
    assert body["journeys"][0]["score"] >= body["journeys"][-1]["score"]
    assert sum(body["normalized_weights"].values()) == pytest.approx(1)
    assert set(body["normalized_weights"]) == {"less_crowding", "shorter_wait", "faster_travel", "seat_availability"}
    assert len(body["journeys"]) == 1
    assert body["journeys"][0]["data_source"] == "simulated"
    assert body["journeys"][0]["distance_km"] is None
    assert "simulated" in body["data_notice"].lower()


def test_journey_search_returns_direct_and_transfer_options() -> None:
    response = client.post("/api/v1/journeys/recommend", json={
        "origin": "SFO Airport",
        "destination": "Oakland 12th Street",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
    })

    assert response.status_code == 200
    journeys = response.json()["journeys"]
    assert any(option["transfer_count"] == 0 for option in journeys)
    assert any(option["transfer_count"] == 1 and option["transfer_stops"] for option in journeys)
    assert all(option["data_source"] == "simulated" for option in journeys)


def test_journey_search_returns_labeled_fallback_for_unlisted_places() -> None:
    response = client.post("/api/v1/journeys/recommend", json={
        "origin": "San Francisco",
        "destination": "San Jose",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
        "origin_coordinates": [-122.4194, 37.7749],
        "destination_coordinates": [-121.8863, 37.3382],
    })

    assert response.status_code == 200
    body = response.json()
    assert len(body["journeys"]) == 1
    fallback = body["journeys"][0]
    assert fallback["journey_kind"] == "simulated_fallback"
    assert fallback["route_names"] == ["Simulated fallback estimate"]
    assert fallback["service_id"].startswith("CR-")
    assert fallback["service_name"] == "Regional Connector"
    assert fallback["service_type"] == "simulated"
    assert fallback["geometry_source"] == "schematic"
    assert any("Rank 1 of 1" in line for line in fallback["explanations"])
    assert "not a verified transit itinerary" in body["availability_message"]

    refreshed_response = client.post("/api/v1/journeys/recommend", json={
        "origin": "San Francisco",
        "destination": "San Jose",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
        "origin_coordinates": [-122.4194, 37.7749],
        "destination_coordinates": [-121.8863, 37.3382],
    })
    assert refreshed_response.json()["journeys"][0]["service_id"] == fallback["service_id"]


def test_fallback_respects_arrival_deadline() -> None:
    response = client.post("/api/v1/journeys/recommend", json={
        "origin": "San Francisco",
        "destination": "San Jose",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
        "arrival_deadline": "09:00",
        "origin_coordinates": [-122.4194, 37.7749],
        "destination_coordinates": [-121.8863, 37.3382],
    })

    assert response.status_code == 200
    assert response.json()["journeys"] == []
    assert "arrival deadline" in response.json()["availability_message"]


def test_bay_area_stops_only_return_matching_configured_demo_patterns() -> None:
    response = client.post("/api/v1/journeys/recommend", json={
        "origin": "Berkeley",
        "destination": "Richmond",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
    })

    assert response.status_code == 200
    journeys = response.json()["journeys"]
    assert journeys
    assert all(option["waypoints"] == ["Berkeley", "Richmond"] for option in journeys)
    assert all(option["data_source"] == "simulated" for option in journeys)
    assert all(option["route_ids"] == ["DEMO-OAK-RIC"] for option in journeys)
    assert all(option["service_id"] == "CR-OAK-RIC" for option in journeys)
    assert all(option["service_name"] == "East Bay Link" for option in journeys)
    assert all(option["service_type"] == "simulated" for option in journeys)


def test_journey_eta_and_demand_match_shared_route_estimates() -> None:
    response = client.post("/api/v1/journeys/recommend", json={
        "origin": "SFO Airport",
        "destination": "San Bruno",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
    })
    demand = client.post("/api/v1/predictions/demand", json={
        "route_id": ROUTES[0].id,
        "travel_date": "2026-10-20",
        "period": "all_day",
    })

    assert response.status_code == 200
    assert demand.status_code == 200
    option = next(item for item in response.json()["journeys"] if item["route_ids"] == [ROUTES[0].id])
    eta = datetime.combine(datetime.fromisoformat("2026-10-20").date(), time.fromisoformat(option["arrival_at"]))
    requested_departure = datetime.fromisoformat("2026-10-20T08:00:00")
    assert eta == requested_departure + timedelta(minutes=option["waiting_minutes"] + option["travel_minutes"])
    matching_demand = next(point for point in demand.json()["estimates"] if point["hour"] == "08:00")
    assert option["estimated_demand_per_hour"] == matching_demand["estimated_passengers"]


def test_journey_recommendations_exclude_arrival_deadlines() -> None:
    deadline_limited = client.post("/api/v1/journeys/recommend", json={
        "origin": "SFO Airport",
        "destination": "San Bruno",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
        "arrival_deadline": "08:10",
    })

    assert deadline_limited.status_code == 200
    assert deadline_limited.json()["journeys"] == []
    assert "arrival deadline" in deadline_limited.json()["availability_message"]


def test_journey_request_rejects_same_stops_and_invalid_weights() -> None:
    same_stop = client.post("/api/v1/journeys/recommend", json={
        "origin": "SFO Airport",
        "destination": "SFO Airport",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
    })
    invalid_weight = client.post("/api/v1/journeys/recommend", json={
        "origin": "SFO Airport",
        "destination": "San Bruno",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
        "preferences": {"faster_travel": 2},
    })

    assert same_stop.status_code == 422
    assert invalid_weight.status_code == 422

    removed_transfer_limit = client.post("/api/v1/journeys/recommend", json={
        "origin": "SFO Airport",
        "destination": "San Bruno",
        "travel_date": "2026-10-20",
        "departure_time": "08:00",
        "max_transfers": 0,
    })
    assert removed_transfer_limit.status_code == 422


def test_demand_predictions_are_deterministic_and_filterable() -> None:
    payload = {"route_id": ROUTES[0].id, "travel_date": "2026-10-20", "period": "morning"}
    first = client.post("/api/v1/predictions/demand", json=payload)
    second = client.post("/api/v1/predictions/demand", json=payload)

    assert first.status_code == 200
    assert first.json() == second.json()
    assert len(first.json()["estimates"]) == 5
    assert {point["route_id"] for point in first.json()["estimates"]} == {ROUTES[0].id}


def test_analytics_summary_filters_routes_and_period() -> None:
    response = client.get(f"/api/v1/analytics/summary?route_id={ROUTES[2].id}&period=evening")

    assert response.status_code == 200
    body = response.json()
    assert [route["route_id"] for route in body["routes"]] == [ROUTES[2].id]
    assert len(body["hourly"]) == 6
    assert body["peak_hours"]
    assert "simulated" in body["data_notice"].lower()


def test_all_route_analytics_only_returns_configured_routes() -> None:
    configured_ids = {route.id for route in ROUTES}
    response = client.get("/api/v1/analytics/summary?period=morning")

    assert response.status_code == 200
    assert {route["route_id"] for route in response.json()["routes"]} == configured_ids


def test_analytics_rejects_unconfigured_route() -> None:
    response = client.get("/api/v1/analytics/summary?route_id=unsupported&period=all_day")

    assert response.status_code == 404


def test_analytics_location_filter_uses_configured_route_stops() -> None:
    response = client.get("/api/v1/analytics/summary?location=Oakland%2012th%20Street&period=morning")

    assert response.status_code == 200
    body = response.json()
    assert body["location"] == "Oakland 12th Street"
    assert {route["route_id"] for route in body["routes"]} == {ROUTES[0].id, ROUTES[1].id, ROUTES[3].id}
    assert len(body["hourly"]) == 5


def test_analytics_location_filter_rejects_unknown_or_unserved_stops() -> None:
    unknown_stop = client.get("/api/v1/analytics/summary?location=Somewhere%20Else")
    unserved_stop = client.get("/api/v1/analytics/summary?location=Oakland%20Airport")
    wrong_route = client.get(f"/api/v1/analytics/summary?route_id={ROUTES[0].id}&location=Richmond")

    assert unknown_stop.status_code == 404
    assert unserved_stop.status_code == 422
    assert wrong_route.status_code == 422


def test_road_alternatives_use_osrm_geometry_and_are_california_only(monkeypatch) -> None:
    class MockResponse:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            if url == road_routes.NOMINATIM_URL:
                locality = params["q"].split(",")[0]
                if locality == "Outside California":
                    return MockResponse([{"lon": "-115.14", "lat": "36.17", "address": {"state": "Nevada"}}])
                coordinates = {
                    "SFO Airport": ("-122.38", "37.62"),
                    "Oakland 12th Street": ("-122.27", "37.80"),
                }
                longitude, latitude = coordinates.get(locality, ("-122.4", "37.7"))
                return MockResponse([{"lon": longitude, "lat": latitude, "display_name": f"{locality}, California, United States"}])
            return MockResponse({"code": "Ok", "routes": [
                {"distance": 1200, "duration": 240, "geometry": {"coordinates": [[-122.38, 37.62], [-122.27, 37.80]]}},
            ]})

    monkeypatch.setattr(road_routes.httpx, "AsyncClient", lambda **kwargs: MockClient())
    monkeypatch.setattr(road_routes, "GEOCODE_CACHE", {})
    monkeypatch.setattr(road_routes, "last_geocode_request", 0.0)
    response = client.post("/api/v1/roads/alternatives", json={
        "journeys": [
            {"id": "TEST-1", "waypoints": ["SFO Airport", "Oakland 12th Street"]},
            {"id": "TEST-2", "waypoints": ["SFO Airport", "Oakland 12th Street"]},
        ],
    })

    assert response.status_code == 200
    body = response.json()
    assert len(body["alternatives"]) == 2
    assert [alternative["id"] for alternative in body["alternatives"]] == ["TEST-1", "TEST-2"]
    assert body["alternatives"][0]["distance_km"] == 1.2
    assert body["alternatives"][0]["road_duration_minutes"] == 4
    assert body["alternatives"][0]["geometry"]
    assert body["alternatives"][0]["stop_names"] == ["SFO Airport", "Oakland 12th Street"]
    assert len(body["alternatives"][0]["stop_coordinates"]) == 2
    assert "transit distance" in body["data_notice"]
    assert body["alternatives"][0]["geometry_source"] == "osrm"

    unsupported = client.post("/api/v1/roads/alternatives", json={
        "journeys": [{"id": "outside", "waypoints": ["Outside California", "SFO Airport"]}],
    })
    assert unsupported.status_code == 422


def test_road_alternatives_geocode_unlisted_california_locality(monkeypatch) -> None:
    class MockResponse:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            if url == road_routes.NOMINATIM_URL:
                if params["q"].startswith("Oakland Airport,"):
                    return MockResponse([{"lon": "-122.27", "lat": "37.72", "display_name": "Oakland Airport, California"}])
                return MockResponse([{"lon": "-122.42", "lat": "37.77", "display_name": "San Francisco, California"}])
            return MockResponse({"code": "Ok", "routes": [
                {"distance": 8000, "duration": 600, "geometry": {"coordinates": [[-122.27, 37.72], [-122.42, 37.77]]}},
            ]})

    monkeypatch.setattr(road_routes.httpx, "AsyncClient", lambda **kwargs: MockClient())
    monkeypatch.setattr(road_routes, "GEOCODE_CACHE", {})
    monkeypatch.setattr(road_routes, "ROAD_CACHE", {})
    monkeypatch.setattr(road_routes, "last_geocode_request", 0.0)
    response = client.post("/api/v1/roads/alternatives", json={
        "journeys": [{"id": "SIM-OAK-SF", "waypoints": ["Oakland Airport", "San Francisco"]}],
    })

    assert response.status_code == 200
    assert response.json()["alternatives"][0]["road_duration_minutes"] == 10


def test_road_alternatives_return_no_fake_geometry_when_osrm_has_no_route(monkeypatch) -> None:
    class MockResponse:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            if url == road_routes.NOMINATIM_URL:
                if params["q"].startswith("Oakland Airport,"):
                    return MockResponse([{"lon": "-122.27", "lat": "37.72"}])
                return MockResponse([{"lon": "-122.42", "lat": "37.77"}])
            return MockResponse({"code": "NoRoute", "routes": []})

    monkeypatch.setattr(road_routes.httpx, "AsyncClient", lambda **kwargs: MockClient())
    monkeypatch.setattr(road_routes, "GEOCODE_CACHE", {})
    monkeypatch.setattr(road_routes, "ROAD_CACHE", {})
    monkeypatch.setattr(road_routes, "last_geocode_request", 0.0)
    response = client.post("/api/v1/roads/alternatives", json={
        "journeys": [{"id": "SIM-OAK-SF", "waypoints": ["Oakland Airport", "San Francisco"]}],
    })

    assert response.status_code == 404
    assert response.json()["detail"] == "No road route was found between these California locations."


def test_demand_rejects_unknown_routes_and_periods() -> None:
    unknown_route = client.post("/api/v1/predictions/demand", json={
        "route_id": "R99",
        "travel_date": "2026-10-20",
    })
    invalid_period = client.get("/api/v1/analytics/summary?period=overnight")

    assert unknown_route.status_code == 404
    assert invalid_period.status_code == 422


def test_optimization_compares_wait_load_and_vehicle_requirements() -> None:
    response = client.post("/api/v1/optimization/scenarios", json={
        "route_id": ROUTES[0].id,
        "proposed_headway_minutes": 8,
        "travel_date": "2026-10-20",
        "period": "morning",
        "vehicle_capacity": 60,
        "fleet_available": 8,
    })

    assert response.status_code == 200
    body = response.json()
    assert body["current"]["headway_minutes"] == 10
    assert body["proposed"]["average_waiting_minutes"] == 4
    assert body["proposed"]["required_vehicles"] >= body["current"]["required_vehicles"]
    assert body["proposed"]["passengers_per_departure"] >= 0
    assert "simulated" in body["data_notice"].lower()


def test_optimization_flags_fleet_and_capacity_constraints() -> None:
    response = client.post("/api/v1/optimization/scenarios", json={
        "route_id": ROUTES[0].id,
        "proposed_headway_minutes": 60,
        "travel_date": "2026-10-20",
        "vehicle_capacity": 10,
        "fleet_available": 5,
    })

    assert response.status_code == 200
    proposed = response.json()["proposed"]
    assert proposed["capacity_feasible"] is False
    assert proposed["fleet_feasible"] is True
    assert response.json()["feasible"] is False

    fleet_limited = client.post("/api/v1/optimization/scenarios", json={
        "route_id": ROUTES[0].id,
        "proposed_headway_minutes": 3,
        "travel_date": "2026-10-20",
        "vehicle_capacity": 300,
        "fleet_available": 2,
    })
    fleet_proposal = fleet_limited.json()["proposed"]
    assert fleet_limited.status_code == 200
    assert fleet_proposal["capacity_feasible"] is True
    assert fleet_proposal["fleet_feasible"] is False


def test_optimization_validates_route_and_headway() -> None:
    unknown_route = client.post("/api/v1/optimization/scenarios", json={
        "route_id": "R99",
        "proposed_headway_minutes": 10,
        "travel_date": "2026-10-20",
    })
    invalid_headway = client.post("/api/v1/optimization/scenarios", json={
        "route_id": ROUTES[0].id,
        "proposed_headway_minutes": 1,
        "travel_date": "2026-10-20",
    })

    assert unknown_route.status_code == 404
    assert invalid_headway.status_code == 422
