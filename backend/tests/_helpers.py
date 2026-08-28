"""Canonical test factories and request helpers, kept here so tests can't drift.

``device``/``system`` build raw device/system records fed to process_and_rank;
one canonical pair used across the ranking and route tests. EIRP is expressed
with the ``"eirp"`` key in WATTS — the only power key ``eirp_dbm_from_device``
actually reads. Omit ``eirp`` to exercise the built-in per-band default
fallback.

``get_towers`` hits GET /api/towers with the FCC/elevation network calls
mocked out, used by both the route tests and the config tests that check a
config change reaches the route.
"""

import unittest.mock


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


def get_towers(client, query, raw_systems):
    """GET /api/towers via the test client with FCC/elevation calls mocked out."""
    with (
        unittest.mock.patch("routes.towers.API_KEY", ""),
        unittest.mock.patch(
            "routes.towers.fetch_fcc_broadcast_systems",
            new=unittest.mock.AsyncMock(return_value=raw_systems),
        ),
        unittest.mock.patch(
            "routes.towers._batch_lookup_elevations",
            new=unittest.mock.AsyncMock(return_value={}),
        ),
    ):
        return client.get(f"/api/towers?{query}")
