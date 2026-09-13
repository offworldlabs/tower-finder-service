"""Address → coordinates for the search box.

Two upstreams, tried in order. The US Census geocoder is authoritative for
street addresses and has no usage policy to honour, but it answers nothing at
all for a city or a bare ZIP, so Nominatim backs it up for those. Neither is
keyed, which is why the whole lookup sits behind a small semaphore and a cache:
the endpoint is unauthenticated, and a burst of it must not turn into a burst
against someone else's free service.

The query text is deliberately absent from every log line here. An address
typed into a public search box is the closest thing this service handles to
personal data, and the outcome plus the provider is all an operator needs.
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# One line to widen. Nominatim takes a comma-separated list, so "us,ca,au"
# follows the day tower data does.
NOMINATIM_COUNTRY_CODES = "us"

CONTACT_ENV_VAR = "TOWER_FINDER_GEOCODER_CONTACT"
_DEFAULT_CONTACT = "https://github.com/offworldlabs/tower-finder-service"

_TIMEOUT_S = 10.0
# Nominatim's usage policy: at most one request per second from one source, and
# an identifying User-Agent. Both are conditions of being allowed to use it at
# all, not politeness.
_NOMINATIM_MIN_INTERVAL_S = 1.0
_CACHE_TTL_S = 24 * 60 * 60
_CACHE_MAX_ENTRIES = 1000
_MAX_CONCURRENT_LOOKUPS = 4


class GeocodeUnavailable(Exception):
    """No upstream could be reached, or none would answer usefully.

    Distinct from an address neither of them knows, which is a valid answer and
    comes back as None.
    """


@dataclass(frozen=True)
class GeocodeResult:
    latitude: float
    longitude: float
    matched_address: str
    provider: str
    precision: str


# ── Throttle, fan-out cap and cache ──────────────────────────────────────────

# The lookup as a whole, not the individual calls: an unauthenticated endpoint
# is the only thing between the public internet and two free upstreams.
_fanout = asyncio.Semaphore(_MAX_CONCURRENT_LOOKUPS)

_nominatim_lock = asyncio.Lock()
_nominatim_last_call: float | None = None

# key -> (expires_at_monotonic, result). Insertion-ordered, so the oldest entry
# is the first key. A definitive "nobody knows this address" is cached as None:
# it costs the same two round trips to re-derive as a match does.
_cache: dict[str, tuple[float, GeocodeResult | None]] = {}
_MISS = object()


def _cache_key(query: str) -> str:
    """Case-folded and whitespace-collapsed, so the same address typed three
    ways is one upstream call rather than three."""
    return " ".join(query.casefold().split())


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry is None:
        return _MISS
    expires_at, result = entry
    if expires_at <= time.monotonic():
        _cache.pop(key, None)
        return _MISS
    return result


def _cache_put(key: str, result: GeocodeResult | None) -> None:
    # Re-inserting moves the key to the end, so a refreshed entry is not the
    # next one evicted.
    _cache.pop(key, None)
    _cache[key] = (time.monotonic() + _CACHE_TTL_S, result)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))


async def _throttle_nominatim() -> None:
    """Hold every caller to one Nominatim request per second, process-wide.

    The sleep happens under the lock on purpose: released first, N waiting
    requests would all see the same stale timestamp and leave together.
    """
    global _nominatim_last_call
    async with _nominatim_lock:
        if _nominatim_last_call is not None:
            wait = _NOMINATIM_MIN_INTERVAL_S - (time.monotonic() - _nominatim_last_call)
            if wait > 0:
                await asyncio.sleep(wait)
        _nominatim_last_call = time.monotonic()


def _user_agent() -> str:
    # Read per request rather than bound at import, as core.auth does with its
    # token: it keeps the value honest when the environment changes under us.
    contact = os.getenv(CONTACT_ENV_VAR, "").strip() or _DEFAULT_CONTACT
    return f"tower-finder-service ({contact})"


# ── Providers ────────────────────────────────────────────────────────────────
#
# Each returns a GeocodeResult, or None for "reached it, it knows nothing", and
# raises GeocodeUnavailable when it could not be reached or would not parse.


def _malformed(provider: str, exc: Exception) -> ValueError:
    """Re-label a payload that is not the documented shape.

    Shape errors are converted at the point of parsing rather than caught
    wholesale around the request, so that a KeyError raised by a bug in this
    module still reaches the caller as a 500 instead of being reported as the
    upstream being down. The elevation lookup keeps the same line.
    """
    return ValueError(f"unexpected {provider} payload: {type(exc).__name__}")


def _parse_census(payload) -> GeocodeResult | None:
    try:
        matches = payload["result"]["addressMatches"]
        if not matches:
            return None
        match = matches[0]
        coords = match["coordinates"]
        return GeocodeResult(
            latitude=round(float(coords["y"]), 6),
            longitude=round(float(coords["x"]), 6),
            # Always a street match: the geocoder answers nothing at all for a
            # city or a bare ZIP, which is the reason Nominatim follows it.
            matched_address=str(match["matchedAddress"]),
            provider="census",
            precision="street",
        )
    except (KeyError, TypeError, IndexError) as exc:
        raise _malformed("Census", exc) from exc


async def _census(query: str) -> GeocodeResult | None:
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {"address": query, "benchmark": "Public_AR_Current", "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return _parse_census(resp.json())
    # Narrow deliberately, as _batch_lookup_elevations is: a transport fault, a
    # bad status, or a body that will not read as numbers is the upstream's
    # failure. Anything else is a fault in the code above and must reach the
    # caller as a 500 rather than be dressed up as a dependency being down.
    #
    # Unlike the elevation lookup this does not re-raise a 4xx. There the
    # request is built from two validated floats, so a rejection is ours to
    # answer for; here it carries free text from a public box, and a 400
    # provoked by somebody's punctuation is not a broken route.
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Census geocode lookup failed: %s", type(exc).__name__)
        raise GeocodeUnavailable(str(exc)) from exc


def _parse_nominatim(payload) -> GeocodeResult | None:
    try:
        if not payload:
            return None
        item = payload[0]
        return GeocodeResult(
            # Strings on the wire, unlike Census's numbers.
            latitude=round(float(item["lat"]), 6),
            longitude=round(float(item["lon"]), 6),
            matched_address=str(item["display_name"]),
            provider="nominatim",
            precision=_nominatim_precision(item),
        )
    except (KeyError, TypeError, IndexError) as exc:
        raise _malformed("Nominatim", exc) from exc


def _nominatim_precision(item: dict) -> str:
    kind = str(item.get("addresstype") or item.get("type") or "").lower()
    if kind == "postcode":
        return "postcode"
    address = item.get("address") or {}
    if isinstance(address, dict) and ("house_number" in address or "road" in address):
        return "street"
    return "locality"


async def _nominatim(query: str) -> GeocodeResult | None:
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": query,
        "format": "jsonv2",
        "countrycodes": NOMINATIM_COUNTRY_CODES,
        "limit": 1,
        "addressdetails": 1,
    }
    await _throttle_nominatim()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.get(url, params=params, headers={"User-Agent": _user_agent()})
            resp.raise_for_status()
            return _parse_nominatim(resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Nominatim geocode lookup failed: %s", type(exc).__name__)
        raise GeocodeUnavailable(str(exc)) from exc


# A sequence, not two hard-coded calls, so a keyed provider can be slotted in
# ahead of these without the route learning about it.
PROVIDERS: Sequence[Callable[[str], Awaitable[GeocodeResult | None]]] = (_census, _nominatim)


# ── Entry point ──────────────────────────────────────────────────────────────


async def geocode(query: str) -> GeocodeResult | None:
    """Resolve an address to a point, or None if no provider knows it.

    Raises GeocodeUnavailable when a provider could not be reached and none of
    the others matched — "we could not look" and "there is nothing there" are
    different answers, and only the second is worth caching or reporting as a
    404.
    """
    key = _cache_key(query)
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]

    async with _fanout:
        # Re-checked after the wait: while this request queued, another may have
        # asked the same thing and filled the entry.
        cached = _cache_get(key)
        if cached is not _MISS:
            return cached  # type: ignore[return-value]

        unreachable = False
        for provider in PROVIDERS:
            try:
                result = await provider(query)
            except GeocodeUnavailable:
                unreachable = True
                continue
            if result is not None:
                _cache_put(key, result)
                logger.info("Geocode matched via %s (%s)", result.provider, result.precision)
                return result

        # A no-match is only definitive when every provider actually answered.
        # With one of them down the address may well exist, so this is an
        # outage rather than a 404, and nothing about it is cached.
        if unreachable:
            logger.warning("Geocode unavailable: no provider answered usefully")
            raise GeocodeUnavailable("no provider could be reached")

        _cache_put(key, None)
        logger.info("Geocode found no match")
        return None
