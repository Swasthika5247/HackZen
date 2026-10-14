# ComfortRoute

ComfortRoute is a local prototype for intelligent public transport demand and route optimization. Demand, crowding, seat availability, and waiting times are deterministic simulations, not live transport information.

## Project structure

```text
backend/
  app/
    main.py              FastAPI application and versioned routes
    schemas.py           Shared API request and response contracts
    data.py              Structured sample route and stop data
    services/            Journey, demand, and optimization calculations
  tests/                 Pytest API and calculation tests
frontend/
  src/
    api/                  Centralized typed Fetch API client
    components/           Shared interface components
    sections/              Planner, analytics, methodology
    App.tsx
```

## Run locally

Prerequisites: Python 3.10+ and Node.js 20+.

Backend, from the repository root:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
python -m uvicorn app.main:app --app-dir backend --reload
```

Frontend, in a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal (normally `http://localhost:5173`). FastAPI docs are at `http://localhost:8000/docs`.

Run backend tests from the repository root after activating the virtual environment:

```powershell
python -m pytest backend\tests -q
```

## API

The local API is rooted at `http://localhost:8000` and enables CORS for the Vite development origin.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Process health check |
| `GET` | `/api/v1/routes` | Structured sample routes and stops |
| `GET` | `/api/v1/locations` | Configured Bay Area demo stops plus curated searchable locality suggestions |
| `POST` | `/api/v1/journeys/recommend` | Validate preferences and rank eligible journeys |
| `POST` | `/api/v1/roads/alternatives` | Return OSRM road geometry and distance references for ranked journey waypoints |
| `POST` | `/api/v1/predictions/demand` | Return deterministic simulated demand by route/time |
| `GET` | `/api/v1/analytics/summary` | Demand, peak, utilization, and waiting-time summaries |
| `POST` | `/api/v1/optimization/scenarios` | Compare service headway scenarios under capacity constraints |

Every endpoint uses Pydantic request/response models. The frontend communicates through `src/api/client.ts`; no paid or live-data API is used.

Journey map previews geocode configured Bay Area locations through OpenStreetMap Nominatim and request road geometry from OSRM. Map distance is a road-network reference through the journey waypoints, not transit distance or a live-traffic estimate; transit duration and arrival remain the journey planner's estimates.

### Example requests

Recommend journeys (preferences are relative weights from `0` to `1`; the backend normalizes them):

```json
{
  "origin": "SFO Airport",
  "destination": "Oakland 12th Street",
  "travel_date": "2026-10-20",
  "departure_time": "08:00",
  "arrival_deadline": "09:00",
  "preferences": {
    "less_crowding": 0.3,
    "shorter_wait": 0.2,
    "faster_travel": 0.3,
    "seat_availability": 0.2
  }
}
```

Predict demand: `POST /api/v1/predictions/demand` with `{"route_id":"DEMO-SFO-OAK","travel_date":"2026-10-20","period":"morning"}`. Use `route_id: null` or omit it for all sample routes; periods are `all_day`, `morning`, `midday`, and `evening`.

Filter analytics with `GET /api/v1/analytics/summary?route_id=DEMO-SFO-OAK&location=Embarcadero&period=evening`; `route_id` and `location` are validated against the configured route/stop list and combined as an intersection. Omit either filter to include all configured routes or locations. A curated locality with no configured demo corridor returns an explanatory validation error rather than unrelated route data.

Compare a schedule with `POST /api/v1/optimization/scenarios` and `{"route_id":"DEMO-SFO-OAK","proposed_headway_minutes":8,"travel_date":"2026-10-20","period":"morning","vehicle_capacity":60,"fleet_available":8}`. Capacity and fleet values are optional overrides; if no fleet is provided, only vehicle capacity is constrained.

Journey origins and destinations use the Bay Area suggestions returned by `/api/v1/locations` and are verified inside the supported California boundary by OpenStreetMap. Invalid request fields and preference/capacity ranges return `422`. An unknown route returns `404`. The journey score is the weighted sum of normalized relative crowding, wait, travel-time, and seat-availability criteria; arrival deadlines filter alternatives before ranking.

## Data and methodology

No California GTFS feed is bundled or configured in this checkout; the public catalog requires authenticated API access. The included stops and route patterns are simulated San Francisco Bay Area demo data, never verified operating bus services. When fewer than two configured demo options match, the planner requests up to three OSRM road alternatives inside the supported Bay Area and derives simulated bus schedules from them. The fallback assumes headways of 10, 13, and 16 minutes, expected wait of half a headway, and in-vehicle time of `ceil(OSRM road minutes * 1.55 + 5)`. Demand uses a stable SHA-256 route seed and time-of-day factor shared with the analytics estimator; crowding and likely-seat status derive from estimated passengers per departure against demo capacity. If OSRM has no route but the endpoints geocode, the map shows a dashed schematic between those verified coordinates with a Haversine-based distance estimate. OSRM road paths are not transit alignments or live-traffic predictions, and simulated buses, crowding, demand, and seat estimates are not actual service or passenger data.