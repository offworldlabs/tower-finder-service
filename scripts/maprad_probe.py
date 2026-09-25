"""Ask maprad.io what it holds near a point, the way the tower search asks.

For the operator, on the host that has MAPRAD_API_KEY (production only; the
upstream is metered, so staging and test carry no key). It answers the
questions a "0 towers" search leaves open:

  1. Does Maprad refuse the query outright? Raw GraphQL errors are printed
     verbatim.
  2. What licence types and subtypes exist near the point? This is the
     vocabulary the subtype queries in backend/clients/maprad.py must match.
  3. How many systems does each subtype query find, for both the AU and the
     CA vocabulary, and the CA fallback query?
  4. What do the power fields look like? CA's `eirp` is expected in dBW
     under a W label (e.g. CKFM-FM ~45.6); the client converts it.

Self-contained (httpx + stdlib) so it runs against an image that predates it.
The image does not ship scripts/, so feed it on stdin:

  docker exec -i tower-finder-prod python - 43.6532 -79.3832 ca < scripts/maprad_probe.py

or, from a checkout with the key in the environment:

  MAPRAD_API_KEY=... python scripts/maprad_probe.py 43.6532 -79.3832 ca [radius_km]

Roughly ten upstream queries per run. Exits 1 when any query came back with
a GraphQL error, so the refusal is visible to a script as well as a reader.
"""

import argparse
import json
import os
import sys
from collections import Counter

import httpx

MAPRAD_URL = "https://maprad.io/api"
PAGE_SIZE = 30  # maprad.io refuses anything larger

# Both vocabularies, whatever the deployed client happens to use: the point of
# the probe is to see which one the source actually answers to.
SUBTYPES = {
    "au": ["Commercial Television", "National Broadcasting", "Commercial Radio"],
    "ca": ["FM", "DTV"],
}

GEO = 'geoFilter: {{ type: CIRCLE, values: ["{coords}", "{radius}"] }}'

COUNT_QUERY = (
    """
query {{
  systemCount(
    source: "{source}"
    field: {field}
    limit: 100
    offset: 0
    """
    + GEO
    + """
    {filter}
  ) {{
    totalCount
    edges {{ count node {{ val }} }}
  }}
}}
"""
)

SYSTEMS_QUERY = (
    """
query {{
  systems(
    first: {page_size}
    after: "{cursor}"
    source: "{source}"
    """
    + GEO
    + """
    {filter}
  ) {{
    totalCount
    edges {{
      cursor
      node {{
        licence {{ type subtype }}
        devices {{ callsign frequency(unit: MHz) eirp transmitPower }}
      }}
    }}
    pageInfo {{ hasNextPage }}
  }}
}}
"""
)

BROADCAST_FILTER = 'filter: [ { field: licence_type, values: "Broadcast" } ]'
CA_FALLBACK_FILTER = (
    'filter: [ { field: licence_type, values: "Broadcast" } '
    '{ field: device_frequency, type: RANGE, values: ["54000000", "698000000"] } ]'
)


class Probe:
    def __init__(self, api_key: str, source: str, lat: float, lon: float, radius_km: int):
        self.client = httpx.Client(timeout=60.0, headers={"X-Api-Key": api_key, "Content-Type": "application/json"})
        self.base = {"source": source, "coords": f"{lat},{lon}", "radius": str(radius_km)}
        self.saw_errors = False

    def run(self, template: str, **kwargs) -> dict | None:
        """One query; prints any GraphQL errors verbatim and returns `data`."""
        query = template.format(**self.base, **kwargs)
        try:
            resp = self.client.post(MAPRAD_URL, json={"query": query})
        except httpx.HTTPError as exc:
            print(f"    HTTP failure: {exc!r}")
            self.saw_errors = True
            return None
        if resp.status_code != 200:
            print(f"    HTTP {resp.status_code}: {resp.text[:500]}")
            self.saw_errors = True
            return None
        body = resp.json()
        if body.get("errors"):
            self.saw_errors = True
            print("    GraphQL errors (raw):")
            print("      " + json.dumps(body["errors"], indent=2).replace("\n", "\n      "))
        return body.get("data")

    def counts(self, field: str, filter_: str = "") -> list[tuple[str, int]] | None:
        data = self.run(COUNT_QUERY, field=field, filter=filter_)
        conn = (data or {}).get("systemCount")
        if not conn:
            return None
        return [((e.get("node") or {}).get("val"), e.get("count")) for e in conn.get("edges") or []]

    def systems(self, filter_: str, max_pages: int = 1) -> tuple[int | None, list[dict]]:
        """(totalCount as reported, the nodes from up to max_pages pages)."""
        nodes: list[dict] = []
        total = None
        cursor = ""
        for _ in range(max_pages):
            data = self.run(SYSTEMS_QUERY, filter=filter_, cursor=cursor, page_size=PAGE_SIZE)
            conn = (data or {}).get("systems")
            if not conn:
                break
            total = conn.get("totalCount") if total is None else total
            edges = conn.get("edges") or []
            nodes += [e["node"] for e in edges if e.get("node")]
            if not edges or not (conn.get("pageInfo") or {}).get("hasNextPage"):
                break
            cursor = edges[-1].get("cursor") or ""
        return total, nodes


def _print_counts(rows: list[tuple[str, int]]) -> None:
    for val, count in sorted(rows, key=lambda r: -(r[1] or 0)):
        print(f"    {count:>7}  {val!r}")


def _tally_by_paging(probe: Probe, pages: int) -> None:
    print(f"  aggregation unavailable; tallying the first {pages} unfiltered page(s) instead (a sample, not a census)")
    _, nodes = probe.systems("", max_pages=pages)
    tally = Counter(((n.get("licence") or {}).get("type"), (n.get("licence") or {}).get("subtype")) for n in nodes)
    for (ltype, subtype), count in tally.most_common():
        print(f"    {count:>7}  type={ltype!r} subtype={subtype!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("lat", type=float)
    parser.add_argument("lon", type=float)
    parser.add_argument("source", type=str.lower, help="Maprad source code, e.g. ca or au")
    parser.add_argument("radius_km", type=int, nargs="?", default=80)
    parser.add_argument("--pages", type=int, default=3, help="unfiltered pages to tally if aggregation fails")
    args = parser.parse_args()

    api_key = os.environ.get("MAPRAD_API_KEY", "")
    if not api_key:
        print("MAPRAD_API_KEY is not set in this environment.", file=sys.stderr)
        return 1

    probe = Probe(api_key, args.source, args.lat, args.lon, args.radius_km)
    print(f"Maprad probe: source={args.source} point=({args.lat}, {args.lon}) radius={args.radius_km} km\n")

    print("1. Licence types near the point (systemCount by licence_type)")
    rows = probe.counts("licence_type")
    if rows is None:
        _tally_by_paging(probe, args.pages)
    else:
        _print_counts(rows)

    print("\n2. Licence subtypes near the point (systemCount by licence_subtype)")
    rows = probe.counts("licence_subtype")
    if rows is not None:
        _print_counts(rows)

    print('\n3. Subtypes among licence type "Broadcast" only')
    rows = probe.counts("licence_subtype", BROADCAST_FILTER)
    if rows is not None:
        _print_counts(rows)

    print("\n4. The subtype queries the tower search runs (first page, totalCount as reported)")
    sample: list[dict] = []
    for vocab, subtypes in SUBTYPES.items():
        marker = "  <- used for this source" if vocab == args.source else ""
        print(f"  {vocab.upper()} vocabulary{marker}")
        for subtype in subtypes:
            total, nodes = probe.systems(f'filter: [ {{ field: licence_subtype, values: "{subtype}" }} ]')
            print(f"    {subtype!r:<28} totalCount={total}  first page={len(nodes)}")
            if nodes and not sample:
                sample = nodes

    if args.source == "ca":
        print('\n5. The CA fallback query (licence_type "Broadcast", device in 54-698 MHz)')
        total, nodes = probe.systems(CA_FALLBACK_FILTER)
        print(f"    totalCount={total}  first page={len(nodes)}")
        sample = sample or nodes

    print("\n6. Raw power fields on a few devices (CA eirp is expected in dBW; AU in W)")
    shown = 0
    for node in sample:
        for dev in node.get("devices") or []:
            print(
                f"    {str(dev.get('callsign')):<14} {str(dev.get('frequency')):>10} MHz"
                f"  eirp={dev.get('eirp')!r:<12} transmitPower={dev.get('transmitPower')!r}"
                f"  [{(node.get('licence') or {}).get('subtype')}]"
            )
            shown += 1
            if shown >= 8:
                break
        if shown >= 8:
            break
    if not shown:
        print("    (no devices returned)")

    return 1 if probe.saw_errors else 0


if __name__ == "__main__":
    sys.exit(main())
