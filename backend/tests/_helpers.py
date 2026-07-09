"""Shared test factories for raw device/system records fed to process_and_rank.

One canonical pair used across the ranking and route tests so the two can't
drift. EIRP is expressed with the ``"eirp"`` key in WATTS — the only power key
``eirp_dbm_from_device`` actually reads. Omit ``eirp`` to exercise the built-in
per-band default fallback.
"""


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
