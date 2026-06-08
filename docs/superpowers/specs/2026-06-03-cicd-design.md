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
| Public exposure | **Reverse-proxy vhost behind tower-finder's existing nginx** (matches `api.`/`dash.` subdomains) |
| Shared networking | **External Docker network `retina-edge`** joined by both stacks; nginx `resolver 127.0.0.11` + variable `proxy_pass` to defer DNS |
| Public hostname | **`tower-finder.retina.fm`** (Cloudflare-proxied A-record → droplet) |
| Deploy target | **Production droplet `157.245.214.30`**, dir `/opt/tower-finder-service` |
| Pipeline weight | **Lightweight**: test on PR/push; deploy + smoke on push to `main` |
| Deploy auth | **New dedicated SSH key** (not reused from tower-finder) |
| Health check | **Hybrid**: cheap `/api/health` liveness; smoke test hits real `/api/towers` |
| TLS | **Reuses tower-finder's `*.retina.fm` Cloudflare Origin cert** at `/etc/ssl/cloudflare` (already covers the subdomain) |

> **Note:** an earlier revision of this spec proposed a Cloudflare Tunnel
> (`cloudflared` sidecar). That was superseded on 2026-06-08 in favour of the
> nginx-vhost approach, to keep the droplet consistent with how every other
> `*.retina.fm` service is exposed. The trade-off accepted: exposing this
> service now requires a change to the production `tower-finder` stack (a vhost
> added to its baked-in nginx config, shipped via that repo's deploy pipeline).

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

The 443-is-taken constraint is resolved not by avoiding tower-finder's nginx but
by *joining* it: tower-finder-service publishes no host port and is reached over
a shared Docker network, exactly as the FastAPI backend behind `api.retina.fm`
is. Only one process binds 443 (tower-finder's nginx), and it fans out to
backends by `server_name`.

## Runtime architecture

Own Docker Compose stack at `/opt/tower-finder-service`. A single container,
attached to the shared external network `retina-edge`:

- **`tower-finder-service`** — `uvicorn app:app --host 0.0.0.0 --port 8000`.
  Not published to any host port; reachable only on `retina-edge` via its
  network alias `tower-finder-service`. Named volume for `data/runtime/`.
  Healthcheck hits `http://localhost:8000/api/health`.

The public edge is provided by the **existing `tower-finder` container's
nginx** (separate repo/stack), which gains a new vhost:

```nginx
server {
    listen 443 ssl;
    server_name tower-finder.retina.fm;
    ssl_certificate     /etc/ssl/cloudflare/cert.pem;
    ssl_certificate_key /etc/ssl/cloudflare/key.pem;
    location / {
        resolver 127.0.0.11 valid=10s;          # Docker embedded DNS
        set $tfs_upstream tower-finder-service;  # variable defers resolution
        proxy_pass http://$tfs_upstream:8000;    # full request URI passed as-is
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

The `resolver` + variable form is deliberate: a literal `proxy_pass` resolves
the upstream name once at nginx startup and **fails to boot** if it's
unresolvable, which would couple tower-finder's whole nginx to this service
being up. The variable defers resolution to request time, so an absent upstream
degrades to a 502 instead.

**Request data flow:**
`client → Cloudflare edge (TLS) → droplet :443 → tower-finder nginx (TLS via Origin cert, routes by server_name) → retina-edge → tower-finder-service:8000 → FCC/Maprad upstreams`.

## Files added to the repo

| File | Purpose |
|---|---|
| `Dockerfile` | `python:3.12-slim`, `pip install .`, run uvicorn on 8000, non-root user. |
| `.dockerignore` | Exclude `.venv`, caches, `frontend/`, tests, `data/runtime/`. |
| `docker-compose.yml` | `tower-finder-service` only; joins external `retina-edge`; named volume; healthcheck; `env_file: .env`. |
| `.env.example` | Documents `MAPRAD_API_KEY` (optional; US works FCC-only), `TOWER_FINDER_RUNTIME_DIR`. |
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
- **Shared network**: `docker network create retina-edge` on the droplet
  (idempotent; both stacks declare it `external: true`).
- **Cloudflare DNS**: add a proxied A-record `tower-finder` → `157.245.214.30`.
  No cert work — the `*.retina.fm` Origin cert already covers it.
- **tower-finder nginx vhost** (separate PR against the `tower-finder` repo):
  add the `tower-finder.retina.fm` server block to `deploy/nginx.conf` and
  attach the `tower-finder` service to `retina-edge` in its compose; deploy via
  tower-finder's own pipeline (rebuilds the image, restarts the container).
- **Droplet bootstrap**: `git clone` the repo to `/opt/tower-finder-service`;
  create `/opt/tower-finder-service/.env` with `MAPRAD_API_KEY=…` (optional;
  kept on the droplet, never in CI — same pattern as tower-finder's
  `backend/.env`).

## Error handling & scope

- Failed `ruff`/`pytest` blocks deploy (and can gate merges via branch
  protection — configured outside these files).
- If the post-deploy health loop fails, the `deploy` job exits non-zero and
  surfaces in Actions. **No automated rollback in v1** (tower-finder's rollback
  machinery is heavier than this stateless service warrants); documented as a
  manual `git reset` + redeploy. Easy to add later.
- **Out of scope**: staging mirror, E2E/Playwright, Claude review bots, frontend
  CI.
