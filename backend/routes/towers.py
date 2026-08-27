"""Tower-finding and tower-config endpoints."""

import json
import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from clients.fcc import fetch_fcc_broadcast_systems
from clients.maprad import fetch_broadcast_systems
from core.auth import require_admin
from models.measurements import MeasurementPayload
from services import tower_ranking
from services.region_lookup import SUPPORTED_REGIONS, UNSUPPORTED_REGION_DETAIL, classify_region
from services.tower_ranking import (
    _CONFIG_PATH,
    _MAX_FREQUENCY_PARTS,
    allowed_bands_for_region,
    apply_config,
    parse_user_frequencies,
    process_and_rank,
    reload_config,
    validate_config,
)

router = APIRouter()


@router.get("/api/health")
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
    except Exception as exc:
        logging.warning("Batch elevation lookup failed: %s", exc)
        return {}


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
    elevations = await _batch_lookup_elevations(tower_coords)
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


@router.get("/api/towers")
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
    # Every occurrence of a repeated `frequencies` key counts (Starlette keeps
    # only the last one for a scalar-typed param, which is why this is
    # list-typed), joined back into the comma-separated form
    # parse_user_frequencies already understands. Slicing first bounds what our
    # own handler does with the list; it cannot bound Starlette's own
    # query-string parsing, which has already built the full list by the time
    # this line runs. That cost is paid upstream and is not capped from here.
    user_freqs = parse_user_frequencies(",".join(frequencies[:_MAX_FREQUENCY_PARTS]))

    raw = await _fetch_raw_towers(source, lat, lon, effective_radius)

    resolved_altitude = altitude
    if altitude == 0:
        elev = await _lookup_elevation(lat, lon)
        if elev is not None:
            resolved_altitude = elev

    towers = process_and_rank(
        raw,
        lat,
        lon,
        limit=effective_limit,
        radius_km=effective_radius,
        user_frequencies=user_freqs,
        allowed_bands=allowed_bands_for_region(source),
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
        },
        "count": len(towers),
    }


@router.post("/api/towers")
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

    towers = process_and_rank(
        raw,
        payload.lat,
        payload.lon,
        limit=effective_limit,
        radius_km=effective_radius,
        measurements=measurements,
        allowed_bands=allowed_bands_for_region(source),
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
        },
        "count": len(towers),
    }


@router.get("/api/elevation")
async def get_elevation(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    """Ground elevation at a point.

    The search form uses this to pre-fill the altitude field as coordinates
    are typed; GET /api/towers resolves altitude itself when none is given.
    """
    elev = await _lookup_elevation(lat, lon)
    if elev is None:
        raise HTTPException(status_code=502, detail="Elevation lookup failed")
    return {"latitude": lat, "longitude": lon, "elevation_m": elev}


@router.get("/api/config")
async def get_config():
    with open(_CONFIG_PATH) as f:
        return json.load(f)


@router.put("/api/config", dependencies=[Depends(require_admin)])
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

    try:
        with open(_CONFIG_PATH, "w") as f:
            f.write(json.dumps(body, indent=2))
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
    return {"status": "updated"}
