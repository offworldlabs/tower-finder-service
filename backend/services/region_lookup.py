"""US/Canada/Australia region lookup backed by real country boundary polygons.

Replaces lat/lon bounding-box heuristics, which can't represent a border
that dips and bulges (e.g. the Great Lakes, the Maine/Quebec line).
"""

import json
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

_BORDERS_PATH = Path(__file__).resolve().parent.parent / "data" / "us_canada_australia_borders.geojson"

_ADMIN_TO_SOURCE = {
    "United States of America": "us",
    "Canada": "ca",
    "Australia": "au",
}

_geoms: dict[str, BaseGeometry] = {}


def _load_borders() -> None:
    if _geoms:
        return
    with open(_BORDERS_PATH) as f:
        data = json.load(f)
    for feature in data["features"]:
        admin = feature["properties"].get("ADMIN")
        source = _ADMIN_TO_SOURCE.get(admin)
        if source is not None:
            _geoms[source] = shape(feature["geometry"])


def classify_region(lat: float, lon: float) -> str | None:
    """Return "us", "ca", "au", or None if the point falls in none of them."""
    _load_borders()
    point = Point(lon, lat)  # GeoJSON order is (lon, lat)
    for source, geom in _geoms.items():
        if geom.contains(point):
            return source
    return None
