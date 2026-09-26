"""US/Canada/Australia region lookup backed by real country boundary polygons.

Replaces lat/lon bounding-box heuristics, which can't represent a border
that dips and bulges (e.g. the Great Lakes, the Maine/Quebec line).

A point inside a country polygon is classified by that polygon. A point inside
none of them -- real coastal land the Natural Earth coastline is too coarse to
cover (a peninsula tip, a pier, a harbour-front block), a boat, a platform, an
island offshore, or a town just over a land border -- is served by the nearest
supported region whose polygon lies within the caller's reach, which for tower
search is the request's own search radius. The question is which database will
give the best towers, not which jurisdiction the point is in: if a country's
territory is within the search radius, so are (some of) its towers. Anything
with no supported polygon within reach (open ocean, other continents) is
unsupported.
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

# Single source of truth for the supported-region set, derived from
# _ADMIN_TO_SOURCE so it and the rejection message below can't drift.
SUPPORTED_REGIONS = tuple(_ADMIN_TO_SOURCE.values())


def unsupported_region_detail(reach_km: float) -> str:
    """The human-facing 422 detail for a point with no supported region within ``reach_km``.

    Names the reach because it is the caller's own search radius, and a larger
    one is the thing that can turn this answer into a result.
    """
    coverage = ", ".join(s.upper() for s in SUPPORTED_REGIONS)
    return f"No tower data within {reach_km:g} km of this location (coverage: {coverage}). Try a larger search radius."


# How far outside every polygon a point may sit and still be served: the
# caller's reach, passed to classify_region() explicitly. For tower search it
# is the request's effective search radius (default 80 km, capped at 300), so
# "is any supported country within my search area" and "which database do I
# query" are the same question. The nearest region within reach wins; there is
# no merging of several databases.
#
# This absorbs coastline error (UBC's Point Grey campus sits ~0.5 km outside
# the Canada polygon) and serves points genuinely out to sea -- ferries,
# platforms, offshore islands -- from the nearest shore's towers. Open ocean
# stays unsupported: the mid-Atlantic is 1,300+ km from any polygon, well past
# the 300 km cap.
#
# Trade-off, accepted deliberately: the reach crosses land borders too, since
# the borders file holds only the supported countries. With the 80 km default,
# Tijuana and every point of northern Mexico within 80 km of the US border
# resolve to "us", and the Torres Strait coast of Papua New Guinea to "au"; a
# user who widens the radius widens that strip with it (to 300 km at the cap).
# Those users get the US or Australian tower list, not their own country's.
# That is the useful answer: those towers are within the radius they asked
# about, and we have no database for Mexico or Papua New Guinea to give them
# instead.

# Mean length of a degree of latitude. Longitude degrees are scaled by
# cos(latitude) at the query point. That local equirectangular approximation
# drifts with the latitude offset of the measured shore: at 60N the error is
# ~2% at 80 km and ~8% at the 300 km cap (less further south), so a shore
# right at the edge of the reach can land either side of it. Ample for picking
# a database; the search itself measures its own distances.
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


def classify_region(lat: float, lon: float, reach_km: float) -> str | None:
    """Return "us", "ca", "au", or None if no supported region is within reach.

    Points inside a country polygon are classified by it, whatever the reach.
    Otherwise the nearest region whose polygon lies within ``reach_km`` wins
    (nearest, so a boat in Haro Strait goes to the closer of the US and
    Canadian shores); with none that close, None.
    """
    _load_borders()
    point = Point(lon, lat)  # GeoJSON order is (lon, lat)
    for source, geom in _geoms.items():
        if geom.covers(point):  # covers() includes boundary points; contains() excludes them
            return source
    # Miss path only: a few ms of clipping, never paid by inland points. The
    # cost is the clip over each whole border geometry, so it barely moves with
    # the reach (measured ~3-7 ms at 25, 80 and 300 km alike).
    nearest: str | None = None
    nearest_km = float(reach_km)
    for source, geom in _geoms.items():
        d = _distance_km(geom, lat, lon, nearest_km)
        if d is not None and (nearest is None or d < nearest_km):
            nearest, nearest_km = source, d
    return nearest
