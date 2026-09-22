"""US/Canada/Australia region lookup backed by real country boundary polygons.

Replaces lat/lon bounding-box heuristics, which can't represent a border
that dips and bulges (e.g. the Great Lakes, the Maine/Quebec line).

A point inside a country polygon is classified by that polygon. A point inside
none of them -- which includes real coastal land the Natural Earth coastline
is too coarse to cover (a peninsula tip, a pier, a harbour-front block) and
nearshore water -- is assigned to the nearest supported region, provided that
region lies within COASTAL_TOLERANCE_KM. Anything further out (open ocean,
other continents) is still unsupported.
"""

import json
import math
import threading
from pathlib import Path

import shapely
from shapely import affinity
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

_BORDERS_PATH = Path(__file__).resolve().parent.parent / "data" / "us_canada_australia_borders.geojson"

_ADMIN_TO_SOURCE = {
    "United States of America": "us",
    "Canada": "ca",
    "Australia": "au",
}

# Single source of truth for the supported-region set and the human-facing
# rejection message, derived from _ADMIN_TO_SOURCE so the two can't drift.
SUPPORTED_REGIONS = tuple(_ADMIN_TO_SOURCE.values())
UNSUPPORTED_REGION_DETAIL = (
    f"Location is not in a supported region ({', '.join(s.upper() for s in SUPPORTED_REGIONS)})."
)

# How far outside the coastline a point may sit and still be served by the
# nearest supported region. The Natural Earth coastline misses real land by
# hundreds of metres to a few km (UBC's Point Grey campus sits ~0.5 km outside
# the Canada polygon), and a user on a ferry, a pier or an offshore island is
# still within reach of that country's towers, so the band is generous. 25 km
# comfortably covers coastline error and nearshore water while keeping open
# ocean unsupported: the mid-Atlantic, mid-Pacific and the sea south of the
# main Hawaiian islands are all 60+ km from any polygon.
#
# Trade-off, accepted deliberately: the band also reaches across land borders
# into neighbouring countries (e.g. Tijuana -> "us", the Torres Strait coast of
# Papua New Guinea -> "au"), since the borders file holds only the supported
# countries. Those users are within reception range of that country's towers,
# so serving them its tower list is useful rather than wrong.
COASTAL_TOLERANCE_KM = 25.0

# Mean length of a degree of latitude. Longitude degrees are scaled by
# cos(latitude); over a ~25 km neighbourhood this local equirectangular
# approximation is accurate to well under 1%.
_KM_PER_DEG = 111.32

_geoms: dict[str, BaseGeometry] = {}
_load_lock = threading.Lock()


def _load_borders() -> None:
    if _geoms:  # fast path after warm-up
        return
    with _load_lock:
        if _geoms:  # re-check inside lock
            return
        with open(_BORDERS_PATH) as f:
            data = json.load(f)
        loaded: dict[str, BaseGeometry] = {}
        for feature in data["features"]:
            admin = feature["properties"].get("ADMIN")
            source = _ADMIN_TO_SOURCE.get(admin)
            if source is not None:
                loaded[source] = shape(feature["geometry"])
        # Published complete or not at all: the fast path above reads _geoms
        # without the lock, so filling it feature-by-feature would let a
        # concurrent caller see {"us"} mid-parse, skip the load, and silently
        # misclassify a Canadian point as unsupported. dict.update runs under
        # one GIL hold, so readers see the border set whole or empty.
        _geoms.update(loaded)


def warm_borders() -> None:
    """Parse the borders now, so no request pays for it.

    Otherwise the first classify_region() call does it — ~1.5s of synchronous
    JSON + shapely work on a 5 MB file, on the event loop, inside a request
    handler, stalling every other request on that worker once per process.
    Call this from application startup, where blocking costs nothing.

    Idempotent, and not required: classify_region() still loads on demand if
    this was never called (tests, scripts, a bare import).
    """
    _load_borders()


def _distance_km(geom: BaseGeometry, lat: float, lon: float, max_km: float) -> float | None:
    """Distance in km from (lat, lon) to ``geom``, or None if beyond ``max_km``.

    shapely measures in degrees, where a degree of longitude shrinks with
    latitude, so a raw degree distance overstates east-west separation by
    1/cos(lat) (x2 at 60N). Instead: clip the geometry to a box ``max_km`` on
    each side of the point (cheap, and an empty clip means "too far"), project
    that small piece onto a local km grid centred on the point, and measure
    there.
    """
    cos_lat = math.cos(math.radians(lat))
    half_lat = max_km / _KM_PER_DEG
    half_lon = min(180.0, max_km / (_KM_PER_DEG * max(cos_lat, 1e-6)))
    kx = _KM_PER_DEG * cos_lat
    ky = _KM_PER_DEG
    best: float | None = None
    # The polygons are split at the antimeridian, so near +-180 also look at
    # the point's wrapped-around twin (e.g. the Aleutians east of 180).
    centres = [lon]
    if lon + half_lon > 180.0:
        centres.append(lon - 360.0)
    if lon - half_lon < -180.0:
        centres.append(lon + 360.0)
    for cx in centres:
        piece = shapely.clip_by_rect(geom, cx - half_lon, lat - half_lat, cx + half_lon, lat + half_lat)
        if piece.is_empty:
            continue
        local = affinity.affine_transform(piece, [kx, 0.0, 0.0, ky, -cx * kx, -lat * ky])
        d = local.distance(Point(0.0, 0.0))
        if d <= max_km and (best is None or d < best):
            best = d
    return best


def classify_region(lat: float, lon: float) -> str | None:
    """Return "us", "ca", "au", or None if the point is in none of them.

    Points inside a country polygon are classified by it. Otherwise the
    nearest region within COASTAL_TOLERANCE_KM wins (nearest, so a boat in the
    Strait of Georgia goes to whichever shore is closer); beyond that, None.
    """
    _load_borders()
    point = Point(lon, lat)  # GeoJSON order is (lon, lat)
    for source, geom in _geoms.items():
        if geom.covers(point):  # covers() includes boundary points; contains() excludes them
            return source
    # Miss path only: a few ms of clipping, never paid by inland points.
    nearest: str | None = None
    nearest_km = COASTAL_TOLERANCE_KM
    for source, geom in _geoms.items():
        d = _distance_km(geom, lat, lon, nearest_km)
        if d is not None and (nearest is None or d < nearest_km):
            nearest, nearest_km = source, d
    return nearest
