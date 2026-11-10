"""Tower-finding and tower-config endpoints."""

import json
import logging
import os
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from clients.fcc import fetch_fcc_broadcast_systems
from clients.maprad import MapradQueryError, fetch_broadcast_systems
from core.auth import require_admin
from models.measurements import MeasurementPayload
from services import elevation, tower_ranking
from services.elevation import ElevationUnavailable
from services.region_lookup import SUPPORTED_REGIONS, classify_region, unsupported_region_detail
from services.tower_ranking import (
    allowed_bands_for_region,
    apply_config,
    licence_record,
    parse_user_frequencies,
    process_and_rank,
    reload_config,
    validate_config,
)

router = APIRouter(prefix="/api")


@router.get("/health")
async def health():
    # Read per request, not at import: three near-identical stacks make
    # "which environment answered?" otherwise unanswerable from outside.
    return {"status": "ok", "environment": os.getenv("TOWER_FINDER_ENV", "unknown")}


API_KEY = os.getenv("MAPRAD_API_KEY", "")

# How far from the tower position it is given the callsign lookup searches:
# room for a configured position a few km off the licensed site, short of a
# namesake in the next market.
LOOKUP_RADIUS_KM = 30

# A lookup outside coverage. Its radius is fixed, so unlike the tower search's
# 422 this one has no larger radius to suggest.
_LOOKUP_UNSUPPORTED_DETAIL = (
    f"No tower data within {LOOKUP_RADIUS_KM} km of this position "
    f"(coverage: {', '.join(s.upper() for s in SUPPORTED_REGIONS)})."
)

# The callsign lookup follows retina-server's own match against a tower list:
# a frequency within this of the licensed one is the same tower, and a service
# suffix (WXXX-TV against WXXX) is not part of the name.
_LOOKUP_MAX_FREQUENCY_OFFSET_MHZ = 3.5
_CALLSIGN_SUFFIX_RE = re.compile(r"-(?:TV|DT|D|LD|LP|CD|CA|FM|HD)$")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _detect_source(lat: float, lon: float, radius_km: float, unsupported_detail: str | None = None) -> str:
    """The database to search for (lat, lon): the nearest supported region within ``radius_km``.

    ``radius_km`` is the request's effective search radius. The question is
    which database gives the best towers, not which jurisdiction the point is
    in, so a point out to sea or just over a land border is served by the
    nearest country whose territory the search area reaches.
    """
    region = classify_region(lat, lon, radius_km)
    if region is not None:
        return region
    # We only have tower data + an ATSC demod for the supported regions, and
    # none of them comes within the search radius (open ocean, another
    # continent), so there is nothing meaningful to serve. The shared detail
    # names the radius because widening it is what can change the answer; a
    # caller whose radius is fixed passes a detail of its own.
    raise HTTPException(status_code=422, detail=unsupported_detail or unsupported_region_detail(radius_km))


def _resolve_source(
    source: str, lat: float, lon: float, radius_km: float, unsupported_detail: str | None = None
) -> str:
    """Normalise + validate the requested source, resolving "auto" by geo-lookup within ``radius_km``."""
    source = source.lower()
    if source == "auto":
        source = _detect_source(lat, lon, radius_km, unsupported_detail)
    if source not in SUPPORTED_REGIONS:
        raise HTTPException(status_code=400, detail=f"Invalid source. Use: {', '.join(SUPPORTED_REGIONS)}, auto")
    return source


async def _fetch_raw_towers(source: str, lat: float, lon: float, radius_km: int) -> list:
    """Fetch raw broadcast systems for a resolved source.

    US comes from the FCC database; every other region uses Maprad. Shared by
    GET and POST /api/towers and the callsign lookup.
    """
    try:
        if source == "us":
            raw = await fetch_fcc_broadcast_systems(lat, lon, radius_km=radius_km)
        else:
            if not API_KEY:
                raise HTTPException(status_code=500, detail="MAPRAD_API_KEY not configured")
            raw = await fetch_broadcast_systems(API_KEY, lat, lon, radius_km=radius_km, source=source)
    except HTTPException:
        raise
    except MapradQueryError as exc:
        # Upstream answered and refused the query. Its own words go to the
        # caller, because "try again" is wrong advice for a query that will be
        # refused the same way every time, and the UI shows this detail as is.
        logging.warning("Maprad refused the %s query: %s", exc.source, exc.upstream_message)
        raise HTTPException(
            status_code=502,
            detail=f"Maprad rejected the {exc.source} query: {exc.upstream_message}",
        ) from None
    except Exception:
        logging.exception("Tower data fetch failed")
        raise HTTPException(status_code=502, detail="External service unavailable. Please try again.") from None
    return raw


async def _enrich_with_elevation(towers: list, query_point: tuple[float, float] | None = None) -> float | None:
    """Attach ground elevation + total altitude to each tower in place.

    ``query_point`` rides along in the same upstream request and comes back as
    the return value, so the node's own altitude and the towers' elevations
    cost one round trip rather than two.
    """
    # Leading, not trailing: the batch is chunked in order and abandoned at the
    # first chunk that fails, so putting it last would lose the node's own
    # altitude on exactly the large searches that need chunking.
    coords = [query_point] if query_point is not None else []
    coords += [(t["latitude"], t["longitude"]) for t in towers]

    try:
        elevations = await elevation.lookup_many(coords)
    except Exception:
        # Best-effort by design: the tower list is the answer here, so a fault
        # in the lookup itself must not take it down with it.
        logging.exception("Elevation enrichment failed")
        elevations = {}

    for t in towers:
        elev = elevations.get(elevation.key(t["latitude"], t["longitude"]))
        t["elevation_m"] = round(elev, 1) if elev is not None else None
        if elev is not None and t.get("antenna_height_m") is not None:
            t["altitude_m"] = round(elev + t["antenna_height_m"], 1)
        elif elev is not None:
            t["altitude_m"] = round(elev, 1)
        else:
            t["altitude_m"] = None

    return elevations.get(elevation.key(*query_point)) if query_point is not None else None


def _bare_callsign(callsign: object) -> str:
    """Uppercased first token less one service suffix, the same on both sides of the match; "" for none."""
    tokens = callsign.upper().split() if isinstance(callsign, str) else []
    return _CALLSIGN_SUFFIX_RE.sub("", tokens[0]) if tokens else ""


def _match_licence(raw: list, callsign: str, frequency_mhz: float, lat: float, lon: float) -> tuple[dict | None, str]:
    """The licence record behind ``callsign`` near (lat, lon), and how far its frequency can be trusted."""
    wanted = _bare_callsign(callsign)
    candidates = []
    # The licence rows as fetched, before ranking merges a shared transmitter
    # into one row: each partner is found here by its own callsign, which is
    # what retina-server gets by also reading a ranked row's shared_callsigns.
    for system in raw if wanted else ():
        if not (system.get("licence") or {}).get("type"):
            continue
        for device in system.get("devices") or []:
            if _bare_callsign(device.get("callsign")) != wanted:
                continue
            record = licence_record(system, device, lat, lon, LOOKUP_RADIUS_KM)
            if record is not None:
                candidates.append(record)
    if not candidates:
        return None, "not_found"

    best = min(candidates, key=lambda r: (abs(r["frequency_mhz"] - frequency_mhz), r["distance_km"]))
    offset = abs(best["frequency_mhz"] - frequency_mhz)
    return best, "matched" if offset <= _LOOKUP_MAX_FREQUENCY_OFFSET_MHZ else "frequency_mismatch"


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/towers")
async def find_towers(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    altitude: float = Query(0, ge=0),
    radius_km: int = Query(0, ge=0, le=300),
    limit: int = Query(0, ge=0, le=200),
    source: str = Query("auto"),
    frequencies: list[str] = Query(default=[]),
):
    # Before the source: "auto" picks the nearest database within the radius.
    effective_radius = radius_km if radius_km > 0 else tower_ranking.DEFAULT_RADIUS_KM
    source = _resolve_source(source, lat, lon, effective_radius)
    effective_limit = limit if limit > 0 else tower_ranking.DEFAULT_LIMIT
    # List-typed, not scalar: Starlette keeps only the last occurrence of a
    # repeated key for a scalar, silently dropping the rest. The occurrences go
    # to parse_user_frequencies as they are, so nothing here can sever a value
    # that spans what would otherwise be a join boundary.
    user_freqs = parse_user_frequencies(frequencies)

    raw = await _fetch_raw_towers(source, lat, lon, effective_radius)

    # Filled in by process_and_rank; echoed back so a client can tell which
    # ordering it got rather than inferring it from the rows.
    diagnostics: dict = {}
    towers = process_and_rank(
        raw,
        lat,
        lon,
        limit=effective_limit,
        radius_km=effective_radius,
        user_frequencies=user_freqs,
        allowed_bands=allowed_bands_for_region(source),
        diagnostics=diagnostics,
    )
    # Only when the caller gave none: an altitude they set is theirs, and an
    # elevation we cannot get leaves it standing.
    query_elev = await _enrich_with_elevation(towers, query_point=(lat, lon) if altitude == 0 else None)
    resolved_altitude = altitude if query_elev is None else query_elev

    return {
        "towers": towers,
        "query": {
            "latitude": lat,
            "longitude": lon,
            "altitude_m": resolved_altitude,
            "radius_km": effective_radius,
            "source": source,
            "user_frequencies_mhz": user_freqs,
            "ranking": diagnostics.get("ranking"),
        },
        "count": len(towers),
    }


@router.get("/towers/lookup")
async def lookup_tower(
    callsign: str = Query(..., min_length=1, max_length=32),
    frequency_mhz: float = Query(..., gt=0, lt=10000),
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    source: str = Query("auto"),
):
    """The one licence record behind a callsign at a tower position.

    ``lat``/``lon`` are the tower's own position, never a receiver's: the
    search is centred on the transmitter, and where a receiver sits stays with
    the system that holds it. The figures are what the licence states (EIRP
    and antenna height, null where it states none, never the ranking
    placeholder; polarisation only where the source gives one), from the same
    upstreams and caches as GET /api/towers, with no elevation and no ranking.
    ``status`` is ``matched`` within 3.5 MHz, ``frequency_mismatch`` with the
    nearest-frequency record otherwise, ``not_found`` with no record.
    """
    resolved = _resolve_source(source, lat, lon, LOOKUP_RADIUS_KM, _LOOKUP_UNSUPPORTED_DETAIL)
    raw = await _fetch_raw_towers(resolved, lat, lon, LOOKUP_RADIUS_KM)
    tower, status = _match_licence(raw, callsign, frequency_mhz, lat, lon)
    return {
        "status": status,
        "tower": tower,
        "query": {
            "callsign": callsign,
            "frequency_mhz": frequency_mhz,
            "latitude": lat,
            "longitude": lon,
            "radius_km": LOOKUP_RADIUS_KM,
            "source": resolved,
        },
    }


@router.post("/towers")
async def find_towers_with_measurements(payload: MeasurementPayload):
    """Tower search enriched with spectrum-analyser measurements from retina-spectrum.

    Fetches the same FCC/Maprad tower database as GET /api/towers, then matches
    each tower against the provided measurements using band-specific frequency
    tolerances.  Only towers the SDR can actually see are returned — unmatched
    towers are excluded entirely.  Matched towers carry real measured quality
    fields (``snr_db``, ``score``, ``power_db``, ``obw_fraction``, ``measured=True``).
    """
    # Before the source: "auto" picks the nearest database within the radius.
    effective_radius = payload.radius_km if payload.radius_km > 0 else tower_ranking.DEFAULT_RADIUS_KM
    source = _resolve_source(payload.source, payload.lat, payload.lon, effective_radius)
    effective_limit = payload.limit if payload.limit > 0 else tower_ranking.DEFAULT_LIMIT
    measurements = [m.model_dump() for m in payload.measurements]

    raw = await _fetch_raw_towers(source, payload.lat, payload.lon, effective_radius)

    diagnostics: dict = {}
    towers = process_and_rank(
        raw,
        payload.lat,
        payload.lon,
        limit=effective_limit,
        radius_km=effective_radius,
        measurements=measurements,
        allowed_bands=allowed_bands_for_region(source),
        diagnostics=diagnostics,
    )
    await _enrich_with_elevation(towers)

    return {
        "towers": towers,
        "query": {
            "latitude": payload.lat,
            "longitude": payload.lon,
            "radius_km": effective_radius,
            "source": source,
            "measurement_count": len(measurements),
            "ranking": diagnostics.get("ranking"),
            # Null when the sweep carried fewer than two matched TV channels:
            # the node can then tell "the model was corrected by what you sent"
            # from "there was not enough to correct it with".
            "calibration_offset_db": diagnostics.get("calibration_offset_db"),
            "calibrated_towers": diagnostics.get("calibrated_towers", 0),
        },
        "count": len(towers),
    }


@router.get("/elevation")
async def get_elevation(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    """Ground elevation at a point.

    The search form uses this to pre-fill the altitude field as coordinates
    are typed; GET /api/towers resolves altitude itself when none is given.
    """
    # 503 and 404 rather than one 502: a caller, and the post-deploy smoke,
    # must be able to tell "the dependency is down" from "this route is broken".
    # Only the service's narrow classification keeps that true; a fault of our
    # own reaches the caller as a 500, which the smoke fails on.
    try:
        elev = await elevation.lookup(lat, lon)
    except ElevationUnavailable as exc:
        raise HTTPException(status_code=503, detail="Elevation service unavailable") from exc
    if elev is None:
        raise HTTPException(status_code=404, detail="No elevation data for this point")
    return {"latitude": lat, "longitude": lon, "elevation_m": elev}


@router.get("/config")
async def get_config():
    # Dotted access, not a by-value import: tests monkeypatch
    # tower_ranking._CONFIG_PATH to a scratch path, which only takes effect on
    # a lookup made at call time against the module.
    with open(tower_ranking._CONFIG_PATH) as f:
        return json.load(f)


@router.put("/config", dependencies=[Depends(require_admin)])
async def update_config(body: dict):
    # Sanity check: config should be a reasonable size
    raw = json.dumps(body)
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail="Config too large (max 1 MB)")

    # Validate it, prove it applies, and only then write it. _CONFIG_PATH lives
    # in a persistent volume and reload_config() runs at import, so a config that
    # reaches disk without applying cleanly outlives both a restart and a
    # redeploy, recoverable only by hand inside the volume. Writing last means
    # the file only ever holds a config the running process has accepted, so
    # there is no rollback to get wrong.
    error = validate_config(body)
    if error:
        raise HTTPException(status_code=400, detail=f"Invalid config: {error}")

    try:
        apply_config(body)
    except Exception as exc:
        # validate_config has a gap. apply_config is all-or-nothing and the file
        # is still untouched, so the running config is unchanged.
        logging.exception("Config passed validation but would not apply")
        raise HTTPException(status_code=400, detail=f"Config could not be applied: {exc}") from exc

    # Written to a sibling and renamed, never opened "w" in place: a truncating
    # write that fails part-way leaves invalid JSON, and reload_config() runs at
    # import, so the next start would crash-loop on a file only reachable inside
    # the volume. os.replace is atomic within a filesystem, and the sibling
    # guarantees that.
    #
    # The fsync is load-bearing, not belt-and-braces: without it the rename can
    # outlive a host crash while the data blocks do not, and a zero-length
    # tower_config.json is the same crash-loop by another road (_load_config
    # re-seeds only when the file is absent, not when it is empty).
    #
    # Dotted access: see get_config above. A by-value import would write to the
    # path bound at import time, missing a test's monkeypatch.
    config_path = tower_ranking._CONFIG_PATH
    # Unique per request. A shared sibling name lets two writers truncate and
    # unlink each other's file mid-write; nothing serialises PUTs but the single
    # worker and this handler having no await, neither of which is a promise.
    tmp_path = config_path.with_name(f"{config_path.name}.{uuid4().hex}.tmp")
    try:
        try:
            with open(tmp_path, "w") as f:
                f.write(json.dumps(body, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, config_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    except OSError as exc:
        # The process has already taken this config but the file has not. Put the
        # two back in step by re-reading whatever is actually on disk. That read
        # can itself fail (a truncated write leaves invalid JSON behind), and the
        # response to report is the write failure either way — the settings still
        # in memory are then the rejected config, which the log says.
        logging.exception("Config applied but could not be written")
        try:
            reload_config()
        except Exception:
            logging.exception("Re-reading the config on disk failed too; in-memory settings are the unwritten config")
        raise HTTPException(status_code=500, detail=f"Config could not be written: {exc}") from exc

    # Past the replace the new config is the file, so failing to make the rename
    # durable is a warning rather than a failed write: reporting a 500 here would
    # have the caller retry a change that has already taken effect.
    try:
        dir_fd = os.open(config_path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        logging.warning("Config written, but the rename could not be made durable", exc_info=True)
    return {"status": "updated"}
