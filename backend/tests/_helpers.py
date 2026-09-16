"""Canonical test factories and request helpers, kept here so tests can't drift.

``device``/``system`` build raw device/system records fed to process_and_rank;
one canonical pair used across the ranking and route tests. EIRP is expressed
with the ``"eirp"`` key in WATTS, the only power key ``eirp_dbm_from_device``
actually reads. Omit ``eirp`` to exercise the built-in per-band default
fallback.

``get_towers`` / ``post_towers`` hit the two towers endpoints with the
FCC/elevation network calls mocked out, used by both the route tests and the
config tests that check a config change reaches the route.

``CONSUMER_FIELDS`` is the response contract retina-gui and retina-spectrum
read, pinned here so the ranking tests and the route tests cannot drift apart
on it.
``get_towers`` hits GET /api/towers with the FCC/elevation network calls
mocked out, used by both the route tests and the config tests that check a
config change reaches the route.

``make_httpx_mock``/``status_error_response`` stand in for an upstream: every
route here that leaves the process does so through ``httpx.AsyncClient``, so
one patch of the class covers the elevation lookup and the geocoders alike.
"""

import contextlib
import unittest.mock

import httpx


def device(freq_mhz, lat, lon, callsign="KXXX", eirp=None, antenna_height=100):
    """Build a minimal raw device dict accepted by process_and_rank.

    ``eirp`` is in watts (the Maprad/FCC unit). When None, no power key is set
    so process_and_rank applies its per-band default EIRP.
    """
    dev = {
        "frequency": freq_mhz,
        "callsign": callsign,
        "antennaHeight": antenna_height,
        "location": {
            # parse_geom accepts a plain WKT string; lon before lat per WKT convention.
            "geom": f"POINT({lon} {lat})",
            "name": "Test Tower",
            "state": "GA",
        },
    }
    if eirp is not None:
        dev["eirp"] = eirp
    return dev


def system(devices, licence_type="", licence_subtype=""):
    """Wrap devices in a raw-system dict."""
    return {
        "licence": {"type": licence_type, "subtype": licence_subtype},
        "devices": devices,
    }


# The fields retina-gui and retina-spectrum read off a tower row, with the type
# each of them expects. Pinned rather than described: a rename or a type change
# here is a broken map pin or a blank column in another repo, found at runtime.
CONSUMER_FIELDS = {
    "callsign": str,
    "name": str,
    "frequency_mhz": float,
    "band": str,
    "latitude": float,
    "longitude": float,
    "distance_km": float,
    "bearing_deg": float,
    "bearing_cardinal": str,
    "state": str,
    "received_power_dbm": float,
    "rank": int,
}

# Read by the same consumers but legitimately null: power_db comes from a
# measurement and altitude_m from the elevation enrichment, and either can be
# absent for good reasons. The key being present is the contract; its type is
# not. altitude_m is added by the route, so only the route sees it.
CONSUMER_NULLABLE_FIELDS = ("power_db", "altitude_m")


@contextlib.contextmanager
def mocked_upstreams(raw_systems):
    """The FCC fetch and the elevation lookup, stubbed out. No network in tests."""
    with (
        unittest.mock.patch("routes.towers.API_KEY", ""),
        unittest.mock.patch(
            "routes.towers.fetch_fcc_broadcast_systems",
            new=unittest.mock.AsyncMock(return_value=raw_systems),
        ),
        unittest.mock.patch(
            "services.elevation.lookup_many",
            new=unittest.mock.AsyncMock(return_value={}),
        ),
    ):
        yield


def get_towers(client, query, raw_systems):
    """GET /api/towers via the test client with FCC/elevation calls mocked out."""
    with mocked_upstreams(raw_systems):
        return client.get(f"/api/towers?{query}")


def post_towers(client, payload, raw_systems):
    """POST /api/towers via the test client with FCC/elevation calls mocked out."""
    with mocked_upstreams(raw_systems):
        return client.post("/api/towers", json=payload)


def make_httpx_mock(get_return=None, get_side_effect=None):
    """Return a patch context manager that intercepts httpx.AsyncClient.

    Pass ``get_side_effect`` a list to answer successive GETs differently — the
    geocoder tests need one response per provider. The patch object carries the
    mock client as ``.mock_client``, for asserting on the request that was made
    (headers, params) rather than only on what came back.
    """
    mock_client = unittest.mock.AsyncMock()
    mock_client.get = unittest.mock.AsyncMock(return_value=get_return, side_effect=get_side_effect)
    mock_ctx = unittest.mock.MagicMock()
    mock_ctx.__aenter__ = unittest.mock.AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = unittest.mock.AsyncMock(return_value=False)
    patcher = unittest.mock.patch("httpx.AsyncClient", return_value=mock_ctx)
    patcher.mock_client = mock_client
    return patcher


def json_response(payload):
    """A response whose raise_for_status() passes and json() returns payload."""
    resp = unittest.mock.MagicMock()
    resp.raise_for_status = unittest.mock.MagicMock()
    resp.json = unittest.mock.MagicMock(return_value=payload)
    return resp


def status_error_response(status_code):
    """A response whose raise_for_status() raises for that status.

    The status has to be a real int, not a bare MagicMock: the classification
    under test compares it.
    """
    resp = unittest.mock.MagicMock()
    resp.raise_for_status = unittest.mock.MagicMock(
        side_effect=httpx.HTTPStatusError(
            f"{status_code} error",
            request=unittest.mock.MagicMock(),
            response=unittest.mock.MagicMock(status_code=status_code),
        )
    )
    return resp
