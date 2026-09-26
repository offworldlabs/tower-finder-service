"""Check a deployed tower search against maprad.io's own index, for Canada.

For the operator. Canadian towers come from Maprad's metered GraphQL API,
walked a bounded number of pages per subtype, so a search can come back short
without saying so. This script asks the service for its towers near a point
and compares them with what maprad.io's web UI lists for the same circle:

  - the service: GET <host>/api/towers?lat=..&lon=..&radius_km=..&limit=200
  - the index:   https://maprad.io/ext/s?... the facet search the maprad.io map
                 reads, which answers WITHOUT an API key. It is an undocumented
                 interface of their UI, not a published API, and may change.

For each target and subtype (ISED `FM`, `DTV`) it prints the index's record
count, its distinct stations (callsigns normalised, see `normalize_callsign`),
how many of those the service returned, and the strongest ones it did not.

Stdlib only and no repo imports, so it runs from any checkout against any
environment:

  python3 scripts/maprad_completeness.py toronto montreal vancouver
  python3 scripts/maprad_completeness.py --host https://test-towers.retina.fm 49.26,-123.25

Exit status: 1 when any missing station is at or above --strong-dbw (default
40 dBW, about 10 kW EIRP); otherwise 2 when a target could not be checked
(the service or the index failed); otherwise 0.

Test and staging carry no MAPRAD_API_KEY, so there the service answers 500 and
the target is reported as failed: only production can be checked end to end.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_HOST = "https://towers.retina.fm"
INDEX_URL = "https://maprad.io/ext/s"
USER_AGENT = "tower-finder-service/maprad_completeness (+https://github.com/offworldlabs/tower-finder-service)"

# Upstream accepted up to 100 on 2026-09-26 and refused more with a 400
# ("Page size of [100] exceeded"); 30 is what the maprad.io UI itself asks for.
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100
# Hard ceiling on index pages per subtype, so an unexpectedly broad answer
# (a huge radius, a changed filter) cannot turn into hundreds of requests.
MAX_PAGES = 40
# The service's own ceiling on `limit`.
SERVICE_LIMIT = 200

PRESETS: dict[str, tuple[float, float]] = {
    "toronto": (43.6532, -79.3832),
    "montreal": (45.5017, -73.5673),
    "vancouver": (49.2827, -123.1207),
    "ubc": (49.26482627154048, -123.25016353304554),
}

# ── Pure logic (unit-tested, no network) ─────────────────────────────────────

_AUX = re.compile(r"-AX\d*(?=-|$)")
_HD = re.compile(r"-HD(?=-|$)")
_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def normalize_callsign(raw: object) -> str | None:
    """Reduce an ISED callsign to the station it belongs to, or None if blank.

    ISED files one licence record per transmitter, so a station appears as
    its main record plus auxiliaries and digital companions:

      CKFM-FM, CKFM-FM-AX1, CKFM-FM-AX2  -> CKFM-FM   (auxiliary sites)
      CFMZ-HD, CBLA-HD-AX1               -> CFMZ-FM, CBLA-FM (HD Radio rides
                                            the analogue carrier)
      CJKX-HD-2                          -> CJKX-FM-2
      CIMA-FM(TP)                        -> CIMA-FM   (parenthesised qualifier)

    Numbered rebroadcasters (CJKX-FM-1, CHIN-1-FM) are distinct stations and
    stay distinct. The same function is applied to both sides of the diff.
    """
    if raw is None:
        return None
    text = str(raw).strip().upper()
    text = _PAREN.sub("", text)
    text = _AUX.sub("", text)
    text = _HD.sub("-FM", text)
    return text or None


def first(value: object) -> object:
    """Solr returns most fields as one-element lists; take the scalar."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _number(value: object) -> float | None:
    value = first(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class Station:
    base: str
    callsign: str
    eirp_dbw: float | None
    freq_mhz: float | None
    site: str
    records: int = 1


def parse_doc(doc: dict) -> Station | None:
    """One index record -> Station, or None when it has no callsign.

    `i_eirp` is dBW for the CA source even though `i_eirp_unit` says W (the
    service's client converts it for the same reason); `i_frequency` is Hz.
    """
    callsign = first(doc.get("i_callsign"))
    base = normalize_callsign(callsign)
    if base is None:
        return None
    freq_hz = _number(doc.get("i_frequency"))
    return Station(
        base=base,
        callsign=str(callsign).strip(),
        eirp_dbw=_number(doc.get("i_eirp")),
        freq_mhz=round(freq_hz / 1e6, 3) if freq_hz is not None else None,
        site=str(first(doc.get("i_site_name")) or "").strip(),
    )


def _stronger(a: Station, b: Station) -> bool:
    """Is `a` the stronger record? Unknown power loses to any known power."""
    if a.eirp_dbw is None:
        return False
    return b.eirp_dbw is None or a.eirp_dbw > b.eirp_dbw


def index_stations(docs: list[dict]) -> tuple[dict[str, Station], int]:
    """Group index records by station, keeping each station's strongest record.

    Returns (stations by base callsign, count of records without a callsign).
    """
    stations: dict[str, Station] = {}
    unnamed = 0
    for doc in docs:
        st = parse_doc(doc)
        if st is None:
            unnamed += 1
            continue
        held = stations.get(st.base)
        if held is None:
            stations[st.base] = st
            continue
        records = held.records + 1
        if _stronger(st, held):
            stations[st.base] = st
        stations[st.base].records = records
    return stations, unnamed


def service_callsigns(towers: list[dict]) -> set[str]:
    """Every station the service returned: primary and shared callsigns alike.

    The service collapses co-sited records into one tower and lists the rest
    under `shared_callsigns`, so both count as present.
    """
    present: set[str] = set()
    for tower in towers:
        names = [tower.get("callsign")] + list(tower.get("shared_callsigns") or [])
        for name in names:
            base = normalize_callsign(name)
            if base:
                present.add(base)
    return present


@dataclass
class Diff:
    subtype: str
    records: int  # the index's numFound
    fetched: int  # records actually paged in
    stations: int
    present: int
    missing: list[Station] = field(default_factory=list)  # strongest first
    unnamed: int = 0


def diff(subtype: str, num_found: int, docs: list[dict], present: set[str]) -> Diff:
    stations, unnamed = index_stations(docs)
    missing = [st for base, st in stations.items() if base not in present]
    missing.sort(key=lambda st: (st.eirp_dbw is None, -(st.eirp_dbw or 0.0), st.base))
    return Diff(
        subtype=subtype,
        records=num_found,
        fetched=len(docs),
        stations=len(stations),
        present=len(stations) - len(missing),
        missing=missing,
        unnamed=unnamed,
    )


def strong_missing(missing: list[Station], threshold_dbw: float) -> list[Station]:
    """Missing stations at or above the threshold. Unknown power never counts."""
    return [st for st in missing if st.eirp_dbw is not None and st.eirp_dbw >= threshold_dbw]


# ── Arguments ────────────────────────────────────────────────────────────────


@dataclass
class Target:
    name: str
    lat: float
    lon: float


def parse_target(text: str) -> Target:
    key = text.strip().lower()
    if key in PRESETS:
        lat, lon = PRESETS[key]
        return Target(key, lat, lon)
    parts = key.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"{text!r}: expected a preset ({', '.join(PRESETS)}) or lat,lon")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: lat,lon must be numbers") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise argparse.ArgumentTypeError(f"{text!r}: lat,lon out of range")
    return Target(f"{lat:.4f},{lon:.4f}", lat, lon)


def parse_subtypes(text: str) -> list[str]:
    subtypes = [s.strip().upper() for s in text.split(",") if s.strip()]
    if not subtypes:
        raise argparse.ArgumentTypeError("at least one subtype is required")
    return subtypes


def _page_size(text: str) -> int:
    value = int(text)
    if not 1 <= value <= MAX_PAGE_SIZE:
        raise argparse.ArgumentTypeError(f"page size must be 1..{MAX_PAGE_SIZE}")
    return value


def _radius(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 300:
        raise argparse.ArgumentTypeError("radius must be 1..300 km (the service's own range)")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Diff a tower-finder deployment's Canadian towers against maprad.io's keyless index.",
        epilog="Exit 1: a missing station at or above --strong-dbw. Exit 2: a target could not be checked.",
    )
    p.add_argument("targets", nargs="+", type=parse_target, help=f"preset ({', '.join(PRESETS)}) or lat,lon")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"service base URL (default {DEFAULT_HOST})")
    p.add_argument("--radius", type=_radius, default=80, help="search radius in km, both sides (default 80)")
    p.add_argument("--subtypes", type=parse_subtypes, default=["FM", "DTV"], help="ISED subtypes (default FM,DTV)")
    p.add_argument(
        "--strong-dbw", type=float, default=40.0, help="fail on a missing station at/above this EIRP (default 40)"
    )
    p.add_argument(
        "--source",
        default="auto",
        help="`source` passed to the service (default auto; the index side is always CA)",
    )
    p.add_argument("--timeout", type=float, default=150.0, help="per-request timeout in seconds (default 150)")
    p.add_argument("--page-size", type=_page_size, default=DEFAULT_PAGE_SIZE, help="index page size (default 30)")
    p.add_argument("--pause", type=float, default=0.25, help="seconds between index pages (default 0.25)")
    p.add_argument("--show", type=int, default=8, help="missing stations listed per subtype (default 8)")
    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    args.host = args.host.rstrip("/")
    return args


# ── Network ──────────────────────────────────────────────────────────────────


class FetchError(Exception):
    """A request that did not produce usable JSON; str() is the operator's message."""


def index_url(lat: float, lon: float, radius_km: int, subtype: str, offset: int, limit: int) -> str:
    # `geoterm` repeats (point, then radius), so urlencode gets a sequence.
    params = [
        ("mode", "summary"),
        ("limit", str(limit)),
        ("offset", str(offset)),
        ("indent", "false"),
        ("source", "CA"),
        ("geoterm", f"{lat},{lon}"),
        ("geoterm", str(radius_km)),
        ("geobound", "CIRCLE_PO"),
        ("fts", f"dv_licence_subtype:e:{subtype}"),
    ]
    return f"{INDEX_URL}?{urllib.parse.urlencode(params)}"


def service_url(host: str, lat: float, lon: float, radius_km: int, source: str) -> str:
    params = {"lat": lat, "lon": lon, "radius_km": radius_km, "limit": SERVICE_LIMIT, "source": source}
    return f"{host}/api/towers?{urllib.parse.urlencode(params)}"


def _error_detail(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:300] or "(empty body)"
    if isinstance(parsed, dict):
        detail = parsed.get("detail", parsed.get("error", parsed))
        return detail if isinstance(detail, str) else json.dumps(detail)[:300]
    return text[:300]


def fetch_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}: {_error_detail(exc.read())}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FetchError(f"request failed: {reason}") from None
    try:
        parsed = json.loads(body)
    except ValueError:
        raise FetchError(f"not JSON: {body[:200]!r}") from None
    if not isinstance(parsed, dict):
        raise FetchError("unexpected JSON shape")
    return parsed


def fetch_index(target: Target, radius_km: int, subtype: str, args: argparse.Namespace) -> tuple[int, list[dict]]:
    """Page the facet endpoint sequentially. Returns (numFound, docs)."""
    docs: list[dict] = []
    num_found = 0
    for page in range(MAX_PAGES):
        if page:
            time.sleep(args.pause)
        url = index_url(target.lat, target.lon, radius_km, subtype, page * args.page_size, args.page_size)
        response = fetch_json(url, args.timeout).get("response")
        if not isinstance(response, dict):
            raise FetchError("index answer has no `response` object")
        num_found = int(response.get("numFound") or 0)
        batch = response.get("docs") or []
        docs.extend(batch)
        if not batch or len(docs) >= num_found:
            break
    return num_found, docs


def fetch_service(target: Target, args: argparse.Namespace) -> dict:
    return fetch_json(service_url(args.host, target.lat, target.lon, args.radius, args.source), args.timeout)


# ── Report ───────────────────────────────────────────────────────────────────


def _dbw(value: float | None) -> str:
    return f"{value:5.1f}" if value is not None else "    ?"


def format_diff(d: Diff, threshold: float, show: int) -> list[str]:
    strong = strong_missing(d.missing, threshold)
    row = f"  {d.subtype:<4} {d.records:>7} {d.stations:>8} {d.present:>7} {len(d.missing):>7} {len(strong):>6}" + (
        "  STRONG MISSING" if strong else ""
    )
    lines = [row]
    notes = []
    if d.fetched < d.records:
        notes.append(f"only {d.fetched}/{d.records} records paged in (page cap)")
    if d.unnamed:
        notes.append(f"{d.unnamed} records without a callsign ignored")
    lines += [f"         note: {n}" for n in notes]
    for st in d.missing[:show]:
        mark = "!" if st in strong else " "
        freq = f"{st.freq_mhz:8.3f} MHz" if st.freq_mhz is not None else "        ? MHz"
        lines.append(f"       {mark} {st.base:<12} {_dbw(st.eirp_dbw)} dBW  {freq}  {st.site}")
    if len(d.missing) > show:
        lines.append(f"         ... and {len(d.missing) - show} more")
    return lines


TABLE_HEADER = "  sub  records stations present missing strong"


def check_target(target: Target, args: argparse.Namespace, out) -> tuple[bool, bool]:
    """Check one target. Returns (failed, has_strong_missing)."""
    print(f"\n== {target.name} ({target.lat:.5f},{target.lon:.5f}) radius {args.radius} km", file=out)
    try:
        answer = fetch_service(target, args)
    except FetchError as exc:
        print(f"  FAILED: service {args.host}: {exc}", file=out)
        return True, False
    towers = answer.get("towers") or []
    present = service_callsigns(towers)
    query = answer.get("query")
    resolved = (query.get("source") if isinstance(query, dict) else None) or "?"
    print(f"  service: {len(towers)} towers, {len(present)} callsigns, source {resolved}", file=out)
    if len(towers) >= SERVICE_LIMIT:
        print(f"  note: service hit its {SERVICE_LIMIT}-tower limit; some misses may be the limit's", file=out)
    if resolved not in ("ca", "?"):
        print(f"  note: service resolved source {resolved!r}, not 'ca'; the index side is CA only", file=out)
    print(TABLE_HEADER, file=out)
    failed = strong = False
    for subtype in args.subtypes:
        try:
            num_found, docs = fetch_index(target, args.radius, subtype, args)
        except FetchError as exc:
            print(f"  {subtype:<4} FAILED: maprad.io index: {exc}", file=out)
            failed = True
            continue
        d = diff(subtype, num_found, docs, present)
        for line in format_diff(d, args.strong_dbw, args.show):
            print(line, file=out)
        strong = strong or bool(strong_missing(d.missing, args.strong_dbw))
    return failed, strong


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    args = parse_args(argv)
    print(
        f"service {args.host}  index maprad.io/ext/s (CA)  radius {args.radius} km  "
        f"strong >= {args.strong_dbw:g} dBW  ('!' marks strong)",
        file=out,
    )
    failed_targets: list[str] = []
    strong_targets: list[str] = []
    for target in args.targets:
        failed, strong = check_target(target, args, out)
        if failed:
            failed_targets.append(target.name)
        if strong:
            strong_targets.append(target.name)
    print("", file=out)
    if strong_targets:
        print(f"RESULT: strong stations missing at {', '.join(strong_targets)}", file=out)
    if failed_targets:
        print(f"RESULT: could not check {', '.join(failed_targets)}", file=out)
    if not strong_targets and not failed_targets:
        print("RESULT: no missing station at or above the threshold", file=out)
    if strong_targets:
        return 1
    return 2 if failed_targets else 0


if __name__ == "__main__":
    sys.exit(main())
