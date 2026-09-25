import asyncio
import logging

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
    Fetch broadcast transmitters near (lat, lon) from Maprad.io.

    Issues parallel queries per broadcast subtype, so that low-power
    narrowcasting does not crowd out the high-power stations. A subtype that
    fails costs only itself; every subtype failing raises (MapradQueryError
    when upstream refused the query), because an empty list would read as a
    location with no stations. For CA, subtype queries that all come back
    empty are followed by one broader query (see _CA_FALLBACK_QUERY).

    ``source`` carries no default: the regions Maprad can answer for are a
    subset of the regions the service supports, so a default would let a
    caller reach the wrong one silently.
    """
    # Maprad's source keys are lowercase, and this value reaches the query as
    # well as the guard: folding it in only one of the two asks upstream for a
    # key that matches nothing, which comes back empty rather than raising.
    source = source.lower()
    # US searches belong to clients/fcc.py.
    if source not in _SUPPORTED_SOURCES:
        raise ValueError(f"Maprad holds no broadcast data for source {source!r}")

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
