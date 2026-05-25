# tower-finder-service

Tower-finding logic split out from the `Tower-Finder` monorepo (full history preserved via `git filter-repo`). Hand-off home for ongoing work on tower selection / node tuning.

## Contents

| Path | Purpose |
| --- | --- |
| `backend/services/tower_ranking.py` | Distance / band / EIRP ranking algorithm |
| `backend/routes/towers.py` | FastAPI endpoints (tower search, config, elevation, health — to be trimmed) |
| `backend/clients/fcc.py` | FCC TV/FM Query CGI client |
| `backend/clients/maprad.py` | Maprad.io broadcast-systems client |
| `backend/config/tower_config.json` | Default ranking config (bands, distance classes, defaults) |
| `backend/tests/test_tower_*.py`, `test_towers_*.py` | Unit + route tests |
| `frontend/src/components/SearchForm.tsx` | Search UI |
| `frontend/e2e/tower-finder.spec.ts` | Playwright e2e |

## Known follow-ups

- `tower_ranking.py` still imports `core.runtime_config` from the parent project — needs to be vendored or replaced for this repo to install standalone.
- `routes/towers.py` bundles tower endpoints with elevation/health/config endpoints — trim down to tower-only when convenient.
- No `pyproject.toml` / build wiring yet — add when packaging is needed.

## Origin

Extracted from `offworldlabs/Tower-Finder` on 2026-05-20 with `git filter-repo --path …` over the 11 paths above; the parent repo still contains the same code for now.
