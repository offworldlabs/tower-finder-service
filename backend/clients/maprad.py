"""
Maprad.io broadcast-systems client for AU and CA tower data.

Maprad's GraphQL API is metered per query, and one search fans out into a
query per broadcast subtype, each walking up to three pages at ~2.4 s a page:
a Toronto search costs ~7 s and half a dozen billed queries. So answers are
held in-process for a day (see "Result cache" below), keyed on the source,
the radius, the page budget and the point rounded to two decimals (~1 km).
The point is rounded before it is sent as well, so what the cache holds for a
key is exactly what upstream answers for it, whichever caller asked first.
Only a successful answer is held: a refused query (MapradQueryError) or an
unreachable upstream is asked again by the next search. Two identical
searches in flight at once share one upstream walk.
"""

import asyncio
import json
import logging
import time

import httpx

log = logging.getLogger(__name__)

MAPRAD_URL = "https://maprad.io/api"


class MapradQueryError(RuntimeError):
    """maprad.io answered, but with a GraphQL error instead of data.

    The API reports a rejected query (no read access to the source, an unknown
    field, a page size over its limit) inside an HTTP 200. Swallowed, that
    reads exactly like a location with no stations, which is how every
    Canadian search came to answer "0 towers" without anyone seeing why.
    """

    def __init__(self, source: str, upstream_message: str):
        self.source = source
        self.upstream_message = upstream_message
        super().__init__(f"Maprad rejected the {source} query: {upstream_message}")


# maprad.io refuses a larger page outright rather than clamping it, and a
# refusal arrives as a GraphQL error, which fails the walk below. So this is
# the API's ceiling, not a preference.
_PAGE_SIZE = 30

# Kept minimal: the API is prone to internal errors on large responses, and
# the walk runs at the maximum page size, so there is no headroom to spend.
_DEVICE_FIELDS = """
          callsign
          frequency(unit: MHz)
          eirp
          transmitPower
          antennaHeight
          location { name state geom }
"""

# Template for querying a specific licence_subtype.
_SUBTYPE_QUERY = """
query {{
  systems(
    first: {page_size}
    after: "{cursor}"
    source: "{source}"
    geoFilter: {{ type: CIRCLE, values: ["{coords}", "{radius}"] }}
    filter: [
      {{ field: licence_subtype, values: "{subtype}" }}
    ]
  ) {{
    edges {{
      cursor
      node {{
        id
        devices {{{devices}}}
        licence {{ type subtype }}
      }}
    }}
    pageInfo {{ hasNextPage }}
  }}
}}
""".strip()

# Broadcast licence subtypes worth asking for, per source. Each regulator has
# its own vocabulary, and a subtype the source does not use matches nothing
# and comes back as an empty result rather than an error.
_BROADCAST_SUBTYPES = {
    # ACMA RRL. High-power, POINT geometries. Retransmission / Community
    # Broadcasting omitted: often low-power or return enormous MULTIPOLYGON
    # coverage geometries that slow down the API response.
    "au": [
        "Commercial Television",
        "National Broadcasting",
        "Commercial Radio",
    ],
    # ISED SMS, where every broadcast record carries licence type "Broadcast"
    # and one of: FM, AM, DTV, Low Power FM, Low Power DTV, Analog TV,
    # Multipoint Distribution Television, Terrestrial Satellite Digital Audio
    # Radio Service, HF Broadcasting or Shortwave Radio. Read from maprad.io's
    # own browse facets for the CA source (2026-09-22). FM and DTV are the
    # illuminators the node can use; AM, satellite radio and shortwave sit
    # outside every band the ranking serves, analog TV cannot be demodulated
    # by an ATSC node, and the low-power classes are left out for the same
    # reason AU's narrowcasting is.
    "ca": [
        "FM",
        "DTV",
    ],
}

# Canada only: asked when the subtype queries above answered but found nothing
# between them (every one failing raises instead). Rather than trusting one spelling of each subtype, it takes every
# "Broadcast" licence whose device sits between the bottom of VHF-TV and the
# top of the old UHF-TV band, which keeps FM and TV of any subtype and drops
# AM, satellite radio and shortwave. It costs one more metered query, and only
# where the subtype queries came back empty, so a vocabulary change upstream
# shows up as a slower search rather than as an empty map.
#
# Not a transmit-power floor: Maprad offers no EIRP filter field, and the CA
# records' transmitPower is missing on major stations (CIUT-FM, CICA-DT) and
# nonsense on others (CFMZ-FM carries 1 W), so a floor on it drops the towers
# this exists to find.
_CA_FALLBACK_QUERY = """
query {{
  systems(
    first: {page_size}
    after: "{cursor}"
    source: "{source}"
    geoFilter: {{ type: CIRCLE, values: ["{coords}", "{radius}"] }}
    filter: [
      {{ field: licence_type, values: "Broadcast" }}
      {{ field: device_frequency, type: RANGE, values: ["54000000", "698000000"] }}
    ]
  ) {{
    edges {{
      cursor
      node {{
        id
        devices {{{devices}}}
        licence {{ type subtype }}
      }}
    }}
    pageInfo {{ hasNextPage }}
  }}
}}
""".strip()

# The whole of what Maprad can answer for: its US dataset is the FCC ULS
# licence system, which holds no broadcast stations.
_SUPPORTED_SOURCES = set(_BROADCAST_SUBTYPES)

# Longest stretch of upstream error text carried into a log line or an HTTP
# detail. Enough for any message seen so far; a stack trace is cut short.
_MAX_ERROR_TEXT = 300


def _error_text(errors) -> str:
    """The upstream's own messages, joined, from a GraphQL ``errors`` array."""
    if not isinstance(errors, list):
        errors = [errors]
    messages = []
    for err in errors:
        message = err.get("message") if isinstance(err, dict) else None
        messages.append(str(message if message else err))
    text = "; ".join(messages) or "unspecified GraphQL error"
    return text if len(text) <= _MAX_ERROR_TEXT else text[: _MAX_ERROR_TEXT - 3] + "..."


def _ca_eirp_to_watts(systems: list[dict]) -> None:
    """Rewrite each CA device's ``eirp`` from dBW to watts, in place.

    Maprad's CA records carry ISED's ERP in dBW under a "W" label: CKFM-FM
    reads 45.58 for a station licensed at ~36 kW, CHCH-DT reads 49.7 beside a
    7,175 W transmitter. The API returns the stored figure whatever unit is
    asked for (production AU answers match maprad.io's index to the decimal),
    and the ranking reads ``eirp`` as watts, so without this every Canadian
    tower would be ranked some 30 dB weak and the distant ones culled by the
    sensitivity floor. The raw figure is kept as ``eirp_dbw``.
    """
    for system in systems:
        for device in system.get("devices") or []:
            raw = device.get("eirp")
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            device["eirp_dbw"] = raw
            device["eirp"] = 10 ** (raw / 10)


async def _paginate_query(
    client: httpx.AsyncClient,
    headers: dict,
    template: str,
    fmt_kwargs: dict,
    max_pages: int,
    page_size: int,
    label: str | None = None,
) -> list[dict]:
    """Run a single paginated query, returning collected system nodes.

    A GraphQL error before anything was collected raises MapradQueryError: the
    query was refused, and an empty list would claim the area holds nothing.
    One that arrives after earlier pages keeps what they gave, and says so.
    """
    label = label or fmt_kwargs.get("subtype") or "query"
    source = fmt_kwargs.get("source", "?")
    systems: list[dict] = []
    cursor = ""
    for page in range(max_pages):
        query = template.format(cursor=cursor, page_size=page_size, **fmt_kwargs)
        resp = await client.post(MAPRAD_URL, json={"query": query}, headers=headers)
        resp.raise_for_status()
        body = resp.json()

        if body.get("errors"):
            text = _error_text(body["errors"])
            if not systems:
                raise MapradQueryError(source, text)
            log.warning(
                "Maprad %s %s: GraphQL error on page %d, keeping the %d system(s) already collected: %s",
                source,
                label,
                page + 1,
                len(systems),
                text,
            )
            break

        data = (body.get("data") or {}).get("systems") or {}
        edges = data.get("edges") or []
        prev_cursor = cursor
        for edge in edges:
            node = edge.get("node")
            if node:
                systems.append(node)
            cursor = edge.get("cursor", cursor)

        if not data.get("pageInfo", {}).get("hasNextPage") or not edges:
            break
        # Defensive: if the API says "hasNextPage" but the cursor didn't advance
        # (e.g. missing cursors on edges, or API quirk), stop to avoid re-fetching
        # the same page indefinitely up to max_pages.
        if cursor == prev_cursor:
            log.warning("Pagination cursor did not advance on page %d — stopping", page + 1)
            break
    else:
        # Every page spent while the API still had more to give. Said out loud
        # because the shortfall is otherwise indistinguishable from a location
        # that simply holds fewer stations.
        log.warning(
            "Subtype %s hit the %d-page budget with more available; returning %d system(s)",
            label,
            max_pages,
            len(systems),
        )
    return systems


# ---------------------------------------------------------------------------
# Result cache
# ---------------------------------------------------------------------------

# Licence records move on the order of weeks, so a day-old answer is the same
# answer, and every search or retry inside the day is one nobody pays for.
_CACHE_TTL_S = 24 * 60 * 60

# A backstop against unbounded growth, not a working limit: nodes do not move,
# so a day's AU and CA searches come back to a few dozen points. Eviction is by
# insertion order (expired entries go as they are met), as in clients/fcc.py.
_CACHE_MAX_ENTRIES = 120

# Two decimals of a degree is ~1.1 km of latitude (less of longitude away from
# the equator), well inside what the search can tell apart: the query radius
# is tens of km and the ranking scores towers on distance at a coarser grain.
_COORD_DECIMALS = 2

_CacheKey = tuple[str, int, int, float, float]

# The cache's clock, by name, so a test can move it without moving
# time.monotonic itself, which the event loop schedules every sleep by.
_clock = time.monotonic

# (source, radius_km, max_pages, lat, lon) -> (expires_at_monotonic, JSON
# text). Text rather than the list itself, as clients/fcc.py holds response
# bodies: a list handed out of a day-long cache would be reachable, and so
# mutable, by every caller that has ever held it, so any caller annotating or
# trimming its records (now or later) would change the next search's answer.
# A str cannot be changed in place, json.loads (C) hands
# each caller its own copy several times faster than copy.deepcopy (Python)
# would, and the records are JSON from upstream to begin with, floats
# included, so the round trip is exact. The stored list is the one after the
# CA dBW->W conversion, so a hit is never converted a second time.
_cache: dict[_CacheKey, tuple[float, str]] = {}

# Walks under way, by key, so a second identical search (a retry, a double
# click, two tabs) joins the first rather than paying for the same pages. A
# Task, not a bare Future: it runs to completion even if the caller that
# started it goes away, and the others are awaiting it through a shield.
_in_flight: dict[_CacheKey, asyncio.Task] = {}


def clear_cache() -> None:
    """Forget every held answer (and any walk in flight). For tests and operators."""
    _cache.clear()
    _in_flight.clear()


def _cache_get(key: _CacheKey) -> str | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, text = entry
    if expires_at <= _clock():
        _cache.pop(key, None)
        return None
    return text


def _cache_put(key: _CacheKey, text: str) -> None:
    # Re-inserting moves the key to the end, so a refreshed entry is not the
    # next one evicted.
    _cache.pop(key, None)
    now = _clock()
    _cache[key] = (now + _CACHE_TTL_S, text)
    # Anything already expired goes first; only then the oldest live entry.
    if len(_cache) > _CACHE_MAX_ENTRIES:
        for stale in [k for k, (expires_at, _) in _cache.items() if expires_at <= now]:
            del _cache[stale]
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))


async def fetch_broadcast_systems(
    api_key: str,
    lat: float,
    lon: float,
    radius_km: int = 80,
    *,
    source: str,
    max_pages: int = 3,
) -> list[dict]:
    """
    Broadcast transmitters near (lat, lon) from Maprad.io, through the cache.

    The point is rounded to ~1 km, for the key and for the query alike. Every
    call gets a list of its own to annotate. See _fetch_upstream for what is
    asked and what raises; nothing that raises is held.
    """
    # Maprad's source keys are lowercase, and this value reaches the query as
    # well as the guard: folding it in only one of the two asks upstream for a
    # key that matches nothing, which comes back empty rather than raising.
    # Folded before the key too, so "CA" and "ca" share an entry.
    source = source.lower()
    # US searches belong to clients/fcc.py.
    if source not in _SUPPORTED_SOURCES:
        raise ValueError(f"Maprad holds no broadcast data for source {source!r}")

    lat = round(lat, _COORD_DECIMALS)
    lon = round(lon, _COORD_DECIMALS)
    key = (source, radius_km, max_pages, lat, lon)

    text = _cache_get(key)
    if text is not None:
        log.debug("Maprad %s cache hit near %s,%s (radius %s km)", source, lat, lon, radius_km)
        return json.loads(text)

    task = _in_flight.get(key)
    # A task from another event loop (one a test tore down mid-walk) cannot
    # be awaited from this one; it is simply replaced.
    if task is not None and task.get_loop() is asyncio.get_running_loop():
        log.debug("Maprad %s cache miss near %s,%s; joining the walk in flight", source, lat, lon)
    else:
        log.debug("Maprad %s cache miss near %s,%s (radius %s km)", source, lat, lon, radius_km)
        task = asyncio.ensure_future(_fetch_and_hold(key, api_key, lat, lon, radius_km, source, max_pages))
        _in_flight[key] = task
        task.add_done_callback(lambda done: _walk_finished(key, done))

    # Shielded: a caller that is cancelled (a closed browser tab) leaves the
    # walk running for whoever else is waiting on it, and for the cache.
    return json.loads(await asyncio.shield(task))


async def _fetch_and_hold(
    key: _CacheKey, api_key: str, lat: float, lon: float, radius_km: int, source: str, max_pages: int
) -> str:
    """One upstream walk, held on success, answered as JSON text."""
    systems = await _fetch_upstream(api_key, lat, lon, radius_km, source=source, max_pages=max_pages)
    text = json.dumps(systems)
    _cache_put(key, text)
    return text


def _walk_finished(key: _CacheKey, task: asyncio.Task) -> None:
    # Only if it is still this walk's slot: clear_cache() may have dropped it,
    # and a later walk for the same key may already hold it.
    if _in_flight.get(key) is task:
        del _in_flight[key]
    # Marks a failure as retrieved even when every waiter was cancelled, so it
    # is not reported again as "Task exception was never retrieved"; the
    # waiters that remain have had it raised to them.
    if not task.cancelled():
        task.exception()


async def _fetch_upstream(
    api_key: str,
    lat: float,
    lon: float,
    radius_km: int = 80,
    *,
    source: str,
    max_pages: int = 3,
) -> list[dict]:
    """
    Fetch broadcast transmitters near (lat, lon) from Maprad.io.

    Issues parallel queries per broadcast subtype, so that low-power
    narrowcasting does not crowd out the high-power stations. A subtype that
    fails costs only itself; every subtype failing raises (MapradQueryError
    when upstream refused the query), because an empty list would read as a
    location with no stations. For CA, subtype queries that all come back
    empty are followed by one broader query (see _CA_FALLBACK_QUERY).

    ``source`` carries no default: the regions Maprad can answer for are a
    subset of the regions the service supports, so a default would let a
    caller reach the wrong one silently. It arrives lowercased and checked
    against _SUPPORTED_SOURCES by fetch_broadcast_systems.
    """
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    base_kwargs = {
        "source": source,
        "coords": f"{lat},{lon}",
        "radius": str(radius_km),
        "devices": _DEVICE_FIELDS,
    }

    subtypes = _BROADCAST_SUBTYPES[source]

    async with httpx.AsyncClient(timeout=45.0) as client:

        async def _fetch_subtype(subtype: str) -> list[dict]:
            kwargs = {**base_kwargs, "subtype": subtype}
            return await _paginate_query(
                client,
                headers,
                _SUBTYPE_QUERY,
                kwargs,
                max_pages=max_pages,
                page_size=_PAGE_SIZE,
            )

        results = await asyncio.gather(*(_fetch_subtype(s) for s in subtypes), return_exceptions=True)

        systems: list[dict] = []
        failures: list[BaseException] = []
        for subtype, result in zip(subtypes, results):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result  # cancellation and friends are not a subtype's failure
                log.warning("Maprad %s subtype %s query failed: %s", source, subtype, result)
                failures.append(result)
            else:
                systems.extend(result)

        if len(failures) == len(subtypes):
            # Nothing answered, so nothing here can be offered as the result:
            # an empty list would read as "no towers near this point". A
            # refusal from upstream goes first, since it names its own cause.
            for exc in failures:
                if isinstance(exc, MapradQueryError):
                    raise exc
            raise failures[0]

        if not systems and source == "ca":
            log.warning(
                "Maprad ca: no system matched subtypes %s near %s; retrying as any Broadcast licence in 54-698 MHz",
                subtypes,
                base_kwargs["coords"],
            )
            systems = await _paginate_query(
                client,
                headers,
                _CA_FALLBACK_QUERY,
                base_kwargs,
                max_pages=max_pages,
                page_size=_PAGE_SIZE,
                label="broadcast-fallback",
            )

    if source == "ca":
        _ca_eirp_to_watts(systems)
    return systems
