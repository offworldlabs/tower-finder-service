"""Coastal tolerance in classify_region().

The Natural Earth coastline behind the border polygons is coarse enough to
miss real land: UBC's Point Grey campus in Vancouver sits ~0.5 km outside the
Canada polygon and was answered with "Location is not in a supported region".
A point covered by no polygon now goes to the nearest supported region within
COASTAL_TOLERANCE_KM, and stays unsupported beyond it.

Tests that depend on the band's width patch the constant around a measured
distance, so they hold whatever default the module ships with.
"""

import pytest
from fastapi import HTTPException
from routes.towers import _detect_source
from services import region_lookup
from services.region_lookup import classify_region

UBC = (49.26482627154048, -123.25016353304554)
SYDNEY_OFFSHORE = (-33.85, 151.35)  # ~5 km east of the heads, Tasman Sea
MAINE_OFFSHORE = (43.62, -70.13)  # ~5.5 km off Cape Elizabeth, Gulf of Maine
POINT_ROBERTS = (48.985, -123.07)  # US exclave, <1 km from the Canada polygon
MID_ATLANTIC = (40.0, -40.0)
MID_PACIFIC = (30.0, -140.0)


def _km(region: str, lat: float, lon: float) -> float | None:
    return region_lookup._distance_km(region_lookup._geoms[region], lat, lon, 10_000.0)


@pytest.fixture(autouse=True)
def _warm():
    region_lookup.warm_borders()


class TestCoastalPointsResolve:
    def test_ubc_point_grey_is_canada(self):
        """The reported bug: just outside the coarse coastline, plainly Canada."""
        lat, lon = UBC
        assert not region_lookup._geoms["ca"].covers(region_lookup.Point(lon, lat)), (
            "fixture no longer exercises the miss path; pick a point the polygon misses"
        )
        assert classify_region(lat, lon) == "ca"
        assert _detect_source(lat, lon) == "ca"

    def test_offshore_sydney_is_australia(self):
        assert classify_region(*SYDNEY_OFFSHORE) == "au"

    def test_offshore_maine_is_us(self):
        assert classify_region(*MAINE_OFFSHORE) == "us"

    def test_point_roberts_is_us_not_canada(self):
        """Canada's polygon is under 1 km away; the US polygon covering the
        point must win (fast path), not the nearby neighbour."""
        assert classify_region(*POINT_ROBERTS) == "us"

    def test_nearest_region_wins_in_boundary_waters(self):
        """Haro Strait water within reach of both shores goes to the closer one."""
        for lat, lon in [(48.55, -123.2), (48.7, -123.22), (49.0, -123.15)]:
            us, ca = _km("us", lat, lon), _km("ca", lat, lon)
            assert us is not None and ca is not None
            assert us < region_lookup.COASTAL_TOLERANCE_KM and ca < region_lookup.COASTAL_TOLERANCE_KM
            assert classify_region(lat, lon) == ("us" if us < ca else "ca")

    def test_antimeridian_wraps(self):
        """Aleutian land lies on both sides of 180; a point just east of the
        line must still see the islands just west of it."""
        lat, lon = 51.8, -179.99
        d = _km("us", lat, lon)
        assert d is not None and d < 30.0
        assert classify_region(lat, lon) == "us"


class TestOpenOceanStillUnsupported:
    @pytest.mark.parametrize("lat_lon", [MID_ATLANTIC, MID_PACIFIC, (0.0, 170.0)])
    def test_mid_ocean_is_none(self, lat_lon):
        assert classify_region(*lat_lon) is None

    @pytest.mark.parametrize("lat_lon", [MID_ATLANTIC, MID_PACIFIC])
    def test_mid_ocean_still_422(self, lat_lon):
        with pytest.raises(HTTPException) as exc_info:
            _detect_source(*lat_lon)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == region_lookup.UNSUPPORTED_REGION_DETAIL


class TestToleranceBoundary:
    """Measured distance +-1 km around the band edge, independent of the default."""

    def test_within_and_beyond_tolerance(self, monkeypatch):
        lat, lon = MAINE_OFFSHORE
        d = _km("us", lat, lon)
        assert d is not None and d > 2.0

        monkeypatch.setattr(region_lookup, "COASTAL_TOLERANCE_KM", d + 1.0)
        assert classify_region(lat, lon) == "us"

        monkeypatch.setattr(region_lookup, "COASTAL_TOLERANCE_KM", d - 1.0)
        assert classify_region(lat, lon) is None

    def test_distance_is_in_km_not_degrees(self):
        """At 60N a degree of longitude is half a degree of latitude's length.
        Two points 0.5 deg of longitude and 0.25 deg of latitude off the same
        spot must measure roughly equal (~27.8 km each)."""
        geom = region_lookup.Point(-150.0, 60.0)
        east = region_lookup._distance_km(geom, 60.0, -149.5, 100.0)
        north = region_lookup._distance_km(geom, 60.25, -150.0, 100.0)
        assert east == pytest.approx(27.8, abs=0.3)
        assert north == pytest.approx(27.8, abs=0.3)

    def test_distance_beyond_limit_is_none(self):
        geom = region_lookup.Point(-150.0, 60.0)
        assert region_lookup._distance_km(geom, 60.25, -150.0, 20.0) is None

    def test_default_tolerance_is_coastal_not_oceanic(self):
        """Generous enough for coastline error and nearshore water; far short of
        the 65 km gap south of the Hawaiian islands that must stay unsupported."""
        assert 10.0 <= region_lookup.COASTAL_TOLERANCE_KM <= 50.0
