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
- `TOWER_FINDER_FEEDBACK_TOKEN`: shared secret gating `POST /api/feedback/tower-outcome`, same bearer shape and the same fail-closed rule. Separate from the admin token on purpose: every node in the fleet holds this one, so a node that is lost or read must not also hand over the ranking config. See "Fleet feedback" below.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/towers?lat&lon&altitude&radius_km&limit&source&frequencies` | Ranked towers near (lat, lon). Ranked by expected detection area (`expected_area_km2`, see "Ranking"), with modelled received power as the tie-break; every tower also carries `best_azimuth_deg` (where to aim the Yagi) and `horizon_km`. `frequencies` boosts towers near a frequency the caller names, in MHz; it never drops one. Accepts both spellings a client might send: comma-separated (`frequencies=95.5,101.1`) and repeated (`frequencies=95.5&frequencies=101.1`), including a mix of the two. Up to ten values are used and echoed back as `query.user_frequencies_mhz`. Stations licensed on one transmitter (FCC channel-sharing pairs, LPFM time-shares) are merged into a single row; the partners' callsigns are listed in `shared_callsigns`. |
| POST | `/api/towers` | Same tower search, enriched with spectrum-analyser measurements. Body: `MeasurementPayload` (see `backend/models/measurements.py`). Only towers the SDR can see are returned — unmatched towers are excluded. Matched towers carry real measured fields (`snr_db`, `score`, `obw_fraction`, `power_db`, `measured=true`). At most 2000 measurements per request, 422 beyond that: ranking pairs every tower with every measurement, synchronously on the one event loop. |
| GET | `/api/elevation?lat&lon` | Ground elevation at a point. The search form pre-fills altitude from this; `GET /api/towers` resolves altitude itself when none is given. 503 when the upstream cannot be reached, 404 for a point it has no data for: two different facts, so a caller can tell an outage from a gap. `GET /api/towers` treats both as best-effort and still returns towers, with a null elevation. |
| GET | `/api/config` | Current ranking config (bands, band priority, band offsets, sort order, scoring knobs, defaults). |
| PUT | `/api/config` | Replace ranking config; sanity-capped at 1 MB. Requires the admin bearer token (see `TOWER_FINDER_ADMIN_TOKEN`). Validated and applied before it is written (400 if either fails), so the file on the persistent volume only ever holds a config the running process has accepted. |
| POST | `/api/feedback/tower-outcome` | Fleet feedback ingest: what a node (or the archive job) actually got out of a tower we ranked. Body: one `TowerOutcome` or a list of up to 100 (see `backend/models/feedback.py`). Requires the feedback bearer token (see `TOWER_FINDER_FEEDBACK_TOKEN`); unset closes it with a 503. Unknown keys are a 422, so a misspelled field is loud rather than silently dropped. Returns `{"stored": n}`. See "Fleet feedback". |
| GET | `/api/feedback/summary?limit=` | Per-tower rollup of stored outcomes, busiest first: row and node counts, distinct receiver cells, mean multiplier, callsigns seen, last observation. Requires the admin bearer token, not the feedback one: the rollup names sites and node counts. |

## Ranking

The rank answers "which of these towers lights up the most ground", not "which
is loudest here". For each tower the service grids a disk around the receiver
and counts the 2 km cells where a 10 m^2 target at 3 km altitude would be
detected at 13 dB SNR or better, sweeping 24 Yagi boresights and keeping the
best one. The terms are the two-way bistatic radar equation (EIRP, the
`1/(R_t^2 R_r^2)` spreading loss, `lambda^2 sigma / (4 pi)^3`), a noise floor
raised by whatever of the direct path survives 50 dB of cancellation, the
band's `B * T` processing gain, a 4/3-earth radio-horizon penalty (20 dB plus
0.5 dB/km beyond it) and a cut on bistatic angles above 150 degrees. The model
lives in `backend/services/tower_scoring.py` and is a faithful port of a
validated reference, vectorised with numpy so a few hundred towers score inside
the request.

The practical consequence: a megawatt transmitter 5 km away is the loudest
signal in the band and near the bottom of the list, because its direct path
swamps the surveillance channel and the geometry it offers is a sliver around
the baseline. Area rises with distance to roughly 50 km and then falls away as
path loss and the horizon take over.

Three fields are added to every tower, and nothing that was there before
changed name, type or meaning: `expected_area_km2`, `best_azimuth_deg` and
`horizon_km`.

Config, all optional, all in `tower_config.json`:

- `ranking.band_offset_db` ({`VHF`, `UHF`, `FM`}) is a per-band nudge in dB
  applied to EIRP inside the model. It replaces the old hard band tier, which
  put every FM station below every TV tower whatever their powers. Shipped as
  zeros, to be fitted from fleet data.
- `ranking.band_priority` still exists, is still applied and is still sortable,
  for an overlay that deliberately ranks on the tier.
- `scoring` holds the model's knobs: `cancellation_db`, `target_rcs_m2`,
  `snr_min_db`, `max_bistatic_angle_deg`, `target_alt_km`, `rx_height_m`,
  `grid_km`, `max_range_km`, `n_azimuths`, `yagi_hpbw_deg`,
  `yagi_front_to_back_db`, `noise_figure_db` and `band_params`
  (`bw_hz` / `cpi_s` per band). The receiver antenna gain is not among them: it
  comes from `receiver.rx_antenna_gain_dbi`, so the model and the link budget
  cannot disagree about the antenna.

**Migration.** The runtime overlay is a persistent volume, seeded once and never
re-seeded, so a deployed environment still holds the sort order that shipped
before this change. On load, an overlay whose `ranking.sort_order` is exactly
one of the defaults this image has shipped is upgraded in memory to the current
one, with a warning naming the file; the file itself is left alone, and a PUT
makes the choice explicit either way. An overlay whose sort order differs at all
is an operator's decision and is never touched.

## Fleet feedback

The ranking predicts how much area a tower lights up (`expected_area_km2`).
Nothing ever told it whether that was true. These endpoints close that loop: the
fleet reports what it actually got out of a tower, and the next request for that
tower from a nearby receiver is corrected by what came back.

Two producers post the same row shape:

- **Calibration.** A node's Auto-Calibrate tries up to three candidate towers and
  posts one row per candidate: the `outcome` it reached, `max_evidence` (0 none,
  1 detections, 2 an active track), `max_detections`, and the gains it settled on.
- **Archive.** A later retina-server job posts archive-derived aggregates per
  tower per window: `verified_range_p85_km`, `adsb_match_rate`, `snr_median_db`,
  `hours_observed`.

Payload (`backend/models/feedback.py`), one object or a list of up to 100:

```bash
curl -X POST https://towers.retina.fm/api/feedback/tower-outcome \
  -H "Authorization: Bearer $TOWER_FINDER_FEEDBACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"node_id": "node-7", "rx_lat": 34.05, "rx_lon": -118.25,
       "tx_lat": 34.23, "tx_lon": -118.06, "fc_hz": 98700000,
       "callsign": "KABC", "source": "calibration",
       "outcome": "confirmed_track", "max_evidence": 2,
       "max_detections": 41, "duration_s": 90, "gain_a": 12,
       "gain_b": 8, "lna_state": 3}'
# {"stored": 1}
```

Unknown keys are a 422 rather than being dropped, so a node that misspells a
field finds out instead of posting rows that weigh nothing. `observed_at` is
optional; the server dates the row when it is absent, and keeps `received_at`
separately so a node with a bad clock still lands something orderable.

**Where it lives.** A SQLite file at `<TOWER_FINDER_RUNTIME_DIR>/feedback.db`,
the same persistent overlay as `tower_config.json` (default
`./data/runtime/feedback.db`). The table keeps its newest 200,000 rows and drops
the rest on insert, so nothing has to prune it on a schedule.

**How it enters the ranking.** On each tower search, one query pulls every row
from receivers within 30 km of the caller, groups them by tower (about 100 m of
position and 100 kHz of frequency), and turns each row into a log-multiplier:
calibration outcomes map through a table (`confirmed_track` 1.5,
`no_confirmed_track` 0.1, `unstable_overload` 0.02; the rest describe the run
rather than the tower and are ignored), and archive rows are weighted by their
hours observed, capped at 24. Every tower then carries:

- `feedback_n`: total weight of rows behind the correction (0.0 when there are none).
- `feedback_factor`: `exp(n / (n + 5) * r̄)`, where `r̄` is the weighted mean
  log-multiplier. `expected_area_km2` is multiplied by this.

The `n / (n + k)` shrinkage is the point of the design: one node saying "nothing
there" should nudge a tower, not erase it, while a hundred rows saying it
converge on what they say. A single confirmed track moves the area about 7%.
A database that will not open costs the caller the correction, not the tower
list: the lookup is wrapped, logs a warning, and leaves the towers untouched.

`GET /api/feedback/summary` (admin token) shows what has come back per tower:
row and node counts, distinct receiver cells, the mean multiplier, the callsigns
seen and the last observation.

**Provisional, and what is left.** The archive residual should be
`log((pi * verified_range_p85_km^2) / expected_area_km2)`, but the model area at
request time is not known at ingest and recomputing it here would fork the
ranking. Until the request-time area is carried on the row, archive rows store
their raw fields and stand in the ADS-B match rate as the multiplier
(`match_rate / 0.5`, clamped to 0.05 ... 3.0). Fitting the correction per band
and per region, rather than per tower and receiver radius, is future work, as is
letting a node's own history weigh more than a neighbour's.

## Layout

| Path | What's there |
| --- | --- |
| `app.py` | FastAPI entry point |
| `backend/routes/towers.py` | HTTP routes |
| `backend/services/tower_ranking.py` | Ranking algorithm + config loader/validator |
| `backend/services/tower_scoring.py` | Bistatic detection-area model: what the rank is computed from |
| `backend/services/tower_coverage.py` | Optional n>=2 coverage-area-added scoring, injected into the ranking as a `coverage_scorer` |
| `backend/clients/fcc.py` | FCC TV/FM Query CGI client |
| `backend/clients/maprad.py` | Maprad.io broadcast-systems client |
| `backend/config/tower_config.json` | Default ranking config (image-shipped) |
| `backend/services/tower_feedback.py` | Fleet feedback store (SQLite) and the `apply_feedback` correction |
| `backend/routes/feedback.py` | Feedback ingest + admin summary routes |
| `backend/models/feedback.py` | `TowerOutcome` payload model |
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
  production directly. The smoke checks assert only what they can establish
  from where they run: the `/api/elevation` check passes on an elevation and
  on the route reporting its own upstream unavailable, since open-meteo's
  uptime is not the deploy's to assert, and fails on anything else.
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

It then asserts two things a status cannot show. The config the container is
running is compared with this checkout's template, rendered the same way, so a
container that never picked the change up fails the deploy rather than serving
the previous one; and `/` is required to be 200, which `/api/health` does not
establish, since the app serves the API with or without a built frontend. The
config travels in the edge's image (`deploy/nginx/Dockerfile`) so that a
template change alters the image and `docker compose up -d --build` replaces
the container: a bind-mounted file's contents are invisible to compose, which
recreates only on a changed definition. `nginx -t` runs against the new image
before that swap, so a template nginx rejects fails the deploy with the working
container still up.

`--resolve` rather than DNS on purpose: every name resolves to Cloudflare,
not to the droplet, and the probe must reach the local listener directly.
`deploy/smoke-test.sh` still targets `https://towers.retina.fm` over 443 —
the URL public traffic enters by both before and after the flip; the flip
changes the origin port behind it, not the address.

**A health check cannot see a broken page.** This vhost serves the SPA as well
as the API, and the failure mode worth guarding is a page whose document loads
while its assets do not: `nginx -t` passes, `/api/health` passes, and an
`id="root"` probe passes, against a blank screen. That is how retina-server's
attempt at this same swap failed (its PR #260). So check the page too:

```bash
# --resolve as above.
HOST=towers.retina.fm
CURL="curl -sk --resolve $HOST:8443:127.0.0.1"

# Transport first, so an unreachable listener does not read as a missing header.
if ! $CURL -o /dev/null "https://$HOST:8443/"; then
  echo "CANNOT REACH THE LISTENER"
else
  # GET, not -I: FastAPI registers GET only, so HEAD answers 405 and `always`
  # stamps the policy onto that too, passing on a page never served.
  $CURL -D- -o /dev/null "https://$HOST:8443/" | grep -i content-security-policy \
    || echo "NO POLICY ON THE DOCUMENT"

  # An empty ASSET would otherwise re-fetch the document and return the 200
  # the check is looking for.
  ASSET=$($CURL "https://$HOST:8443/" | grep -o '/assets/[^"]*\.js' | head -1)
  if [ -z "$ASSET" ]; then
    echo "NO ASSET LINK IN THE DOCUMENT"
  else
    $CURL -o /dev/null -w '%{http_code}\n' "https://$HOST:8443$ASSET"  # must be 200
  fi
fi
```

Then open it in a browser once and confirm the console is clean: a CSP that is
too strict shows up only there, as a blocked subresource, and never as a
non-200.

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
