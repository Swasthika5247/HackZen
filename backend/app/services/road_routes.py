import asyncio
from time import monotonic

import httpx
from fastapi import HTTPException

from app.data import CALIFORNIA_LOCALITIES, ROUTES
from app.schemas import LocationSearchResponse, LocationSuggestion, RoadAlternative, RoadAlternativeRequest, RoadAlternativeResponse

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
ROAD_DATA_NOTICE = (
    "OSRM driving routes are shown where available. Driving distance and duration are not transit distance, transit alignment, or live traffic. If OSRM has no route, no geometry is displayed."
)
STOP_NAMES = {name.casefold(): name for name in CALIFORNIA_LOCALITIES}
STOP_NAMES.update({stop.casefold(): stop for route in ROUTES for stop in route.stops})
GEOCODE_CACHE: dict[str, tuple[float, float]] = {}
ROAD_CACHE: dict[tuple[tuple[tuple[float, float], ...], bool], list[dict]] = {}
GEOCODE_LOCK = asyncio.Lock()
last_geocode_request = 0.0
CALIFORNIA_BOUNDS = (32.4, -124.5, 42.1, -114.1)


async def search_california_locations(query: str) -> LocationSearchResponse:
    query = query.strip()
    if len(query) < 2:
        return LocationSearchResponse(locations=[])
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "ComfortRoute-local-demo/0.1 (location search)"}) as client:
            response = await client.get(NOMINATIM_URL, params={
                "q": f"{query}, California, United States", "format": "jsonv2", "limit": 20,
                "countrycodes": "us", "viewbox": "-124.5,42.1,-114.1,32.4", "bounded": 1, "addressdetails": 1,
            })
            response.raise_for_status()
            records = response.json()
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail="California location search is temporarily unavailable. Please try again.") from error
    south, west, north, east = CALIFORNIA_BOUNDS
    locations = []
    for record in records:
        latitude, longitude = float(record["lat"]), float(record["lon"])
        state = record.get("address", {}).get("state")
        if south <= latitude <= north and west <= longitude <= east and (not state or state.casefold() == "california"):
            locations.append(LocationSuggestion(
                name=record.get("name") or record.get("display_name", "").split(",")[0],
                latitude=latitude, longitude=longitude,
            ))
    return LocationSearchResponse(locations=locations)


async def _geocode(stop: str, client: httpx.AsyncClient) -> tuple[float, float]:
    global last_geocode_request
    query_name = stop.strip()
    if not query_name or len(query_name) > 120:
        raise HTTPException(status_code=422, detail="Enter a valid California location.")
    canonical_stop = STOP_NAMES.get(query_name.casefold(), query_name)
    cache_key = canonical_stop.casefold()
    if cache_key in GEOCODE_CACHE:
        return GEOCODE_CACHE[cache_key]

    async with GEOCODE_LOCK:
        if cache_key in GEOCODE_CACHE:
            return GEOCODE_CACHE[cache_key]
        wait_seconds = 1.1 - (monotonic() - last_geocode_request)
        if last_geocode_request and wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        try:
            response = await client.get(NOMINATIM_URL, params={
                "q": f"{canonical_stop}, California, United States",
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "us",
                "viewbox": "-124.5,42.1,-114.1,32.4",
                "bounded": 1,
                "addressdetails": 1,
            })
            response.raise_for_status()
            results = response.json()
        except httpx.HTTPError as error:
            raise HTTPException(status_code=502, detail="OpenStreetMap locality lookup is temporarily unavailable.") from error
        finally:
            last_geocode_request = monotonic()

        if not results:
            raise HTTPException(status_code=422, detail=f"Could not verify '{canonical_stop}' within California. Choose a California location from the suggestions.")
        latitude = float(results[0]["lat"])
        longitude = float(results[0]["lon"])
        south, west, north, east = CALIFORNIA_BOUNDS
        state = results[0].get("address", {}).get("state")
        if not (south <= latitude <= north and west <= longitude <= east) or (state and state.casefold() != "california"):
            raise HTTPException(status_code=422, detail=f"'{canonical_stop}' is outside California.")
        coordinates = (longitude, latitude)
        GEOCODE_CACHE[cache_key] = coordinates
        return coordinates


async def road_alternatives(request: RoadAlternativeRequest) -> RoadAlternativeResponse:
    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": "ComfortRoute-local-demo/0.1 (OSM road preview)"},
    ) as client:
        alternatives = []
        for journey in request.journeys:
            if journey.stop_coordinates:
                coordinates = [(float(lon), float(lat)) for lon, lat in journey.stop_coordinates]
                south, west, north, east = CALIFORNIA_BOUNDS
                if len(coordinates) != len(journey.waypoints) or any(not (west <= lon <= east and south <= lat <= north) for lon, lat in coordinates):
                    raise HTTPException(status_code=422, detail="Route coordinates must match the stops and lie within California.")
            else:
                coordinates = [await _geocode(stop, client) for stop in journey.waypoints]
            wants_alternatives = journey.include_alternatives or journey.alternative_index > 0
            cache_key = (tuple(coordinates), wants_alternatives)
            cached_routes = ROAD_CACHE.get(cache_key)
            if cached_routes is None:
                coordinate_path = ";".join(f"{longitude},{latitude}" for longitude, latitude in coordinates)
                result = {}
                try:
                    response = await client.get(
                        f"{OSRM_URL}/{coordinate_path}",
                        params={
                            "alternatives": 3 if wants_alternatives else "false",
                            "overview": "full",
                            "geometries": "geojson",
                            "steps": "false",
                        },
                    )
                    response.raise_for_status()
                    result = response.json()
                except httpx.HTTPError as error:
                    result = {"code": "Unavailable"}

                if result.get("code") == "Ok" and result.get("routes"):
                    cached_routes = []
                    unique = set()
                    for route in result["routes"]:
                        geometry = [
                            (float(longitude), float(latitude))
                            for longitude, latitude in route.get("geometry", {}).get("coordinates", [])
                        ]
                        distance_km = round(float(route["distance"]) / 1000, 1)
                        key = (distance_km, tuple((round(lon, 4), round(lat, 4)) for lon, lat in geometry))
                        if key in unique:
                            continue
                        unique.add(key)
                        cached_routes.append({
                            "distance_km": distance_km,
                            "road_duration_minutes": round(float(route["duration"]) / 60),
                            "geometry": geometry,
                            "geometry_source": "osrm",
                            "stop_names": journey.waypoints,
                            "stop_coordinates": coordinates,
                        })
                else:
                    cached_routes = []
                ROAD_CACHE[cache_key] = cached_routes

            if journey.alternative_index > 0:
                route_index = journey.alternative_index - 1
                if route_index < len(cached_routes):
                    route = cached_routes[route_index]
                    alternatives.append(RoadAlternative(
                        id=journey.id,
                        distance_km=route["distance_km"],
                        road_duration_minutes=route["road_duration_minutes"],
                        geometry=route["geometry"],
                        geometry_source=route["geometry_source"],
                        stop_names=route["stop_names"],
                        stop_coordinates=route["stop_coordinates"],
                    ))
            else:
                routes = cached_routes if journey.include_alternatives else cached_routes[:1]
                for index, route in enumerate(routes, start=1):
                    alternatives.append(RoadAlternative(
                        id=f"{journey.id}-road-{index}" if journey.include_alternatives else journey.id,
                        distance_km=route["distance_km"],
                        road_duration_minutes=route["road_duration_minutes"],
                        geometry=route["geometry"],
                        geometry_source=route["geometry_source"],
                        stop_names=route["stop_names"],
                        stop_coordinates=route["stop_coordinates"],
                    ))

    if not alternatives:
        raise HTTPException(status_code=404, detail="No road route was found between these California locations.")

    return RoadAlternativeResponse(alternatives=alternatives, data_notice=ROAD_DATA_NOTICE)
