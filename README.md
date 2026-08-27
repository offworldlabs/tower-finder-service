# tower-finder-service

FastAPI service that ranks broadcast towers near a node from FCC and Maprad data. Split out from the `retina-server` monorepo with full git history (`git filter-repo`).

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app:app --reload
```

That serves the API. For the UI, either build it once so `app.py` picks up
`frontend/dist`:

```bash
cd frontend && npm ci && npm run build
```

…or run Vite alongside `uvicorn` while working on it — it proxies `/api` back
to port 8000:

```bash
cd frontend && npm run dev
```

The Docker image builds the UI itself and serves it from the same origin as the
API, so a deployed service is a single container with no separate web host.

Optional env vars:
- `MAPRAD_API_KEY` — required for non-US queries; US can fall back to FCC only.
- `TOWER_FINDER_RUNTIME_DIR` — where `tower_config.json` is read/written (default `./data/runtime/`). On first start the runtime overlay is seeded from `backend/config/tower_config.json`.
- `TOWER_FINDER_ADMIN_TOKEN` — shared secret gating `PUT /api/config`, presented as `Authorization: Bearer <token>`. Unset closes the endpoint (503) rather than opening it, so a deploy that omits it cannot silently expose a public config write.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/towers?lat&lon&altitude&radius_km&limit&source` | Ranked towers near (lat, lon) using model-based scoring (EIRP, FSPL, distance class). |
| POST | `/api/towers` | Same tower search, enriched with spectrum-analyser measurements. Body: `MeasurementPayload` (see `backend/models/measurements.py`). Only towers the SDR can see are returned — unmatched towers are excluded. Matched towers carry real measured fields (`snr_db`, `score`, `obw_fraction`, `power_db`, `measured=true`). |
| GET | `/api/elevation?lat&lon` | Ground elevation at a point. The search form pre-fills altitude from this; `GET /api/towers` resolves altitude itself when none is given. 502 if the upstream lookup fails. |
| GET | `/api/config` | Current ranking config (bands, distance classes, defaults). |
| PUT | `/api/config` | Replace ranking config; sanity-capped at 1 MB. Requires the admin bearer token (see `TOWER_FINDER_ADMIN_TOKEN`). Validated and applied before it is written (400 if either fails), so the file on the persistent volume only ever holds a config the running process has accepted. |

## Layout

| Path | What's there |
| --- | --- |
| `app.py` | FastAPI entry point |
| `backend/routes/towers.py` | HTTP routes |
| `backend/services/tower_ranking.py` | Ranking algorithm + config loader/validator |
| `backend/services/tower_coverage.py` | Optional n>=2 coverage-area-added scoring, injected into the ranking as a `coverage_scorer` |
| `backend/clients/fcc.py` | FCC TV/FM Query CGI client |
| `backend/clients/maprad.py` | Maprad.io broadcast-systems client |
| `backend/config/tower_config.json` | Default ranking config (image-shipped) |
| `backend/tests/` | pytest suite (176 tests); integration tests require running `capture_fixture.py` first) |
| `frontend/` | The standalone React UI (Vite). Built into the image and served by `app.py`; `npm test` / `npm run test:e2e` cover it |
| `pyproject.toml` | Package + tooling config |

## Tests

```bash
pytest -q                 # backend
cd frontend && npm test   # frontend unit tests
cd frontend && npm run test:e2e   # Playwright, against the built dist
```

## Origin

Extracted from `offworldlabs/retina-server` on 2026-05-20 with `git filter-repo --path ...` over the 11 tower-finder paths, then made standalone:
- `tower_ranking.py` no longer imports `core.runtime_config`; the runtime overlay is inlined.
- `routes/towers.py` trimmed to tower endpoints only (dropped `/api/health` and the
  `core.users.require_admin` auth dep). `/api/health` and `/api/elevation` were later
  added back — the first for deploy smoke tests, the second for the UI's altitude field.
- Tests rewired to a local `app` entry point.

The parent repo still contains the same code for now; deduplication can come later.
The two have already diverged once in a way worth knowing about: the region
detection here is a border-polygon lookup (`services/region_lookup.py`), while
retina-server kept a lat/lon bounding-box heuristic that returned "ca" for every
US point above 42°N until it was ported across.

## Deployment

CI/CD runs via GitHub Actions (`.github/workflows/ci.yml`) across three
environments, each its own droplet, Compose project and overlay:

| Environment | Droplet (SSH alias) | Public hostname | Deploys on |
| --- | --- | --- | --- |
| staging | `retina-staging` | `staging-towers.retina.fm` | push to `main` |
| production | `retina-prod` | `towers.retina.fm` | push to `main`, once staging deploys and passes smoke |
| test | `retina-test` | `test-towers.retina.fm` | `workflow_dispatch` only |

- **Every PR / push to `main`**: `ruff check`, `ruff format --check`, and
  `pytest -m "not integration"`.
- **Push to `main`**: staging deploys and is smoke-tested first; production
  deploys only after staging succeeds, so a merge no longer reaches
  production directly.
- **Manual dispatch**: `deploy-test` deploys to `retina-test`, for rehearsing
  a change without touching staging or production.

Every environment has a public hostname — a proxied Cloudflare record
pointing at its own droplet, served today by retina-server's nginx there. The
staging and test deploy jobs still verify against the running container, over
`docker compose exec` (`deploy/smoke-local.sh`), once the health poll
succeeds: the droplet-local path cannot be blurred by Cloudflare caching, and
proves this deploy rather than whatever the name still routes to. Production
additionally keeps the public smoke test it already had
(`deploy/smoke-test.sh`, against `towers.retina.fm`).

Each deploy job SSHes to its droplet, checks the box's `hostname` matches the
environment it expects (three near-identical droplets and secret pairs mean a
mis-set secret would otherwise deploy the wrong one, silently), hard-resets
`$APP_DIR` to the commit the run is for (`github.sha`, not `origin/main`, so a
second merge landing mid-run cannot reach production untested), and rebuilds. On every deploy it copies
`deploy/env.<env>.example` to `.env`, so the droplet's `.env` cannot drift or
name another environment; secrets live in `backend/.env`, which CI never
writes, and the job refuses to proceed if that file is missing.

The service runs as its own Docker Compose stack under `$APP_DIR`
(`/opt/tower-finder-service` on each droplet). The app container publishes no
host port; instead it joins a shared Docker network (`retina-edge`). On
every droplet that network is also how retina-server's nginx reaches it: nginx
terminates TLS (Cloudflare Origin cert) and proxies that droplet's towers name
to `http://tower-finder-service:8000`, mirroring how `api.retina.fm`,
`dash.retina.fm`, etc. are served.

That vhost lives in the `retina-server` repo (`deploy/nginx/nginx.conf.template`)
and ships through that repo's own deploy pipeline. See "Public hostname" below,
and "Own ingress + the flip plan" for how it stops being the way in.

### Own ingress + the flip plan

The stack now also runs an `edge` container: the official nginx image rendering
`deploy/nginx/edge.conf.template`, listening on **8443** (a Cloudflare-supported
HTTPS origin port) with the same Cloudflare origin certificate retina-server
uses, proxying everything to the app. It is this service's own way in.

**It ships dark.** Nothing routes to 8443 in any environment. Browsers still
reach the towers names on 443, through retina-server's nginx, exactly as
before — and the fleet still calls `tower-finder.retina.fm`: `retina-spectrum`
reads that hostname from retina-node's compose and moving the *name* would need
an OTA rollout, so that name never moves. Only the origin port behind the names
does, and only Cloudflare sees that.

Two couplings with retina-server are worth separating here, because only one of
them is going away:

- The **towers-proxy seam** — retina's own vhosts proxying `/api/towers`,
  `/api/elevation` and `/api/config` to `tower-finder-service:8000` over
  `retina-edge` — is the intended permanent architecture. It does not change,
  which is why the app keeps its `retina-edge` alias. The edge container reaches
  the app over this project's own `internal` network instead, so this service's
  ingress does not depend on retina's network existing.
- The **serving vhosts** in retina's nginx — the towers vhosts, and the
  `${HOST_LEGACY_REDIRECT}` vhost that proxies `tower-finder.retina.fm` (the
  fleet name) here — are what the flip below retires.

**Verification.** Until the flip there is no public path to 8443, so each deploy
job probes the edge from the droplet itself:

```bash
curl -sk --resolve "towers.retina.fm:8443:127.0.0.1" \
  https://towers.retina.fm:8443/api/health   # must be 200
```

`--resolve` rather than DNS on purpose: every name resolves to Cloudflare,
not to the droplet, and the probe must reach the local listener directly.
`deploy/smoke-test.sh` still targets `https://towers.retina.fm` over 443 —
the URL public traffic enters by both before and after the flip; the flip
changes the origin port behind it, not the address.

**The origin is Cloudflare-only, and stays that way.** retina-server enforces
Cloudflare Authenticated Origin Pulls (`ssl_verify_client on`) plus a
DOCKER-USER rule that narrows 80 and 443 to Cloudflare's ranges. The edge keeps
the first half: it requires a client certificate signed by Cloudflare's
origin-pull CA, refusing anything else with `403`, exempting only peers on
`127.0.0.0/8` and `172.16.0.0/12` (the droplet itself and its Docker networks)
so the deploy probe above can run. The second half is retina's, and
now covers the edge too: `deploy/docker-user-firewall.sh` there sets
`PORTS="80,443,8443"`. Changing that list does nothing on a live droplet by
itself — only boot re-runs the unit — so run
`systemctl restart retina-firewall.service` on each droplet before its flip.

**The flip is one manual Cloudflare change per hostname**, and because every
environment has its own name and droplet, it rehearses on test first:

> Rules → Origin Rules → Create rule
> - Name: `tower-finder origin port (test)`
> - When incoming requests match: `Hostname` `equals` `test-towers.retina.fm`
> - Then: **Rewrite to… Destination Port → `8443`**

Verify, then repeat for `staging-towers.retina.fm`, then `towers.retina.fm`.
Production also needs the same rewrite for `tower-finder.retina.fm` — the
fleet name — either as a fourth rule or a `Hostname` `is in` list alongside
`towers.retina.fm`: without it, retiring retina's `${HOST_LEGACY_REDIRECT}`
vhost would cut the fleet off. The names are proxied (orange cloud), so
clients stay on 443 and never see the origin port change. Confirm
Authenticated Origin Pulls is still on for the zone before flipping — if it
were off, Cloudflare would present no client certificate and the edge would
answer 403.

**After the flip is verified** (`curl https://towers.retina.fm/api/health`
still 200, and the request shows up in `docker compose logs edge` rather than in
retina's nginx), retina-server deletes its `${HOST_LEGACY_REDIRECT}` vhost and
the `HOST_LEGACY_REDIRECT` environment variable, and can retire the towers
vhosts' tower-serving role at its own pace. That is a separate PR in that
repo, and it must come after, not with, the flip.

**Rollback is deleting the Origin Rule(s).** Traffic instantly re-enters through
retina-server's nginx on 443, which still carries the vhosts until the step
above. Nothing has to be redeployed here.

### One-time setup

Every environment needs steps 1, 2 and 4 below on its own droplet, plus its
own pair of repository secrets. Step 3 is already in place everywhere.

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

The guard reads the droplet's own OS `hostname`, not the SSH alias you connect
by, so each box must actually be named for its environment (`hostnamectl
set-hostname retina-staging`, or DigitalOcean's droplet name at creation). A
local `~/.ssh/config` alias alone leaves every deploy failing at the guard.

**2. Shared network:**

```bash
# On each droplet, create the shared network both stacks attach to (idempotent).
docker network create retina-edge 2>/dev/null || true
```

**2b. Origin certificate (every droplet):**

The `edge` container bind-mounts `/etc/ssl/cloudflare` read-only and needs
`cert.pem`, `key.pem` and `origin-pull-ca.pem` there. retina-server's
`deploy/setup-server.sh` already places them on every droplet these stacks are
co-located on, so there is normally nothing to do — but nginx will not start
without them, and it fails at boot rather than at first request.

**3. Public hostname (already in place everywhere):**

In the Cloudflare dashboard each environment has a **proxied** DNS A-record
(orange cloud on) pointing at its droplet: `towers` → `retina-prod`,
`staging-towers` → `retina-staging`, `test-towers` → `retina-test`, plus
`tower-finder` (the fleet name) → `retina-prod`. The `*.retina.fm` Origin
cert covers them all, so no certificate work is needed. retina-server's nginx
carries the matching server blocks (proxying to
`http://tower-finder-service:8000` over `retina-edge`) until the flip retires
them.

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

# The CARTO basemap key, in a file of its own outside the repo. Unkeyed tiles
# come back stamped "API KEY REQUIRED", so a droplet whose map anyone looks at
# wants this; a droplet without it still builds and runs. Every deploy appends
# this file to ./.env after copying the example above, and docker-compose.yml
# interpolates it into the frontend's VITE_CARTO_API_KEY build arg. It is NOT
# in backend/.env: Compose reads build args from ./.env alone.
install -d -m 700 /root/.secrets
printf 'CARTO_API_KEY=%s\n' "<key from the CARTO dashboard>" > /root/.secrets/carto.env
chmod 600 /root/.secrets/carto.env

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
