# tower-finder-service

FastAPI service that ranks broadcast towers near a node from FCC and Maprad data. Split out from the `Tower-Finder` monorepo with full git history (`git filter-repo`).

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app:app --reload
```

Optional env vars:
- `MAPRAD_API_KEY` — required for non-US queries; US can fall back to FCC only.
- `TOWER_FINDER_RUNTIME_DIR` — where `tower_config.json` is read/written (default `./data/runtime/`). On first start the runtime overlay is seeded from `backend/config/tower_config.json`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/towers?lat&lon&altitude&radius_km&limit&source` | Ranked towers near (lat, lon) using model-based scoring (EIRP, FSPL, distance class). |
| POST | `/api/towers` | Same tower search, enriched with spectrum-analyser measurements. Body: `MeasurementPayload` (see `backend/models/measurements.py`). Matched towers carry real measured fields (`snr_db`, `score`, `obw_fraction`, `power_db`, `measured=true`); unmatched towers carry nulls. |
| GET | `/api/config` | Current ranking config (bands, distance classes, defaults). |
| PUT | `/api/config` | Replace ranking config; sanity-capped at 1 MB. No auth — gate this behind a reverse proxy if it's reachable externally. |

## Layout

| Path | What's there |
| --- | --- |
| `app.py` | FastAPI entry point |
| `backend/routes/towers.py` | HTTP routes |
| `backend/services/tower_ranking.py` | Ranking algorithm + config loader |
| `backend/clients/fcc.py` | FCC TV/FM Query CGI client |
| `backend/clients/maprad.py` | Maprad.io broadcast-systems client |
| `backend/config/tower_config.json` | Default ranking config (image-shipped) |
| `backend/tests/` | pytest suite (141 tests) |
| `frontend/` | Reference React/Playwright snippets from the parent monorepo's UI — not part of the service runtime |
| `pyproject.toml` | Package + tooling config |

## Tests

```bash
pytest -q
```

## Origin

Extracted from `offworldlabs/Tower-Finder` on 2026-05-20 with `git filter-repo --path ...` over the 11 tower-finder paths, then made standalone:
- `tower_ranking.py` no longer imports `core.runtime_config`; the runtime overlay is inlined.
- `routes/towers.py` trimmed to tower endpoints only (dropped `/api/health`, `/api/elevation`, and the `core.users.require_admin` auth dep).
- Tests rewired to a local `app` entry point.

The parent repo still contains the same code for now; deduplication can come later.
