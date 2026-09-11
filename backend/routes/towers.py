"""Tower-finding and tower-config endpoints."""

import json
import logging
import os
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from clients.fcc import fetch_fcc_broadcast_systems
from clients.maprad import fetch_broadcast_systems
from core.auth import require_admin
from models.measurements import MeasurementPayload
from services import tower_ranking
from services.region_lookup import SUPPORTED_REGIONS, UNSUPPORTED_REGION_DETAIL, classify_region
from services.tower_ranking import (
    allowed_bands_for_region,
    apply_config,
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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _detect_source(lat: float, lon: float) -> str:
    region = classify_region(lat, lon)
    if region is not None:
        return region
    # Deliberate stopgap: we only have tower data + an ATSC demod for the
    # supported regions, so an unmapped location can't be served meaningfully.
    # Edge-of-country false negatives are accepted for now; revisit when
    # coverage and demod standards expand.
    raise HTTPException(status_code=422, detail=UNSUPPORTED_REGION_DETAIL)


class ElevationUnavailable(Exception):
    """The elevation dependency could not be reached, or would not answer.

    Distinct from a point it simply has no data for, which is a valid answer
    and comes back as an absent key.
    """


async def _lookup_elevation(lat: float, lon: float) -> float | None:
    result = await _batch_lookup_elevations([(lat, lon)])
    return result.get((round(lat, 6), round(lon, 6)))


async def _batch_lookup_elevations(
    coords: list[tuple[float, float]],
) -> dict[tuple[float, float], float]:
    if not coords:
        return {}
    url = "https://api.open-meteo.com/v1/elevation"
    unique = list(dict.fromkeys((round(c[0], 6), round(c[1], 6)) for c in coords))
    lats = ",".join(str(c[0]) for c in unique)
    lons = ",".join(str(c[1]) for c in unique)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"latitude": lats, "longitude": lons})
            resp.raise_for_status()
            data = resp.json()
            elevations = data.get("elevation", [])
            result = {}
            for i, coord in enumerate(unique):
                if i < len(elevations) and elevations[i] is not None:
                    result[coord] = float(elevations[i])
            return result
    # Narrow deliberately: a transport fault, a 5xx or 429, or a body that will
    # not read as numbers is open-meteo's failure. Anything else is a fault in
    # the code above and must not be dressed up as the dependency being down,
    # which the post-deploy smoke passes on.
    except (httpx.HTTPError, ValueError) as exc:
        # A 4xx is open-meteo rejecting the request we built, which is ours to
        # answer for: it must reach the caller as a 500. 429 is the exception,
        # being its rate limit rather than anything wrong with the request.
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status < 500 and status != 429:
                raise
        logging.warning("Batch elevation lookup failed: %s", exc)
        raise ElevationUnavailable(str(exc)) from exc


def _resolve_source(source: str, lat: float, lon: float) -> str:
    """Normalise + validate the requested source, resolving "auto" by geo-lookup."""
    source = source.lower()
    if source == "auto":
        source = _detect_source(lat, lon)
    if source not in SUPPORTED_REGIONS:
        raise HTTPException(status_code=400, detail=f"Invalid source. Use: {', '.join(SUPPORTED_REGIONS)}, auto")
    return source


async def _fetch_raw_towers(source: str, lat: float, lon: float, radius_km: int) -> list:
    """Fetch raw broadcast systems for a resolved source.

    US pulls the FCC database and optionally supplements it with Maprad; every
    other region uses Maprad alone. Shared by GET and POST /api/towers.
    """
    try:
        if source == "us":
            raw = await fetch_fcc_broadcast_systems(lat, lon, radius_km=radius_km)
            if API_KEY:
                try:
                    maprad_raw = await fetch_broadcast_systems(API_KEY, lat, lon, radius_km=radius_km, source=source)
                    raw.extend(maprad_raw)
                except Exception:
                    logging.warning("Maprad supplement failed, using FCC data only")
        else:
            if not API_KEY:
                raise HTTPException(status_code=500, detail="MAPRAD_API_KEY not configured")
            raw = await fetch_broadcast_systems(API_KEY, lat, lon, radius_km=radius_km, source=source)
    except HTTPException:
        raise
    except Exception:
        logging.exception("Tower data fetch failed")
        raise HTTPException(status_code=502, detail="External service unavailable. Please try again.") from None
    return raw


async def _enrich_with_elevation(towers: list) -> None:
    """Attach ground elevation + total altitude to each tower in place."""
    tower_coords = [(t["latitude"], t["longitude"]) for t in towers]
    try:
        elevations = await _batch_lookup_elevations(tower_coords)
    except ElevationUnavailable:
        elevations = {}
    except Exception:
        # Best-effort by design: the tower list is the answer here, so a fault
        # in the lookup itself must not take it down with it.
        logging.exception("Elevation enrichment failed")
        elevations = {}
    for t in towers:
        key = (round(t["latitude"], 6), round(t["longitude"], 6))
        elev = elevations.get(key)
        t["elevation_m"] = round(elev, 1) if elev is not None else None
        if elev is not None and t.get("antenna_height_m") is not None:
            t["altitude_m"] = round(elev + t["antenna_height_m"], 1)
        elif elev is not None:
            t["altitude_m"] = round(elev, 1)
        else:
            t["altitude_m"] = None


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
    source = _resolve_source(source, lat, lon)

    effective_radius = radius_km if radius_km > 0 else tower_ranking.DEFAULT_RADIUS_KM
    effective_limit = limit if limit > 0 else tower_ranking.DEFAULT_LIMIT
    # List-typed, not scalar: Starlette keeps only the last occurrence of a
    # repeated key for a scalar, silently dropping the rest. The occurrences go
    # to parse_user_frequencies as they are, so nothing here can sever a value
    # that spans what would otherwise be a join boundary.
    user_freqs = parse_user_frequencies(frequencies)

    raw = await _fetch_raw_towers(source, lat, lon, effective_radius)

    resolved_altitude = altitude
    if altitude == 0:
        # Best-effort, as in _enrich_with_elevation: an elevation we cannot get
        # leaves the caller's own altitude standing, whoever's fault it was.
        try:
            elev = await _lookup_elevation(lat, lon)
        except ElevationUnavailable:
            elev = None
        except Exception:
            logging.exception("Elevation lookup failed")
            elev = None
        if elev is not None:
            resolved_altitude = elev

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
    await _enrich_with_elevation(towers)

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


@router.post("/towers")
async def find_towers_with_measurements(payload: MeasurementPayload):
    """Tower search enriched with spectrum-analyser measurements from retina-spectrum.

    Fetches the same FCC/Maprad tower database as GET /api/towers, then matches
    each tower against the provided measurements using band-specific frequency
    tolerances.  Only towers the SDR can actually see are returned — unmatched
    towers are excluded entirely.  Matched towers carry real measured quality
    fields (``snr_db``, ``score``, ``power_db``, ``obw_fraction``, ``measured=True``).
    """
    source = _resolve_source(payload.source, payload.lat, payload.lon)

    effective_radius = payload.radius_km if payload.radius_km > 0 else tower_ranking.DEFAULT_RADIUS_KM
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
    # Only _batch_lookup_elevations' narrow classification keeps that true; a
    # fault of our own reaches the caller as a 500, which the smoke fails on.
    try:
        elev = await _lookup_elevation(lat, lon)
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
