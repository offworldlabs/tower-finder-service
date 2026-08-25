# tower-finder-service

FastAPI service that ranks broadcast towers near a node from FCC and Maprad data. Split out from the `retina-server` monorepo with full git history (`git filter-repo`).

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app:app --reload
```

Optional env vars:
- `MAPRAD_API_KEY` — required for non-US queries; US can fall back to FCC only.
- `TOWER_FINDER_RUNTIME_DIR` — where `tower_config.json` is read/written (default `./data/runtime/`). On first start the runtime overlay is seeded from `backend/config/tower_config.json`.
- `TOWER_FINDER_ADMIN_TOKEN` — shared secret gating `PUT /api/config`, presented as `Authorization: Bearer <token>`. Unset closes the endpoint (503) rather than opening it, so a deploy that omits it cannot silently expose a public config write.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/towers?lat&lon&altitude&radius_km&limit&source` | Ranked towers near (lat, lon) using model-based scoring (EIRP, FSPL, distance class). |
| POST | `/api/towers` | Same tower search, enriched with spectrum-analyser measurements. Body: `MeasurementPayload` (see `backend/models/measurements.py`). Only towers the SDR can see are returned — unmatched towers are excluded. Matched towers carry real measured fields (`snr_db`, `score`, `obw_fraction`, `power_db`, `measured=true`). |
| GET | `/api/config` | Current ranking config (bands, distance classes, defaults). |
| PUT | `/api/config` | Replace ranking config; sanity-capped at 1 MB. Requires the admin bearer token (see `TOWER_FINDER_ADMIN_TOKEN`). |

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

Extracted from `offworldlabs/retina-server` on 2026-05-20 with `git filter-repo --path ...` over the 11 tower-finder paths, then made standalone:
- `tower_ranking.py` no longer imports `core.runtime_config`; the runtime overlay is inlined.
- `routes/towers.py` trimmed to tower endpoints only (dropped `/api/health`, `/api/elevation`, and the `core.users.require_admin` auth dep).
- Tests rewired to a local `app` entry point.

The parent repo still contains the same code for now; deduplication can come later.

## Deployment

CI/CD runs via GitHub Actions (`.github/workflows/ci.yml`) across three
environments, each its own droplet, Compose project and overlay:

| Environment | Droplet (SSH alias) | Public hostname | Deploys on |
| --- | --- | --- | --- |
| staging | `retina-staging` | none | push to `main` |
| production | `retina-prod` | `tower-finder.retina.fm` | push to `main`, once staging deploys and passes smoke |
| test | `retina-test` | none | `workflow_dispatch` only |

- **Every PR / push to `main`**: `ruff check`, `ruff format --check`, and
  `pytest -m "not integration"`.
- **Push to `main`**: staging deploys and is smoke-tested first; production
  deploys only after staging succeeds, so a merge no longer reaches
  production directly.
- **Manual dispatch**: `deploy-test` deploys to `retina-test`, for rehearsing
  a change without touching staging or production.

Staging and test have no public hostname and are deliberately not proxied
through retina-server's nginx: that proxying (`staging-towers.retina.fm/api/towers`)
is a separate, later change that cannot switch on until this stack exists to
answer it, and switching it on before then would make each wait on the other.
Their deploy jobs verify against the running container instead, over
`docker compose exec` (`deploy/smoke-local.sh`), once the health poll
succeeds. Production keeps the public smoke test it already had
(`deploy/smoke-test.sh`, against `tower-finder.retina.fm`).

Each deploy job SSHes to its droplet, checks the box's `hostname` matches the
environment it expects (three near-identical droplets and secret pairs mean a
mis-set secret would otherwise deploy the wrong one, silently), hard-resets
`$APP_DIR` to `origin/main`, and rebuilds. On every deploy it copies
`deploy/env.<env>.example` to `.env`, so the droplet's `.env` cannot drift or
name another environment; secrets live in `backend/.env`, which CI never
writes, and the job refuses to proceed if that file is missing.

The service runs as its own Docker Compose stack under `$APP_DIR`
(`/opt/tower-finder-service` on each droplet). It publishes no host port;
instead it joins a shared Docker network (`retina-edge`). On `retina-prod`
that network is also how retina-server's nginx reaches it: nginx terminates
TLS (Cloudflare Origin cert) and proxies `tower-finder.retina.fm` to
`http://tower-finder-service:8000`, mirroring how `api.retina.fm`,
`dash.retina.fm`, etc. are served. Staging and test have no vhost and no DNS
record.

The nginx vhost itself lives in the `retina-server` repo (`deploy/nginx.conf`) and
ships through that repo's own deploy pipeline. See "Public hostname" below.

### One-time setup

Every environment needs steps 1, 2 and 4 below on its own droplet, plus its
own pair of repository secrets. Step 3 (public hostname) is production only.

**1. Deploy SSH key (run locally, once per droplet):**

```bash
ssh-keygen -t ed25519 -f ~/.ssh/tower_finder_service_deploy -C "tfs-deploy" -N ""
ssh retina-prod "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys" \
  < ~/.ssh/tower_finder_service_deploy.pub
```

Then add GitHub Actions repository secrets (Settings → Secrets and variables →
Actions), one pair per environment:

| Environment | Host secret | Private-key secret |
| --- | --- | --- |
| production | `DEPLOY_HOST` | `DEPLOY_SSH_KEY` |
| staging | `STAGING_HOST` | `STAGING_SSH_KEY` |
| test | `TEST_HOST` | `TEST_SSH_KEY` |

Each host secret is that droplet's public address; each key secret is the
matching private key. Every deploy job checks its own pair is set and fails
with the secret's name if not.

**2. Shared network:**

```bash
# On each droplet, create the shared network both stacks attach to (idempotent).
docker network create retina-edge 2>/dev/null || true
```

**3. Public hostname (production only):**

In the Cloudflare dashboard, add a **proxied** DNS A-record (orange cloud on)
for `tower-finder` pointing at `retina-prod`. The `*.retina.fm` Origin cert
already covers it, so no certificate work is needed. Staging and test get no
record: they are verified on the droplet, not over a public path.

Add a server block for `tower-finder.retina.fm` to the droplet's copy of the
`retina-server` repo's `deploy/nginx.conf` (proxying to
`http://tower-finder-service:8000` over `retina-edge`), and attach the
`tower-finder-service` service to the `retina-edge` network in its compose
file. Deploy that change through retina-server's own pipeline so its image is
rebuilt and the container restarted.

**4. Droplet bootstrap (run on the droplet as root):**

```bash
git clone https://github.com/offworldlabs/tower-finder-service.git /opt/tower-finder-service
cd /opt/tower-finder-service
cp backend/.env.example backend/.env
# Edit backend/.env: set TOWER_FINDER_ADMIN_TOKEN (a different one per
# environment: it gates config writes and must not cross a trust boundary).
# Set MAPRAD_API_KEY on production only: staging and test are not meant to
# reach the metered upstream, so leave it unset there; see "Metered upstream"
# below for the consequence. This file holds secrets and CI never writes it.
cp deploy/env.<env>.example .env   # prod, staging or test
docker compose up -d --build
```

After this, every push to `main` deploys to staging first, then to
production once staging deploys and passes its smoke test.

### Metered upstream

`MAPRAD_API_KEY` is deliberately absent from staging and test: those
environments exist to rehearse a change, not to spend a metered budget, and
Maprad's upstream is billed per query. Both serve US tower queries via the
keyless FCC path as normal, but every `au` or `ca` query returns 500. A
ranking change that touches the Maprad path can only be exercised in
production.

### Rollback (manual)

No automated rollback in v1. To roll back, SSH to the droplet and reset to a
known-good commit:

```bash
cd /opt/tower-finder-service
git reset --hard <good-commit-sha>
docker compose up -d --build
```
