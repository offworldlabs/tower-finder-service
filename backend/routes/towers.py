"""Tower-finding and tower-config endpoints."""

import json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Query

from clients.fcc import fetch_fcc_broadcast_systems
from clients.maprad import fetch_broadcast_systems
from models.measurements import MeasurementPayload
from services.region_lookup import classify_region
from services.tower_ranking import (
    _CONFIG_PATH,
    DEFAULT_LIMIT,
    DEFAULT_RADIUS_KM,
    process_and_rank,
    reload_config,
)

router = APIRouter()


@router.get("/api/health")
async def health():
    return {"status": "ok"}


API_KEY = os.getenv("MAPRAD_API_KEY", "")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _detect_source(lat: float, lon: float) -> str:
    region = classify_region(lat, lon)
    if region is not None:
        return region
    return "us"


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


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/api/towers")
async def find_towers(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    altitude: float = Query(0, ge=0),
    radius_km: int = Query(0, ge=0, le=300),
    limit: int = Query(0, ge=0, le=200),
    source: str = Query("auto"),
):
    source = source.lower()
    if source == "auto":
        source = _detect_source(lat, lon)
    if source not in ("us", "au", "ca"):
        raise HTTPException(status_code=400, detail="Invalid source. Use: us, au, ca, auto")

    effective_radius = radius_km if radius_km > 0 else DEFAULT_RADIUS_KM
    effective_limit = limit if limit > 0 else DEFAULT_LIMIT

    try:
        if source == "us":
            raw = await fetch_fcc_broadcast_systems(lat, lon, radius_km=effective_radius)
            if API_KEY:
                try:
                    maprad_raw = await fetch_broadcast_systems(
                        API_KEY,
                        lat,
                        lon,
                        radius_km=effective_radius,
                        source=source,
                    )
                    raw.extend(maprad_raw)
                except Exception:
                    logging.warning("Maprad supplement failed, using FCC data only")
        else:
            if not API_KEY:
                raise HTTPException(status_code=500, detail="MAPRAD_API_KEY not configured")
            raw = await fetch_broadcast_systems(
                API_KEY,
                lat,
                lon,
                radius_km=effective_radius,
                source=source,
            )
    except HTTPException:
        raise
    except Exception:
        logging.exception("Tower data fetch failed")
        raise HTTPException(status_code=502, detail="External service unavailable. Please try again.") from None

    resolved_altitude = altitude
    if altitude == 0:
        elev = await _lookup_elevation(lat, lon)
        if elev is not None:
            resolved_altitude = elev

    towers = process_and_rank(raw, lat, lon, limit=effective_limit, radius_km=effective_radius)

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

    return {
        "towers": towers,
        "query": {
            "latitude": lat,
            "longitude": lon,
            "altitude_m": resolved_altitude,
            "radius_km": effective_radius,
            "source": source,
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
    source = payload.source.lower()
    if source == "auto":
        source = _detect_source(payload.lat, payload.lon)
    if source not in ("us", "au", "ca"):
        raise HTTPException(status_code=400, detail="Invalid source. Use: us, au, ca, auto")

    effective_radius = payload.radius_km if payload.radius_km > 0 else DEFAULT_RADIUS_KM
    effective_limit = payload.limit if payload.limit > 0 else DEFAULT_LIMIT
    measurements = [m.model_dump() for m in payload.measurements]

    try:
        if source == "us":
            raw = await fetch_fcc_broadcast_systems(
                payload.lat,
                payload.lon,
                radius_km=effective_radius,
            )
            if API_KEY:
                try:
                    maprad_raw = await fetch_broadcast_systems(
                        API_KEY,
                        payload.lat,
                        payload.lon,
                        radius_km=effective_radius,
                        source=source,
                    )
                    raw.extend(maprad_raw)
                except Exception:
                    logging.warning("Maprad supplement failed, using FCC data only")
        else:
            if not API_KEY:
                raise HTTPException(status_code=500, detail="MAPRAD_API_KEY not configured")
            raw = await fetch_broadcast_systems(
                API_KEY,
                payload.lat,
                payload.lon,
                radius_km=effective_radius,
                source=source,
            )
    except HTTPException:
        raise
    except Exception:
        logging.exception("Tower data fetch failed")
        raise HTTPException(
            status_code=502,
            detail="External service unavailable. Please try again.",
        ) from None

    towers = process_and_rank(
        raw,
        payload.lat,
        payload.lon,
        limit=effective_limit,
        radius_km=effective_radius,
        measurements=measurements,
    )

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


@router.get("/api/config")
async def get_config():
    with open(_CONFIG_PATH) as f:
        return json.load(f)


@router.put("/api/config")
async def update_config(body: dict):
    # Sanity check: config should be a reasonable size
    raw = json.dumps(body)
    if len(raw) > 1_000_000:
        raise HTTPException(status_code=413, detail="Config too large (max 1 MB)")
    with open(_CONFIG_PATH, "w") as f:
        f.write(json.dumps(body, indent=2))
    reload_config()
    return {"status": "updated"}
