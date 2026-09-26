"""Reach in classify_region(): the nearest supported region within the search radius.

The Natural Earth coastline behind the border polygons is coarse enough to
miss real land: UBC's Point Grey campus in Vancouver sits ~0.5 km outside the
Canada polygon and was once answered with a 422. Beyond coastline error,
tower-finding should work out to sea: the question is which database gives the
best towers, not which jurisdiction a point is in. A point covered by no
polygon therefore goes to the nearest supported region within ``reach_km`` --
for tower search, the request's own search radius -- and is unsupported only
when no region lies that close.

Tests that depend on the reach pass it explicitly, mostly around a measured
distance, so they hold whatever default radius the service ships with.
"""

import pytest
from fastapi import HTTPException
from routes.towers import _detect_source
from services import region_lookup
from services.region_lookup import classify_region, unsupported_region_detail

UBC = (49.26482627154048, -123.25016353304554)
SYDNEY_OFFSHORE = (-33.85, 151.35)  # ~5 km east of the heads, Tasman Sea
MAINE_OFFSHORE = (43.62, -70.13)  # ~5.5 km off Cape Elizabeth, Gulf of Maine
POINT_ROBERTS = (48.985, -123.07)  # US exclave, <1 km from the Canada polygon
HALIFAX_OFFSHORE = (43.95, -63.35)  # open water ~59 km off the Nova Scotia shore, south of Halifax
MID_ATLANTIC = (40.0, -40.0)  # ~1,340 km from the nearest polygon (Newfoundland)
MID_PACIFIC = (30.0, -140.0)

DEFAULT_RADIUS_KM = 80  # the service's shipped default search radius
MAX_RADIUS_KM = 300  # the route's cap on radius_km


def _km(region: str, lat: float, lon: float) -> float | None:
    return region_lookup._distance_km(region_lookup._geoms[region], lat, lon, 10_000.0)


def _covered(lat: float, lon: float) -> bool:
    point = region_lookup.Point(lon, lat)
    return any(g.covers(point) for g in region_lookup._geoms.values())


@pytest.fixture(autouse=True)
def _warm():
    region_lookup.warm_borders()


class TestCoastalPointsResolve:
    def test_ubc_point_grey_is_canada(self):
        """The original bug: just outside the coarse coastline, plainly Canada."""
        lat, lon = UBC
        assert not _covered(lat, lon), "fixture no longer exercises the miss path; pick a point the polygon misses"
        assert classify_region(lat, lon, DEFAULT_RADIUS_KM) == "ca"
        assert _detect_source(lat, lon, DEFAULT_RADIUS_KM) == "ca"

    def test_offshore_sydney_is_australia(self):
        assert classify_region(*SYDNEY_OFFSHORE, DEFAULT_RADIUS_KM) == "au"

    def test_offshore_maine_is_us(self):
        assert classify_region(*MAINE_OFFSHORE, DEFAULT_RADIUS_KM) == "us"

    def test_point_roberts_is_us_not_canada(self):
        """Canada's polygon is under 1 km away; the US polygon covering the
        point must win (fast path), not the nearby neighbour."""
        assert classify_region(*POINT_ROBERTS, DEFAULT_RADIUS_KM) == "us"
        assert classify_region(*POINT_ROBERTS, MAX_RADIUS_KM) == "us"

    def test_nearest_region_wins_in_boundary_waters(self):
        """Haro Strait water within reach of both shores goes to the closer one."""
        for lat, lon in [(48.55, -123.2), (48.7, -123.22), (49.0, -123.15)]:
            us, ca = _km("us", lat, lon), _km("ca", lat, lon)
            assert us is not None and ca is not None
            assert us < 25.0 and ca < 25.0
            for reach in (25.0, DEFAULT_RADIUS_KM, MAX_RADIUS_KM):
                assert classify_region(lat, lon, reach) == ("us" if us < ca else "ca")

    def test_antimeridian_wraps(self):
        """Aleutian land lies on both sides of 180; a point just east of the
        line must still see the islands just west of it."""
        lat, lon = 51.8, -179.99
        d = _km("us", lat, lon)
        assert d is not None and d < 30.0
        assert classify_region(lat, lon, 30.0) == "us"
        assert classify_region(lat, lon, DEFAULT_RADIUS_KM) == "us"


class TestReachIsTheSearchRadius:
    def test_halifax_offshore_fixture_is_where_it_claims(self):
        lat, lon = HALIFAX_OFFSHORE
        assert not _covered(lat, lon)
        d = _km("ca", lat, lon)
        assert d is not None and 50.0 < d < 70.0

    def test_halifax_offshore_is_canada_within_the_default_radius(self):
        assert classify_region(*HALIFAX_OFFSHORE, DEFAULT_RADIUS_KM) == "ca"
        assert _detect_source(*HALIFAX_OFFSHORE, DEFAULT_RADIUS_KM) == "ca"

    def test_halifax_offshore_is_unsupported_within_30_km(self):
        assert classify_region(*HALIFAX_OFFSHORE, 30) is None
        with pytest.raises(HTTPException) as exc_info:
            _detect_source(*HALIFAX_OFFSHORE, 30)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == unsupported_region_detail(30)
        assert "30 km" in exc_info.value.detail

    def test_within_and_beyond_reach(self):
        """Measured distance +-1 km around the reach's edge."""
        lat, lon = MAINE_OFFSHORE
        d = _km("us", lat, lon)
        assert d is not None and d > 2.0
        assert classify_region(lat, lon, d + 1.0) == "us"
        assert classify_region(lat, lon, d - 1.0) is None

    def test_nearest_wins_not_first_found(self):
        """With a reach wide enough to take in both countries, the closer one
        still wins: Haro Strait nearest Canada stays Canada at 300 km, with the
        US shore also inside the reach."""
        lat, lon = 48.7, -123.22
        us, ca = _km("us", lat, lon), _km("ca", lat, lon)
        assert ca < us < MAX_RADIUS_KM
        assert classify_region(lat, lon, MAX_RADIUS_KM) == "ca"


class TestOpenOceanStillUnsupported:
    @pytest.mark.parametrize("lat_lon", [MID_ATLANTIC, MID_PACIFIC, (0.0, 170.0)])
    def test_mid_ocean_is_none_even_at_the_cap(self, lat_lon):
        assert classify_region(*lat_lon, DEFAULT_RADIUS_KM) is None
        assert classify_region(*lat_lon, MAX_RADIUS_KM) is None

    @pytest.mark.parametrize("lat_lon", [MID_ATLANTIC, MID_PACIFIC])
    def test_mid_ocean_still_422(self, lat_lon):
        with pytest.raises(HTTPException) as exc_info:
            _detect_source(*lat_lon, MAX_RADIUS_KM)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == unsupported_region_detail(MAX_RADIUS_KM)


class TestUnsupportedRegionDetail:
    def test_names_the_reach_and_the_coverage(self):
        assert unsupported_region_detail(30) == (
            "No tower data within 30 km of this location (coverage: US, CA, AU). Try a larger search radius."
        )

    def test_coverage_follows_the_supported_regions(self):
        """Derived from the region table, so the message and the set can't drift."""
        detail = unsupported_region_detail(80)
        assert f"coverage: {', '.join(s.upper() for s in region_lookup.SUPPORTED_REGIONS)})" in detail

    def test_fractional_reach_is_not_padded(self):
        assert "within 80 km" in unsupported_region_detail(80.0)
        assert "within 12.5 km" in unsupported_region_detail(12.5)


class TestDistance:
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
