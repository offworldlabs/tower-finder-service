"""Address lookup for the search box.

Unauthenticated like the rest of the read endpoints: it exposes nothing but two
public geocoders, and the guards that matter (a fan-out cap, a cache and the
Nominatim throttle) live in the service rather than in an auth dependency.
"""

from fastapi import APIRouter, HTTPException

from models.geocode import GeocodeRequest, GeocodeResponse
from services.geocode import GeocodeUnavailable, geocode

router = APIRouter(prefix="/api")


@router.post("/geocode", response_model=GeocodeResponse)
async def geocode_address(payload: GeocodeRequest) -> GeocodeResponse:
    """Resolve an address, place name or ZIP to a point.

    503 and 404 rather than one 502, as /api/elevation does it: "we could not
    ask" and "nobody knows that address" are different facts, and the caller —
    a search box that must decide between "try again" and "check the
    spelling" — has to be able to tell them apart.
    """
    try:
        result = await geocode(payload.query)
    except GeocodeUnavailable as exc:
        raise HTTPException(status_code=503, detail="Address lookup is unavailable right now") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="No match for that address")
    return GeocodeResponse(
        query=payload.query,
        latitude=result.latitude,
        longitude=result.longitude,
        matched_address=result.matched_address,
        provider=result.provider,
        precision=result.precision,
    )
