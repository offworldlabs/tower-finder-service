# tower-finder-service

FastAPI service that ranks broadcast towers near a node from FCC and Maprad data. Split out from the `retina-server` monorepo with full git history (`git filter-repo`).

## Run

```bash
uv sync
PYTHONPATH=.:backend uv run uvicorn app:app --reload
```

That serves the API. The service is never installed into `.venv`, so
`PYTHONPATH` puts `app.py` and `backend/` on the path, as the image does and as
`pythonpath` in `pyproject.toml` does for pytest. For the UI, either build it
once so `app.py` picks up `frontend/dist`:

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
- `MAPRAD_API_KEY` — required for `au` and `ca` queries. US searches never reach Maprad: its only US dataset is the FCC ULS licence system, which holds no broadcast stations.
- `TOWER_FINDER_RUNTIME_DIR` — where `tower_config.json` is read/written (default `./data/runtime/`). On first start the runtime overlay is seeded from `backend/config/tower_config.json`.
- `TOWER_FINDER_GEOCODER_CONTACT` — contact string in the `User-Agent` `POST /api/geocode` sends to Nominatim (default the repo URL). Nominatim's usage policy requires an identifying contact and one request per second; unidentified traffic gets blocked, and it is the fallback for city and ZIP lookups.
- `TOWER_FINDER_ADMIN_TOKEN` — shared secret gating `PUT /api/config`, presented as `Authorization: Bearer <token>`. Unset closes the endpoint (503) rather than opening it, so a deploy that omits it cannot silently expose a public config write.
- `TOWER_FINDER_FEEDBACK_TOKEN` — shared secret gating `POST /api/feedback/tower-outcome`, same bearer shape and the same fail-closed rule. Separate from the admin token on purpose: every node in the fleet holds this one, so a node that is lost or read must not also hand over the ranking config. See "Fleet feedback" below.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/towers?lat&lon&altitude&radius_km&limit&source&frequencies` | Ranked towers near (lat, lon). The direct path uses a terrestrial path-loss model and an under-beam derating for towers close to a tall mast (see "Path-loss model"); each row carries the breakdown as `path_loss_db`, `excess_path_loss_db` and `underbeam_loss_db`. Ranked by expected detection area (`expected_area_km2`, see "Ranking"), then reordered so the top of the list is not ten channels of one mast; every tower also carries `best_azimuth_deg` (where to aim the Yagi), `horizon_km`, and the site annotations `site_id`, `site_channels` and `diversity_penalty`. `query.ranking` names the ordering that ran (`expected_area_mmr`, `expected_area` or `sort_order`). `frequencies` boosts towers near a frequency the caller names, in MHz; it never drops one, and the boosted towers keep the top of the list whatever the diversity pass does inside each group. Accepts both spellings a client might send: comma-separated (`frequencies=95.5,101.1`) and repeated (`frequencies=95.5&frequencies=101.1`), including a mix of the two. Up to ten values are used and echoed back as `query.user_frequencies_mhz`. Stations licensed on one transmitter (FCC channel-sharing pairs, LPFM time-shares) are merged into a single row; the partners' callsigns are listed in `shared_callsigns`. |
| POST | `/api/towers` | Same tower search, enriched with spectrum-analyser measurements. Body: `MeasurementPayload` (see `backend/models/measurements.py`). Only towers the SDR can see are returned; unmatched towers are excluded. Matched towers carry real measured fields (`snr_db`, `score`, `obw_fraction`, `power_db`, `measured=true`). The sweep also calibrates the model: `query.calibration_offset_db` and `query.calibrated_towers` say whether it could be done and over how many towers, each matched tower says whether its direct path is `measured` or modelled (`direct_power_source`) and what the analyser's `score` discounted it by (`measurement_quality`). See "Ranking". At most 2000 measurements per request, 422 beyond that: ranking pairs every tower with every measurement, synchronously on the one event loop. |
| GET | `/api/elevation?lat&lon` | Ground elevation at a point. The search form pre-fills altitude from this; `GET /api/towers` resolves altitude itself when none is given. 503 when the upstream cannot be reached, 404 for a point it has no data for: two different facts, so a caller can tell an outage from a gap. `GET /api/towers` treats both as best-effort and still returns towers, with a null elevation. A coordinate's elevation does not change, so answers are cached for the life of the process; open-meteo caps a request at 100 coordinates, so a larger search is chunked and a chunk that fails costs only its own coordinates. A 429 puts the upstream in a cooldown that lengthens with each further failure, rather than being retried into. |
| POST | `/api/geocode` | Address, place or ZIP → a point, for the search box. Body `{"query": "..."}`, 1–200 characters once stripped. Tries the US Census geocoder first (authoritative for street addresses, unkeyed, no usage policy) and falls back to Nominatim, which is the only one of the two that answers a city or a bare ZIP. `precision` (`street`/`postcode`/`locality`) says how far to trust the point; the form warns on anything below street level, since a city-centre fix can sit 10 km from the real site. 404 when both answered and neither knew it, 503 when one could not be reached and no other matched — a search box has to tell "check the spelling" from "try again". Unauthenticated, so it is capped at four concurrent lookups, caches for 24 h, and holds Nominatim to its one request per second. The query text is never logged. |
| POST | `/api/feedback/tower-outcome` | Fleet feedback ingest: what a node's Auto-Calibrate actually got out of a tower we ranked. Body: one `TowerOutcome` or a list of up to 100 (see `backend/models/feedback.py`). Requires the feedback bearer token (see `TOWER_FINDER_FEEDBACK_TOKEN`); unset closes it with a 503. Unknown keys are a 422, so a misspelled field is loud rather than silently dropped. Rows carrying a `run_id` are stored once per node, run and tower, so a retried post is safe. Returns `{"stored": n, "ignored": m}`. See "Fleet feedback". |
| GET | `/api/feedback/summary?limit=` | Per-tower rollup of stored outcomes, busiest first: row and node counts, distinct receiver cells, mean multiplier, callsigns seen, last observation. Requires the admin bearer token, not the feedback one: the rollup names sites and node counts. |
| GET | `/api/config` | Current ranking config (bands, band priority, band offsets, sort order, scoring and propagation knobs, defaults). |
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

### Diversity ordering

A node tunes one centre frequency at a time, and its Auto-Calibrate tries at
most three candidates from the top of this list. A San Francisco search returns
14 channels on Sutro Tower and 7 on Mount Diablo, so a list ordered on area
alone spends all three candidate slots on one mast: the same direct path, the
same bearing and the same failure, three times.

So the list is reordered by maximal marginal relevance. Each pick maximises
`expected_area_km2 * (1 - lambda * max_sim)`, where `max_sim` is the tower's
highest similarity to anything already picked. That reads as "the best tower
that is not another view of the one above it".
Similarity is 1.0 for another channel on the same mast in the same
band, 0.5 for the same mast in another band, and otherwise a Gaussian in
bearing and in range (30 degrees, 15 km), halved again across bands. Sites are
found by greedy clustering within `site_radius_km`, the way channel-sharing
partners already are, except that this one crosses frequency and band because
the thing being counted is the mast.

The pass runs only when `ranking.diversity.enabled` is true and the configured
`sort_order` leads with `expected_area_km2` descending, which is what the
shipped config does. Rank on anything else and the plain sort applies
untouched. The user-frequency split survives it either way: a hand-typed
frequency says which towers the caller asked about, so the matched group is
diversified within itself and still sorts ahead of the rest.

Every tower carries `site_id` (the site's lead tower's coordinates),
`site_channels` (how many towers this search found on that site, all bands,
counted before `limit` truncates the list) and
`diversity_penalty` (the `lambda * similarity` the tower paid at the moment it
was picked, 0 for the first pick and whenever the pass did not run).
`query.ranking` says which of `expected_area_mmr`, `expected_area` or
`sort_order` produced the order.

### Measurement calibration (POST only)

On `POST /api/towers` the node's own sweep is the best direct-path measurement
available, and terrain is the largest error in the path model. `power_db`
is always dBFS, the ATSC pilot peak as the node's front end sees it, and is
present only for TV rows (FM sends null). It carries no absolute scale of its
own, but the differences between channels within one sweep are real. So the
median of (measured minus modelled) over the matched TV towers is taken as that
sweep's fixed offset, at least two of them or no calibration is done, and
removing it leaves each tower's own residual on the modelled dBm scale. That
residual becomes the tower's direct-path power inside the model, which changes
the noise floor rather than the energy reaching the target. The response
reports `query.calibration_offset_db` (null when there was too little to take
one) and `query.calibrated_towers`, and each tower says
`direct_power_source: "measured"` or `"model"`.

The analyser's `score` is not a sort key. It says how well the SDR hears the
illuminator, which is not what the rank is answering, so on the POST path it
becomes a quality discount instead: `expected_area_km2` is multiplied by
`0.5 + 0.5 * score` (clamped to [0, 1]), stamped as `measurement_quality`, so a
badly resolved channel is demoted rather than deleted. It is deliberately never
compared between towers: FM's score is an SNR ramp and TV's is absolute dBFS,
two different scales set upstream in retina-spectrum.

Config, all optional, all in `tower_config.json`:

- `ranking.band_offset_db` ({`VHF`, `UHF`, `FM`}) is a per-band nudge in dB
  applied to EIRP inside the model. It replaces the old hard band tier, which
  put every FM station below every TV tower whatever their powers. Shipped as
  zeros, to be fitted from fleet data.
- `ranking.band_priority` still exists, is still applied and is still sortable,
  for an overlay that deliberately ranks on the tier.
- `ranking.diversity` holds the ordering knobs: `enabled`, `lambda` (how much
  of a repeat's similarity is charged against it), `same_site_same_band` and
  `same_site_other_band` (the two same-mast similarities), `bearing_sigma_deg`
  and `distance_sigma_km` (the Gaussian widths for towers on different sites),
  `other_band_factor` (what a cross-band pair keeps of that), and
  `site_radius_km` (how far apart two records can be and still be one mast).
  The four unit-interval values are validated to [0, 1] and the three widths to
  strictly positive, because they meet as a `1 - lambda * sim` multiplier and as
  divisors: outside those ranges the pass promotes the most redundant tower
  instead of demoting it, or divides by zero, on every search.
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

`ranking.diversity` needs no such migration and gets none: no overlay on disk
carries the section, and an absent section takes the in-code default, so every
environment turns the diversity pass on at the deploy that brings it. An
operator who does not want it sets `enabled` to false with a PUT. The knobs an
overlay does name are kept and the rest defaulted, so a partial section is a
partial override rather than a reset.

## Path-loss model

`received_power_dbm` is what a receiver with `receiver.rx_antenna_gain_dbi` of
gain, pointed at the tower and polarisation-matched, hears at
`propagation.rx_height_m` above ground. It is not what an indoor whip hears: on
one home installation the measured levels sat 20 to 50 dB below the earlier
free-space figures, and most of that was the antenna, the polarisation, the
walls and the measurement bandwidth rather than the path. Those are the node's
to measure (the sweep-calibrated POST route and the fleet feedback do that);
the model's job is the part that is the same for everyone.

Two terms do that, both in the `propagation` config section:

- **Terrestrial loss.** `model: "hata"` adds Okumura-Hata's loss in excess of
  free space for the tower's mast height, the receiver height and
  `environment` (`open`, `suburban`, `urban`; shipped `suburban`). The excess is
  never negative, so a tower in plain view is still free space away. Inputs
  outside the formula's validity (FM below 150 MHz, masts above 200 m, ranges
  under 1 km) are clamped to its edge rather than refused. `model: "free_space"`
  is the previous behaviour, one line away.
- **Under-beam derating.** A broadcast antenna's beam is a few degrees tall and
  tilted `beam_tilt_deg` below horizontal, so a receiver 3 km from a 300 m mast
  sits 6 degrees under it, where a UHF panel array is 20 dB down. The derating
  is a parabolic main lobe, `12 * (angle / beamwidth)^2`, per band from
  `vertical_beamwidth_deg`, capped at `max_underbeam_loss_db` for the null fill
  real arrays have. Height is above ground, so a hilltop mast is derated less
  than it should be, never more. A zero cap disables it.

Both apply to the direct path only. An overlay written before this section
existed gets the shipped model, not free space: nothing needs a config PUT.

## Layout

| Path | What's there |
| --- | --- |
| `app.py` | FastAPI entry point |
| `backend/routes/towers.py` | HTTP routes |
| `backend/routes/geocode.py` | `POST /api/geocode` |
| `backend/services/geocode.py` | Address lookup: Census then Nominatim, with the cache, fan-out cap and throttle |
| `backend/services/elevation.py` | Ground elevation from open-meteo, with the cache, the 100-coordinate chunking and the 429 cooldown |
| `backend/routes/feedback.py` | Feedback ingest + admin summary routes |
| `backend/models/feedback.py` | `TowerOutcome` payload model |
| `backend/services/tower_feedback.py` | Fleet feedback store (SQLite) and the `apply_feedback` correction |
| `backend/services/tower_ranking.py` | Ranking algorithm + config loader/validator |
| `backend/services/tower_scoring.py` | Bistatic detection-area model: what the rank is computed from |
| `backend/services/tower_coverage.py` | Optional n>=2 coverage-area-added scoring, injected into the ranking as a `coverage_scorer` |
| `backend/clients/fcc.py` | FCC TV/FM Query CGI client |
| `backend/clients/maprad.py` | Maprad.io broadcast-systems client |
| `backend/config/tower_config.json` | Default ranking config (image-shipped) |
| `backend/tests/` | pytest suite (176 tests); integration tests require running `capture_fixture.py` first) |
| `frontend/` | The standalone React UI (Vite). Built into the image and served by `app.py`; `npm test` / `npm run test:e2e` cover it |
| `frontend/src/surface.css` | Design tokens and the shared console idioms; the only file holding a palette value |
| `pyproject.toml`, `uv.lock` | Dependencies, their lock, and tooling config |

## UI surface

The UI is dash.retina.fm's design system with map.retina.fm's palette as the
dark theme, per `claude-shared/docs/brand/brand-guide.md`. Token names and the
light values are dash's, so a card, a chip or a micro-label reads the same here
and on the console; retina-server's `frontend/src/map-surface.css` is the third
copy of the same vocabulary. Change a shared name in dash first.

The appearance switch is dash's, ported class for class: the same three-state
`.theme-switch` radiogroup (Light / System / Dark), the same `retina.theme`
storage key, the same `ThemeContext`. The console hangs it under an
"Appearance" label inside the header's avatar menu; there is no signed-in user
on this surface, so it sits in the header bar instead. Only the placement
differs. Change it in dash first.

Three rules keep it working:

- **No palette value outside `surface.css`.** Component stylesheets and inline
  styles read custom properties, which is what lets both themes come out of one
  block each. (Marker drop shadows stay a literal black: the basemap is a light
  tile set in both themes, so they are not palette.) Leaflet vectors are the
  exception that cannot read a property at all — a `pathOptions` colour lands in
  an SVG presentation attribute, where `var()` is not substituted, so a themed
  vector takes a class, passed as a top-level `className` prop because
  react-leaflet replays `pathOptions` through `setStyle` and that drops it.
  `themeTokens.test.ts` enforces this, and that the two dark blocks stay
  identical.
- **`system` stamps no attribute.** It is the default, and the
  `prefers-color-scheme` block answers it, so the OS preference needs no
  JavaScript and keeps working when the OS changes its mind mid-session.
  Resolving it to a value and stamping that instead would pin the surface to
  whatever the OS happened to be at load. The cost is the dark palette written
  twice, once per selector, because CSS cannot share a declaration block across
  a media query boundary.
- **No inline `<script>` in `index.html`.** The edge sends `script-src 'self'`
  with no nonce or hash, so an inline block is refused in the browser and
  nowhere else: the build succeeds and the suite passes while only the deployed
  page misbehaves. Nothing needs one today — the default theme is pure CSS — and
  `test_edge_security_headers.py` keeps it that way.

`useResolvedTheme` exists for the one thing CSS cannot reach: the basemap is a
tile set chosen in JavaScript, so the map has to be told which palette is drawn.

## Dependencies

`uv.lock` pins every package CI tests and the image runs, and `uv sync` builds
`.venv` from it, dev tools included. Change a dependency in `pyproject.toml` and
relock in the same commit, with the uv the Dockerfile pins as `UV_VERSION`,
which CI also runs, so the image can always read the lock:

```bash
uv tool run --from "uv==$(sed -n 's/^ARG UV_VERSION=//p' Dockerfile)" uv lock
```

CI fails a lock that no longer matches `pyproject.toml`. Nothing relocks on its
own: `uv lock --upgrade-package <name>` moves one package deliberately.

## Tests

```bash
uv run pytest -q          # backend
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

## Fleet feedback

Nothing has ever told the ranking whether a tower it put first was any good once
a node tuned there. These endpoints are the return path: a node's Auto-Calibrate
(retina-gui's calibrator) tries up to three candidate towers and reports what
happened on each one.

Two producers post the same row shape:

- **Calibration.** One row per candidate tower the run tried, flattened from the
  entry the calibrator keeps in its run `history`:

  | Row field | Calibrator `history` entry | Notes |
  | --- | --- | --- |
  | `node_id` | the node's Mender id | `get_node_id()` in retina-gui |
  | `run_id` | one id per run, e.g. the run's start time | optional, but send it: it is what makes a retried post safe |
  | `rx_lat`, `rx_lon` | `location.rx` from the merged config | |
  | `tx_lat`, `tx_lon` | the alternate's `tx` block, or `location.tx` for the configured tower | |
  | `fc_hz` | `fc` | Hz, as the tuner has it |
  | `callsign` | `tower_name` | up to 32 characters, the node's own limit |
  | `outcome` | `outcome` | the calibrator's vocabulary, verbatim |
  | `max_evidence`, `max_detections` | same names | 0 none, 1 detections, 2 active track |
  | `duration_s` | `dwell_seconds` | |
  | `gain_a`, `gain_b`, `lna_state` | `final_gain_a`, `final_gain_b`, `final_lna_state` | absent on a `not_reached` entry, which is fine |
  | `device_error` | `device_error` | the SDR wedged rather than reporting a clean overload |

- **Archive.** A later retina-server job posts archive-derived aggregates per
  tower per window: `verified_range_p85_km`, `adsb_match_rate`, `snr_median_db`,
  `hours_observed`, with `outcome: "observed"`.

Payload (`backend/models/feedback.py`), one object or a list of up to 100:

```bash
curl -X POST https://tower-finder.retina.fm/api/feedback/tower-outcome \
  -H "Authorization: Bearer $TOWER_FINDER_FEEDBACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"node_id": "node-7", "run_id": "2026-09-11T10:00:00Z",
       "rx_lat": 34.05, "rx_lon": -118.25,
       "tx_lat": 34.23, "tx_lon": -118.06, "fc_hz": 98700000,
       "callsign": "KABC", "source": "calibration",
       "outcome": "confirmed_track", "max_evidence": 2,
       "max_detections": 41, "duration_s": 90, "gain_a": 41,
       "gain_b": 35, "lna_state": 6}'
# {"stored": 1, "ignored": 0}
```

Unknown keys are a 422 rather than being dropped, so a node that misspells a
field finds out instead of posting rows that weigh nothing. `observed_at` is
optional; the server dates the row when it is absent, and keeps `received_at`
separately so a node with a bad clock still lands something orderable.

**Retries are safe.** A node that times out on the post and tries again would
otherwise land the same run twice, and every duplicate doubles that run's
weight. Rows carrying a `run_id` are stored once per (node, run, tower); a
repeat comes back `200` with everything under `ignored`, which is the reply a
node wants: on record, stop retrying. Rows without a `run_id` (the archive job,
or an older node) are never deduplicated.

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
# Set TOWER_FINDER_FEEDBACK_TOKEN too; it is the one the nodes hold, so it is
# the one to hand to retina-node's .env, and it must not be the admin token.
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
