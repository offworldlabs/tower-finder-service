"""The border polygons must be parsed at startup, not on the first request.

Loading is ~1.5s of synchronous JSON + shapely work on a 5 MB file. On the
event loop inside a request handler that stalls every other request on the
worker, once per process.
"""

import unittest.mock

from fastapi.testclient import TestClient
from services import region_lookup

from app import app


def _clear_cache():
    """Drop the module-level geometry cache so a cold start can be observed."""
    region_lookup._geoms.clear()


class TestWarmBorders:
    def test_warm_borders_populates_the_cache(self):
        _clear_cache()
        assert not region_lookup._geoms

        region_lookup.warm_borders()

        assert set(region_lookup._geoms) == {"us", "ca", "au"}

    def test_warm_borders_is_idempotent(self):
        _clear_cache()
        region_lookup.warm_borders()
        first = dict(region_lookup._geoms)

        region_lookup.warm_borders()

        # Same geometry objects, not re-parsed.
        assert all(region_lookup._geoms[k] is first[k] for k in first)

    def test_classify_region_still_loads_on_demand_without_a_warm_up(self):
        """Scripts and tests import this module without running app startup."""
        _clear_cache()

        assert region_lookup.classify_region(42.38708028093612, -71.24905416622781) == "us"


class TestStartupWarmsBorders:
    def test_app_startup_loads_the_borders(self):
        _clear_cache()

        with TestClient(app):
            # Entering the context runs lifespan; borders must already be in
            # memory before any request is handled.
            assert set(region_lookup._geoms) == {"us", "ca", "au"}

    def test_no_request_re_parses_the_borders_after_startup(self):
        """The regression this guards: the 5 MB parse happening on the request path.

        classify_region() always calls _load_borders(); the point is that after a
        warm start it returns on a dict check. Patch the shapely constructor,
        which only runs on a real parse, rather than the guard itself.
        """
        _clear_cache()

        with (
            TestClient(app) as client,
            unittest.mock.patch.object(
                region_lookup, "shape", side_effect=AssertionError("re-parsed on the request path")
            ),
        ):
            # Paris is in no supported region — reaches classify_region and
            # returns 422 without needing to parse anything again.
            r = client.get("/api/towers", params={"lat": 48.8566, "lon": 2.3522})

        assert r.status_code == 422
