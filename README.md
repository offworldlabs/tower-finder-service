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
| POST | `/api/towers` | Same tower search, enriched with spectrum-analyser measurements. Body: `MeasurementPayload` (see `backend/models/measurements.py`). Only towers the SDR can see are returned — unmatched towers are excluded. Matched towers carry real measured fields (`snr_db`, `score`, `obw_fraction`, `power_db`, `measured=true`). |
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
| `backend/tests/` | pytest suite (176 tests); integration tests require running `capture_fixture.py` first) |
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

## Deployment

CI/CD runs via GitHub Actions (`.github/workflows/ci.yml`):

- **Every PR / push to `main`**: `ruff check`, `ruff format --check`, and
  `pytest -m "not integration"`.
- **Push to `main`**: after tests pass, SSHes to the production droplet,
  hard-resets to `origin/main`, rebuilds the stack, waits for health, then
  smoke-tests `https://tower-finder.retina.fm`.

The service runs as its own Docker Compose stack at `/opt/tower-finder-service`,
exposed at `tower-finder.retina.fm` through a Cloudflare Tunnel (`cloudflared`
sidecar) — no host port is published and the existing `tower-finder` stack is
untouched.

### One-time setup

**1. Deploy SSH key (run locally):**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/tower_finder_service_deploy -C "tfs-deploy" -N ""
ssh root@157.245.214.30 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys" \
  < ~/.ssh/tower_finder_service_deploy.pub
```

Then add GitHub Actions repository secrets (Settings → Secrets and variables → Actions):
- `DEPLOY_HOST` = `157.245.214.30`
- `DEPLOY_SSH_KEY` = contents of `~/.ssh/tower_finder_service_deploy` (the private key)

**2. Cloudflare Tunnel (Zero Trust dashboard):**

- Networks → Tunnels → Create a tunnel (type `cloudflared`); copy the token.
- Add a public hostname: `tower-finder.retina.fm` → service
  `http://tower-finder-service:8000` (this auto-creates the DNS record).

**3. Droplet bootstrap (run on the droplet as root):**

```bash
git clone https://github.com/offworldlabs/tower-finder-service.git /opt/tower-finder-service
cd /opt/tower-finder-service
cp .env.example .env
# Edit .env: set TUNNEL_TOKEN (required) and MAPRAD_API_KEY (optional, non-US only).
docker compose up -d --build
```

After this, every push to `main` redeploys automatically.

### Rollback (manual)

No automated rollback in v1. To roll back, SSH to the droplet and reset to a
known-good commit:

```bash
cd /opt/tower-finder-service
git reset --hard <good-commit-sha>
docker compose up -d --build
```
