# CI/CD for tower-finder-service — Design

**Date:** 2026-06-03
**Status:** Approved (brainstorming complete; ready for implementation plan)

## Goal

Set up continuous integration and continuous deployment for the standalone
`tower-finder-service` FastAPI app, deploying it to the **same production
droplet** that runs `tower-finder` — without disturbing that existing stack.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Public exposure | **Cloudflare Tunnel** (`cloudflared` sidecar) |
| Public hostname | **`tower-finder.retina.fm`** |
| Deploy target | **Production droplet `157.245.214.30`**, dir `/opt/tower-finder-service` |
| Pipeline weight | **Lightweight**: test on PR/push; deploy + smoke on push to `main` |
| Deploy auth | **New dedicated SSH key** (not reused from tower-finder) |
| Health check | **Hybrid**: cheap `/api/health` liveness; smoke test hits real `/api/towers` |

## Context / constraints discovered

- The `tower-finder` container binds host ports **80 and 443 directly** and runs
  nginx *inside itself*, terminating TLS with Cloudflare origin certs at
  `/etc/ssl/cloudflare/`. A second container therefore **cannot bind 443**.
- DNS is **Cloudflare-proxied** (orange cloud); A-records point at the droplet.
- The droplet's UFW firewall allows only 22/80/443 inbound.
- `tower-finder-service` is **stateless** (FCC/Maprad lookups + ranking), making
  it a clean candidate for a fully independent stack.
- The service currently has **no `/api/health`** (deliberately dropped in the
  monorepo split). The `frontend/` directory is **not** part of the runtime.
- Integration tests are marked `integration` and require running
  `capture_fixture.py` first, so they are excluded in CI.

The Cloudflare Tunnel approach was chosen precisely because it sidesteps the
443-is-taken constraint: `cloudflared` dials *outbound* to Cloudflare, so no
inbound port, no UFW change, and no contact with tower-finder's nginx.

## Runtime architecture

Own Docker Compose stack at `/opt/tower-finder-service`, independent of
`tower-finder`. Two containers:

- **`tower-finder-service`** — `uvicorn app:app --host 0.0.0.0 --port 8000`.
  Not published to any host port; reachable only on the internal compose
  network. Named volume for `data/runtime/`. Healthcheck hits
  `http://localhost:8000/api/health`.
- **`cloudflared`** — official `cloudflare/cloudflared` image running
  `tunnel run` with `TUNNEL_TOKEN`. Forwards `tower-finder.retina.fm` →
  `http://tower-finder-service:8000`.

**Request data flow:**
`client → Cloudflare edge (TLS) → outbound tunnel → cloudflared → tower-finder-service:8000 → FCC/Maprad upstreams`.

## Files added to the repo

| File | Purpose |
|---|---|
| `Dockerfile` | `python:3.12-slim`, `pip install .`, run uvicorn on 8000, non-root user. |
| `.dockerignore` | Exclude `.venv`, caches, `frontend/`, tests, `data/runtime/`. |
| `docker-compose.yml` | `tower-finder-service` + `cloudflared`; named volume; healthcheck; `env_file: .env`. |
| `.env.example` | Documents `TUNNEL_TOKEN`, `MAPRAD_API_KEY` (optional; US works FCC-only), `TOWER_FINDER_RUNTIME_DIR`. |
| `.github/workflows/ci.yml` | The pipeline (below). |
| `deploy/smoke-test.sh` | Post-deploy curl checks against the public URL. |
| `GET /api/health` added to `backend/routes/towers.py` | New health route on the existing router (avoids touching `app.py`, which only includes `routes.towers.router`). |
| `README.md` | Add a "Deployment" section documenting one-time setup. |

## The pipeline (`.github/workflows/ci.yml`)

Triggers: PR to `main`, push to `main`.

- **`test`** (always): `actions/setup-python@v5` (3.12, pip cache) →
  `pip install -e ".[dev]"` → `ruff check .` → `ruff format --check .` →
  `pytest -m "not integration"`.
- **`deploy`** (push to `main` only; `needs: [test]`; concurrency group
  `tf-svc-deploy`): `appleboy/ssh-action` → SSH to droplet →
  `cd /opt/tower-finder-service` → `git fetch origin main && git reset --hard origin/main`
  (hard reset, not `pull` — matches the documented tower-finder deploy failure
  mode) → `docker compose up -d --build` → wait-for-healthy loop against
  `http://localhost:8000/api/health` inside the container.
- **`smoke`** (`needs: [deploy]`): `bash deploy/smoke-test.sh` —
  `curl https://tower-finder.retina.fm/api/health` expect 200, and a real
  `GET /api/towers?lat=34.85&lon=-82.40` (Greenville SC — the location already
  used by the repo's integration fixture) expect 200. Validates the full path
  including the tunnel.

## Health check behavior (hybrid)

- `GET /api/health` → `{"status": "ok"}`, no external calls. Used by the Docker
  healthcheck (~every 30s) and as a cheap liveness probe.
- The once-per-deploy smoke test additionally issues a real `/api/towers`
  request for a known US coordinate and asserts a 200, exercising the FCC path
  end-to-end through the tunnel.

Rationale: liveness must stay cheap (frequent), while deploy validation can
afford one real upstream-touching request.

## Secrets & one-time manual setup (documented in README, not automated)

- **New deploy SSH key**: generate a dedicated `ed25519` keypair; add the public
  key to the droplet's `root` `authorized_keys`; store the private key as GitHub
  secret **`DEPLOY_SSH_KEY`** and **`DEPLOY_HOST=157.245.214.30`**.
- **Droplet bootstrap**: `git clone` the repo to `/opt/tower-finder-service`;
  create `/opt/tower-finder-service/.env` with `TUNNEL_TOKEN=…` and
  `MAPRAD_API_KEY=…` (kept on the droplet, never in CI — same pattern as
  tower-finder's `backend/.env`).
- **Cloudflare**: create a tunnel; add public hostname
  `tower-finder.retina.fm` → `http://tower-finder-service:8000` (auto-creates
  the DNS record); copy the token into the droplet `.env`.

## Error handling & scope

- Failed `ruff`/`pytest` blocks deploy (and can gate merges via branch
  protection — configured outside these files).
- If the post-deploy health loop fails, the `deploy` job exits non-zero and
  surfaces in Actions. **No automated rollback in v1** (tower-finder's rollback
  machinery is heavier than this stateless service warrants); documented as a
  manual `git reset` + redeploy. Easy to add later.
- **Out of scope**: staging mirror, E2E/Playwright, Claude review bots, frontend
  CI.
