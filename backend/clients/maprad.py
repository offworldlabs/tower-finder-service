import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

MAPRAD_URL = "https://maprad.io/api"

# maprad.io refuses a larger page outright rather than clamping it, and a
# refusal arrives as a GraphQL error, which the walk below reads as the end of
# the results. So this is the API's ceiling, not a preference.
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

# Template for querying a specific licence_subtype (AU / CA).
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

# Broadcast subtypes useful for passive radar — high-power, POINT geometries.
# Retransmission / Community Broadcasting omitted: often low-power or return
# enormous MULTIPOLYGON coverage geometries that slow down the API response.
_BROADCAST_SUBTYPES = [
    "Commercial Television",
    "National Broadcasting",
    "Commercial Radio",
]

# The whole of what Maprad can answer for: its US dataset is the FCC ULS
# licence system, which holds no broadcast stations.
_SUPPORTED_SOURCES = {"au", "ca"}


async def _paginate_query(
    client: httpx.AsyncClient,
    headers: dict,
    template: str,
    fmt_kwargs: dict,
    max_pages: int,
    page_size: int,
) -> list[dict]:
    """Run a single paginated query, returning collected system nodes."""
    systems: list[dict] = []
    cursor = ""
    for page in range(max_pages):
        query = template.format(cursor=cursor, page_size=page_size, **fmt_kwargs)
        resp = await client.post(MAPRAD_URL, json={"query": query}, headers=headers)
        resp.raise_for_status()
        body = resp.json()

        if "errors" in body:
            log.warning("GraphQL errors on page %d: %s", page + 1, body["errors"])
            break

        data = body.get("data", {}).get("systems", {})
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
            fmt_kwargs.get("subtype"),
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
    narrowcasting does not crowd out the high-power stations.

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

    async with httpx.AsyncClient(timeout=45.0) as client:

        async def _fetch_subtype(subtype: str) -> list[dict]:
            kwargs = {**base_kwargs, "subtype": subtype}
            try:
                return await _paginate_query(
                    client,
                    headers,
                    _SUBTYPE_QUERY,
                    kwargs,
                    max_pages=max_pages,
                    page_size=_PAGE_SIZE,
                )
            except Exception as exc:
                log.warning("Subtype %s query failed: %s", subtype, exc)
                return []

        batches = await asyncio.gather(*(_fetch_subtype(s) for s in _BROADCAST_SUBTYPES))
        return [sys for batch in batches for sys in batch]
