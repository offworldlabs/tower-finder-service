"""Tests for tower ranking utilities — source detection, band classification, frequency parsing."""

import json

import pytest

from routes.towers import _detect_source
from services.tower_ranking import (
    DEFAULT_LIMIT,
    FM_ONLY,
    MEASUREMENT_TOLERANCE_MHZ,
    SENSITIVITY_DBM,
    _as_float,
    _match_measurement,
    bearing_to_cardinal,
    classify_band,
    eirp_dbm_from_device,
    fspl,
    haversine,
    initial_bearing,
    parse_geom,
    parse_user_frequencies,
    process_and_rank,
    watts_to_dbm,
)
from tests._helpers import device as _device
from tests._helpers import system as _system

# ── Auto source detection ────────────────────────────────────────────────────


class TestDetectSource:
    def test_sydney_au(self):
        assert _detect_source(-33.8688, 151.2093) == "au"

    def test_washington_dc_us(self):
        assert _detect_source(38.8977, -77.0365) == "us"

    def test_toronto_ca(self):
        assert _detect_source(43.6532, -79.3832) == "ca"

    def test_anchorage_us(self):
        assert _detect_source(61.2181, -149.9003) == "us"

    def test_honolulu_us(self):
        assert _detect_source(21.3069, -157.8583) == "us"

    def test_unknown_region_raises(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            _detect_source(0, 0)
        assert exc_info.value.status_code == 422
        assert "supported region" in exc_info.value.detail


# ── Broadcast band classification ────────────────────────────────────────────


class TestClassifyBand:
    def test_fm_low_edge(self):
        assert classify_band(87.8) == "FM"

    def test_fm_high_edge(self):
        assert classify_band(108.0) == "FM"

    def test_fm_mid(self):
        assert classify_band(95.5) == "FM"

    def test_below_fm(self):
        assert classify_band(87.7) is None

    def test_vhf_low_edge(self):
        assert classify_band(174) == "VHF"

    def test_vhf_high_edge(self):
        assert classify_band(216) == "VHF"

    def test_vhf_mid(self):
        assert classify_band(195) == "VHF"

    def test_gap_returns_none(self):
        assert classify_band(140) is None

    def test_uhf_low_edge(self):
        assert classify_band(470) == "UHF"

    def test_uhf_high_edge(self):
        assert classify_band(608) == "UHF"

    def test_uhf_mid(self):
        assert classify_band(550) == "UHF"

    def test_above_uhf(self):
        assert classify_band(609) is None


# ── Band taxonomy ────────────────────────────────────────────────────────────


class TestBandTaxonomy:
    def test_all_bands_is_fm_vhf_uhf(self):
        import services.tower_ranking as _tr

        assert _tr.ALL_BANDS == frozenset({"FM", "VHF", "UHF"})

    def test_fm_only_is_fm(self):
        assert FM_ONLY == frozenset({"FM"})


# ── Haversine ────────────────────────────────────────────────────────────────


class TestHaversine:
    def test_same_point_zero(self):
        assert haversine(0, 0, 0, 0) == 0.0

    def test_known_distance(self):
        # Sydney → Melbourne ≈ 714 km
        d = haversine(-33.87, 151.21, -37.81, 144.96)
        assert 700 < d < 730


# ── Bearing ──────────────────────────────────────────────────────────────────


class TestBearing:
    def test_due_north(self):
        b = initial_bearing(0, 0, 1, 0)
        assert abs(b) < 1.0 or abs(b - 360) < 1.0

    def test_due_east(self):
        b = initial_bearing(0, 0, 0, 1)
        assert abs(b - 90) < 1.0

    def test_cardinal_north(self):
        assert bearing_to_cardinal(0) == "N"

    def test_cardinal_south(self):
        assert bearing_to_cardinal(180) == "S"

    def test_cardinal_wrap(self):
        assert bearing_to_cardinal(359) == "N"


# ── FSPL ─────────────────────────────────────────────────────────────────────


class TestFSPL:
    def test_zero_distance_returns_zero(self):
        assert fspl(0, 100) == 0.0

    def test_zero_freq_returns_zero(self):
        assert fspl(10, 0) == 0.0

    def test_positive_loss(self):
        assert fspl(10, 100) > 0


# ── Watts to dBm ─────────────────────────────────────────────────────────────


class TestWattsToDbm:
    def test_one_watt(self):
        assert abs(watts_to_dbm(1.0) - 30.0) < 0.01

    def test_zero_returns_neg_inf(self):
        assert watts_to_dbm(0) == float("-inf")

    def test_negative_returns_neg_inf(self):
        assert watts_to_dbm(-1) == float("-inf")


# ── Parse geometry ───────────────────────────────────────────────────────────


class TestParseGeom:
    def test_point_wkt(self):
        result = parse_geom({"string": "POINT(151.2 -33.87)"})
        assert result is not None
        lat, lon = result
        assert abs(lat - (-33.87)) < 0.01
        assert abs(lon - 151.2) < 0.01

    def test_none_input(self):
        assert parse_geom(None) is None

    def test_empty_string(self):
        assert parse_geom({"string": ""}) is None

    def test_plain_string(self):
        result = parse_geom("POINT(0 0)")
        assert result is not None


# ── process_and_rank ─────────────────────────────────────────────────────────

# Atlanta, GA — used as our fixed "user" position throughout these tests
_USER_LAT = 33.749
_USER_LON = -84.388


# _device / _system are the canonical factories from tests._helpers (imported
# at the top), shared with test_towers_routes.py so the two can't drift.

# A valid FM tower ~20 km north of Atlanta — well within the default 80 km radius
_FM_DEVICE = _device(freq_mhz=95.5, lat=33.93, lon=-84.388, callsign="WXYZ")
_FM_SYSTEM = _system([_FM_DEVICE], licence_type="Broadcast", licence_subtype="FM")

# Same spot, a UHF tower, used to pin the FREQUENCY_MATCH_TOLERANCE_MHZ (5.0)
# boundary at a decimal pair with float noise (see TestUserFrequencyRanking).
_UHF_BOUNDARY_DEVICE = _device(freq_mhz=507.2, lat=33.93, lon=-84.388, callsign="WBND")
_UHF_BOUNDARY_SYSTEM = _system([_UHF_BOUNDARY_DEVICE], licence_type="Broadcast", licence_subtype="TV")


class TestProcessAndRank:
    # ── Basic smoke tests ────────────────────────────────────────────────────

    def test_empty_input_returns_empty(self):
        result = process_and_rank([], _USER_LAT, _USER_LON)
        assert result == []

    def test_empty_devices_returns_empty(self):
        result = process_and_rank([_system([])], _USER_LAT, _USER_LON)
        assert result == []

    # ── Single valid FM tower ────────────────────────────────────────────────

    def test_single_fm_tower_returned(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        assert len(result) == 1

    def test_single_fm_tower_fields(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        t = result[0]
        assert t["callsign"] == "WXYZ"
        assert t["frequency_mhz"] == 95.5
        assert t["band"] == "FM"
        assert t["rank"] == 1
        assert isinstance(t["distance_km"], float)
        assert isinstance(t["bearing_deg"], float)
        assert isinstance(t["bearing_cardinal"], str)
        assert isinstance(t["received_power_dbm"], float)
        assert isinstance(t["eirp_dbm"], float)
        assert "distance_class" not in t
        assert t["licence_type"] == "Broadcast"
        assert t["licence_subtype"] == "FM"
        assert t["frequency_matched"] is False
        assert t["shared_callsigns"] == []

    def test_single_fm_tower_distance_reasonable(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        # Tower is ~20 km north — should be between 15 and 25 km
        assert 15.0 < result[0]["distance_km"] < 25.0

    def test_single_fm_tower_bearing_roughly_north(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        # Tower is directly north; bearing should be close to 0/360
        brg = result[0]["bearing_deg"]
        assert brg < 10 or brg > 350

    # ── Radius filtering ─────────────────────────────────────────────────────

    def test_tower_beyond_radius_excluded(self):
        # Tower is ~20 km away; use a 10 km radius — should be excluded
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, radius_km=10)
        assert result == []

    def test_tower_within_explicit_radius_included(self):
        # Tower is ~20 km away; use a 50 km radius — should be included
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, radius_km=50)
        assert len(result) == 1

    def test_zero_radius_uses_default(self):
        # radius_km=0 should fall back to DEFAULT_RADIUS_KM (80 km).
        # Near tower is ~20 km away (within 80 km); far tower is ~140 km away (clearly beyond).
        far_device = _device(freq_mhz=95.5, lat=35.0, lon=-84.388, callsign="KFAR")
        near_device = _FM_DEVICE  # ~20 km north, callsign WXYZ
        system = _system([near_device, far_device])
        result = process_and_rank([system], _USER_LAT, _USER_LON, radius_km=0)
        callsigns = {t["callsign"] for t in result}
        assert "WXYZ" in callsigns, "Near tower (~20 km) should be included within default 80 km radius"
        assert "KFAR" not in callsigns, "Far tower (~140 km) should be excluded by default 80 km radius"

    # ── Band filtering ───────────────────────────────────────────────────────

    def test_non_broadcast_frequency_excluded(self):
        # 300 MHz falls in no recognised broadcast band
        bad_device = _device(freq_mhz=300.0, lat=33.93, lon=-84.388, callsign="KBAD")
        result = process_and_rank([_system([bad_device])], _USER_LAT, _USER_LON)
        assert result == []

    def test_none_frequency_excluded(self):
        bad_device = {
            "frequency": None,
            "callsign": "KNONE",
            "location": {"geom": "POINT(-84.388 33.93)"},
        }
        result = process_and_rank([_system([bad_device])], _USER_LAT, _USER_LON)
        assert result == []

    # ── allowed_bands filtering (ATSC-region gating) ─────────────────────────

    def test_fm_only_excludes_tv_keeps_fm(self):
        # FM_ONLY should drop VHF and UHF (TV) towers but keep the FM tower.
        fm = _device(freq_mhz=95.5, lat=33.85, lon=-84.388, callsign="KFM")
        vhf = _device(freq_mhz=195.0, lat=33.85, lon=-84.388, callsign="KVHF")
        uhf = _device(freq_mhz=545.0, lat=33.85, lon=-84.388, callsign="KUHF")
        result = process_and_rank([_system([fm, vhf, uhf])], _USER_LAT, _USER_LON, allowed_bands=FM_ONLY)
        bands = {t["band"] for t in result}
        callsigns = {t["callsign"] for t in result}
        assert bands == {"FM"}
        assert callsigns == {"KFM"}

    def test_default_allowed_bands_keeps_tv(self):
        # The allowed_bands default is "unrestricted": omitting it keeps every
        # band, incl. TV. Callers opt into narrowing (e.g. FM_ONLY for non-ATSC
        # regions) rather than opting out of it.
        vhf = _device(freq_mhz=195.0, lat=33.85, lon=-84.388, callsign="KVHF")
        uhf = _device(freq_mhz=545.0, lat=33.85, lon=-84.388, callsign="KUHF")
        result = process_and_rank([_system([vhf, uhf])], _USER_LAT, _USER_LON)
        bands = {t["band"] for t in result}
        assert "VHF" in bands
        assert "UHF" in bands

    def test_fm_only_filters_before_limit(self):
        # TV outranks FM (band_priority). With several TV devices plus one FM
        # device and a small limit, FM_ONLY must still return the FM tower —
        # proving the band filter runs BEFORE truncation, not after.
        tv_devices = [_device(freq_mhz=195.0, lat=33.85 + i * 0.001, lon=-84.388, callsign=f"KTV{i}") for i in range(5)]
        fm = _device(freq_mhz=95.5, lat=33.85, lon=-84.388, callsign="KFM")
        result = process_and_rank(
            [_system(tv_devices + [fm])],
            _USER_LAT,
            _USER_LON,
            limit=1,
            allowed_bands=FM_ONLY,
        )
        assert len(result) == 1
        assert result[0]["callsign"] == "KFM"
        assert result[0]["band"] == "FM"

    # ── Geometry filtering ───────────────────────────────────────────────────

    def test_device_with_no_geom_excluded(self):
        no_geom_device = {
            "frequency": 95.5,
            "callsign": "KNOGEOM",
            "location": {"geom": None},
        }
        result = process_and_rank([_system([no_geom_device])], _USER_LAT, _USER_LON)
        assert result == []

    def test_device_with_missing_location_excluded(self):
        no_loc_device = {
            "frequency": 95.5,
            "callsign": "KNOLOC",
        }
        result = process_and_rank([_system([no_loc_device])], _USER_LAT, _USER_LON)
        assert result == []

    # ── Deduplication ────────────────────────────────────────────────────────

    def test_deduplication_keeps_stronger_signal(self):
        # Two devices with the same callsign+frequency but different distances
        # The closer one (stronger signal) should win
        closer = _device(95.5, 33.85, -84.388, callsign="KDUP")  # ~11 km
        farther = _device(95.5, 33.99, -84.388, callsign="KDUP")  # ~27 km
        result = process_and_rank([_system([closer, farther])], _USER_LAT, _USER_LON)
        assert len(result) == 1
        # Closer tower should be kept (higher received_power_dbm)
        assert result[0]["distance_km"] < 20.0

    def test_deduplication_different_callsigns_both_kept(self):
        dev1 = _device(95.5, 33.85, -84.388, callsign="KAAA")
        dev2 = _device(95.5, 33.86, -84.388, callsign="KBBB")
        result = process_and_rank([_system([dev1, dev2])], _USER_LAT, _USER_LON)
        assert len(result) == 2

    def test_deduplication_different_frequencies_both_kept(self):
        dev1 = _device(95.5, 33.85, -84.388, callsign="KSAME")
        dev2 = _device(101.1, 33.85, -84.388, callsign="KSAME")
        result = process_and_rank([_system([dev1, dev2])], _USER_LAT, _USER_LON)
        assert len(result) == 2

    # ── Limit parameter ──────────────────────────────────────────────────────

    def test_limit_restricts_output_count(self):
        devices = [
            _device(95.5, 33.85, -84.388, callsign="K001"),
            _device(97.1, 33.85, -84.388, callsign="K002"),
            _device(99.3, 33.85, -84.388, callsign="K003"),
        ]
        result = process_and_rank([_system(devices)], _USER_LAT, _USER_LON, limit=2)
        assert len(result) == 2

    def test_limit_zero_uses_default(self):
        # Create DEFAULT_LIMIT + 1 devices so limit=0 (→ DEFAULT_LIMIT) actually caps output.
        # Devices use slightly different latitudes to avoid deduplication.
        devices = [
            _device(95.5 + i * 0.1, _USER_LAT + i * 0.001, _USER_LON, callsign=f"K{i:03d}")
            for i in range(DEFAULT_LIMIT + 1)
        ]
        result_default = process_and_rank([_system(devices)], _USER_LAT, _USER_LON, limit=0)
        assert len(result_default) == DEFAULT_LIMIT, f"limit=0 should fall back to DEFAULT_LIMIT ({DEFAULT_LIMIT})"
        result_one = process_and_rank([_system(devices)], _USER_LAT, _USER_LON, limit=1)
        assert len(result_one) == 1

    # ── frequency_matched flag ───────────────────────────────────────────────

    def test_frequency_matched_false_when_no_measurements(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        assert result[0]["frequency_matched"] is False

    # ── Default EIRP fallback ────────────────────────────────────────────────

    def test_fm_default_eirp_used_when_missing(self):
        device_no_eirp = {
            "frequency": 95.5,
            "callsign": "KNOEIRP",
            "location": {"geom": "POINT(-84.388 33.85)"},
            # no eirp_dbm, no eirp, no transmitPower
        }
        result = process_and_rank([_system([device_no_eirp])], _USER_LAT, _USER_LON)
        assert len(result) == 1
        # FM default is 50.0 dBm
        assert result[0]["eirp_dbm"] == 50.0

    def test_vhf_default_eirp_used_when_missing(self):
        device_no_eirp = {
            "frequency": 180.0,  # VHF band
            "callsign": "KVHF",
            "location": {"geom": "POINT(-84.388 33.85)"},
        }
        result = process_and_rank([_system([device_no_eirp])], _USER_LAT, _USER_LON)
        assert len(result) == 1
        # non-FM default is 60.0 dBm
        assert result[0]["eirp_dbm"] == 60.0

    # ── Rank assignment ──────────────────────────────────────────────────────

    def test_rank_is_one_based(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        assert result[0]["rank"] == 1

    def test_rank_sequence_correct(self):
        devices = [
            _device(95.5, 33.85, -84.388, callsign="K001"),
            _device(97.1, 33.86, -84.388, callsign="K002"),
            _device(99.3, 33.87, -84.388, callsign="K003"),
        ]
        result = process_and_rank([_system(devices)], _USER_LAT, _USER_LON)
        ranks = [t["rank"] for t in result]
        assert ranks == list(range(1, len(result) + 1))

    def test_limit_one_returns_only_best_tower(self):
        # With limit=1, only the single highest-ranked tower is returned.
        devices = [
            _device(95.5, 33.85, -84.388, callsign="K001"),
            _device(97.1, 33.86, -84.388, callsign="K002"),
        ]
        result = process_and_rank([_system(devices)], _USER_LAT, _USER_LON, limit=1)
        assert len(result) == 1
        assert result[0]["rank"] == 1

    # ── Sensitivity filter ───────────────────────────────────────────────────

    def test_tower_below_sensitivity_excluded(self):
        # Mock received_power to return SENSITIVITY_DBM - 1 so the sensitivity filter
        # is exercised regardless of the actual path-loss calculation.
        from unittest.mock import patch

        import services.tower_ranking as _tr

        near_device = _device(freq_mhz=95.5, lat=33.85, lon=_USER_LON, callsign="KWEAK")
        below_sensitivity = SENSITIVITY_DBM - 1.0

        with patch.object(_tr, "received_power", return_value=below_sensitivity):
            result = process_and_rank([_system([near_device])], _USER_LAT, _USER_LON)

        assert result == [], (
            f"Tower whose received power ({below_sensitivity} dBm) is below "
            f"SENSITIVITY_DBM ({SENSITIVITY_DBM} dBm) should be excluded"
        )

    # ── Output field completeness ────────────────────────────────────────────

    def test_output_contains_all_expected_fields(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        t = result[0]
        expected_fields = {
            "callsign",
            "name",
            "state",
            "frequency_mhz",
            "band",
            "latitude",
            "longitude",
            "antenna_height_m",
            "distance_km",
            "bearing_deg",
            "bearing_cardinal",
            "received_power_dbm",
            "eirp_dbm",
            "licence_type",
            "licence_subtype",
            "frequency_matched",
            "rank",
            # Spectrum-analyser fields always present (None when no measurement)
            "measured",
            "snr_db",
            "score",
            "power_db",
            "obw_fraction",
            # Channel-sharing merge — always present, empty when the tower stands alone
            "shared_callsigns",
            # Detection-area model — always present, 0.0 for a tower it cannot score
            "expected_area_km2",
            "best_azimuth_deg",
            "horizon_km",
        }
        assert expected_fields.issubset(t.keys())

    def test_the_fields_our_consumers_read_keep_their_names_and_types(self):
        """retina-gui and retina-spectrum read this response.

        The ranking redesign only adds fields. A rename or a type change here
        is a broken map pin or a blank column in another repo, found at
        runtime, so the contract is pinned rather than described.
        """
        t = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)[0]
        consumed = {
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
        for field, expected_type in consumed.items():
            assert isinstance(t[field], expected_type), f"{field} is {type(t[field]).__name__}"
        # power_db and altitude_m are nullable: the first comes from a
        # measurement, the second from the elevation enrichment in the route.
        assert "power_db" in t

    def test_the_model_fields_are_json_serialisable_numbers(self):
        """numpy scalars would pass every assertion above and then fail
        json.dumps in the route, which is a 500 on the towers endpoint."""
        t = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)[0]
        for field in ("expected_area_km2", "best_azimuth_deg", "horizon_km"):
            assert type(t[field]) is float, field
        json.dumps({k: v for k, v in t.items() if k != "antenna_height_m"})

    def test_no_measurements_fields_are_none(self):
        """When no measurements provided, analyser fields should all be None/False."""
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON)
        t = result[0]
        assert t["measured"] is False
        assert t["snr_db"] is None
        assert t["score"] is None
        assert t["power_db"] is None
        assert t["obw_fraction"] is None

    # ── Multiple systems ─────────────────────────────────────────────────────

    def test_devices_from_multiple_systems_aggregated(self):
        system1 = _system([_device(95.5, 33.85, -84.388, callsign="K001")])
        system2 = _system([_device(97.1, 33.86, -84.388, callsign="K002")])
        result = process_and_rank([system1, system2], _USER_LAT, _USER_LON)
        callsigns = {t["callsign"] for t in result}
        assert "K001" in callsigns
        assert "K002" in callsigns


# ── Shared-transmitter merge (FCC channel-sharing, LPFM time-shares) ─────────


class TestSharedTransmitterMerge:
    # ~150 m of latitude — within SHARED_TRANSMITTER_RADIUS_KM (0.2 km).
    _NEARBY_LAT_OFFSET = 0.00135

    def test_channel_sharing_pair_merges_to_one_tower(self):
        # WNTV + WRET-TV: two callsigns, one ATSC multiplex, same transmitter.
        wntv = _device(183.0, 33.85, -84.388, callsign="WNTV", eirp=10000)
        wret = _device(183.0, 33.85, -84.388, callsign="WRET-TV", eirp=10000)
        result = process_and_rank([_system([wntv, wret])], _USER_LAT, _USER_LON)
        assert len(result) == 1
        assert result[0]["callsign"] == "WNTV"
        assert result[0]["shared_callsigns"] == ["WRET-TV"]

    def test_three_way_lpfm_time_share_merges_to_one_tower(self):
        devices = [
            _device(101.1, 33.85, -84.388, callsign="WBRU-LP", eirp=100),
            _device(101.1, 33.85, -84.388, callsign="WFOO-LP", eirp=100),
            _device(101.1, 33.85, -84.388, callsign="WVVX-LP", eirp=100),
        ]
        result = process_and_rank([_system(devices)], _USER_LAT, _USER_LON)
        assert len(result) == 1
        t = result[0]
        assert t["callsign"] in {"WBRU-LP", "WFOO-LP", "WVVX-LP"}
        others = sorted({"WBRU-LP", "WFOO-LP", "WVVX-LP"} - {t["callsign"]})
        assert t["shared_callsigns"] == others

    def test_same_frequency_distant_sites_not_merged(self):
        near = _device(183.0, 33.85, -84.388, callsign="KNEAR")
        far = _device(183.0, 33.85 + 0.05, -84.388, callsign="KFAR")  # ~5.5 km away
        result = process_and_rank([_system([near, far])], _USER_LAT, _USER_LON)
        assert len(result) == 2
        assert all(t["shared_callsigns"] == [] for t in result)

    def test_same_site_different_frequencies_not_merged(self):
        a = _device(183.0, 33.85, -84.388, callsign="KAAA")
        b = _device(189.0, 33.85, -84.388, callsign="KBBB")
        result = process_and_rank([_system([a, b])], _USER_LAT, _USER_LON)
        assert len(result) == 2
        assert all(t["shared_callsigns"] == [] for t in result)

    def test_stronger_signal_is_the_primary(self):
        weak = _device(183.0, 33.85, -84.388, callsign="WEAK", eirp=1000)
        strong = _device(183.0, 33.85, -84.388, callsign="STRONG", eirp=100000)
        merged = process_and_rank([_system([weak, strong])], _USER_LAT, _USER_LON)
        assert len(merged) == 1
        assert merged[0]["callsign"] == "STRONG"
        assert merged[0]["shared_callsigns"] == ["WEAK"]

        # Compare against a run with only the strong device — eirp_dbm must match.
        solo_device = _device(183.0, 33.85, -84.388, callsign="STRONG", eirp=100000)
        solo = process_and_rank([_system([solo_device])], _USER_LAT, _USER_LON)
        assert merged[0]["eirp_dbm"] == solo[0]["eirp_dbm"]

    def test_sites_150m_apart_still_merge(self):
        a = _device(183.0, 33.85, -84.388, callsign="WNTV")
        b = _device(183.0, 33.85 + self._NEARBY_LAT_OFFSET, -84.388, callsign="WRET-TV")
        result = process_and_rank([_system([a, b])], _USER_LAT, _USER_LON)
        assert len(result) == 1
        assert result[0]["shared_callsigns"] == ["WRET-TV"]

    def test_ranks_stay_contiguous_after_merge(self):
        wntv = _device(183.0, 33.85, -84.388, callsign="WNTV")
        wret = _device(183.0, 33.85, -84.388, callsign="WRET-TV")
        standalone = _device(95.5, 33.86, -84.388, callsign="KSOLO")
        result = process_and_rank([_system([wntv, wret, standalone])], _USER_LAT, _USER_LON)
        assert len(result) == 2
        assert sorted(t["rank"] for t in result) == [1, 2]

    def test_existing_same_callsign_dedup_still_applies(self):
        # Same callsign, same site, same frequency — the pre-existing (callsign,
        # frequency) dedup collapses these before the shared-transmitter merge
        # ever runs, so there is no self-entry in shared_callsigns.
        dup1 = _device(183.0, 33.85, -84.388, callsign="WNTV")
        dup2 = _device(183.0, 33.85, -84.388, callsign="WNTV")
        result = process_and_rank([_system([dup1, dup2])], _USER_LAT, _USER_LON)
        assert len(result) == 1
        assert result[0]["shared_callsigns"] == []


# ── _as_float ────────────────────────────────────────────────────────────────


class TestAsFloat:
    def test_string_numeric(self):
        assert _as_float("3.14") == pytest.approx(3.14)

    def test_string_invalid_returns_none(self):
        assert _as_float("not_a_number") is None

    def test_dict_value_key(self):
        assert _as_float({"value": 5.5}) == pytest.approx(5.5)

    def test_dict_low_high_keys_averages(self):
        assert _as_float({"low": 1.0, "high": 3.0}) == pytest.approx(2.0)

    def test_dict_unknown_keys_returns_none(self):
        assert _as_float({"other": 5.0}) is None


# ── eirp_dbm_from_device ─────────────────────────────────────────────────────


class TestEirpDbmFromDevice:
    def test_eirp_watts_converted_to_dbm(self):
        result = eirp_dbm_from_device({"eirp": 100})
        assert result == pytest.approx(watts_to_dbm(100))

    def test_transmit_power_with_antenna_gain(self):
        result = eirp_dbm_from_device({"transmitPower": 100, "antenna": {"gain": 6}})
        assert result == pytest.approx(watts_to_dbm(100) + 6)

    def test_transmit_power_no_antenna_uses_default_10dbi(self):
        result = eirp_dbm_from_device({"transmitPower": 100})
        assert result == pytest.approx(watts_to_dbm(100) + 10.0)

    def test_no_power_fields_returns_none(self):
        assert eirp_dbm_from_device({}) is None


# ── parse_geom edge cases ─────────────────────────────────────────────────────


class TestParseGeomEdgeCases:
    def test_point_single_coord_returns_none(self):
        """POINT with only one token inside → len(parts) < 2 → None."""
        assert parse_geom("POINT(123)") is None

    def test_polygon_no_parens_returns_none(self):
        """POLYGON WKT with no parentheses → regex doesn't match → None."""
        assert parse_geom("POLYGON xyz") is None

    def test_polygon_bare_minus_coord_skipped(self):
        """POLYGON where one pair has bare '-' → ValueError → continue; valid pairs still used."""
        result = parse_geom("POLYGON((0 0, - 1, 2 2))")
        assert result is not None

    def test_polygon_all_invalid_coords_returns_none(self):
        """POLYGON where all coord pairs have bare '-' → empty lats list → None."""
        assert parse_geom("POLYGON((- -, - -))") is None


# ── _match_measurement ────────────────────────────────────────────────────────


def _make_measurement(
    freq_mhz: float,
    band: str = "FM",
    snr_db: float = 30.0,
    obw_fraction: float = 0.5,
    score: float = 0.8,
    power_db: float = -60.0,
) -> dict:
    return {
        "freq_mhz": freq_mhz,
        "band": band,
        "snr_db": snr_db,
        "obw_fraction": obw_fraction,
        "score": score,
        "power_db": power_db,
    }


class TestMatchMeasurement:
    def test_exact_match_fm(self):
        m = _make_measurement(95.5, band="FM")
        assert _match_measurement(95.5, "FM", [m]) is m

    def test_within_fm_tolerance(self):
        m = _make_measurement(95.5, band="FM")
        # 0.10 MHz offset — safely within ±0.15 MHz, avoids float rounding edge
        assert _match_measurement(95.60, "FM", [m]) is m

    def test_outside_fm_tolerance(self):
        m = _make_measurement(95.5, band="FM")
        # 0.20 MHz offset — safely outside ±0.15 MHz
        assert _match_measurement(95.70, "FM", [m]) is None

    def test_decimal_exact_fm_boundary_matches(self):
        """abs(88.25 - 88.10) is 0.15000000000000568 in IEEE-754, not 0.15,
        a raw float `<=` against the 0.15 MHz FM tolerance rejects this
        boundary pair even though the two are exactly 0.15 MHz apart."""
        m = _make_measurement(88.25, band="FM")
        assert _match_measurement(88.10, "FM", [m]) is m

    def test_within_uhf_tolerance(self):
        m = _make_measurement(546.0, band="UHF")
        # ±4 MHz tolerance for UHF
        assert _match_measurement(549.9, "UHF", [m]) is m

    def test_outside_uhf_tolerance(self):
        m = _make_measurement(546.0, band="UHF")
        assert _match_measurement(550.5, "UHF", [m]) is None

    def test_within_vhf_tolerance(self):
        m = _make_measurement(194.0, band="VHF")
        assert _match_measurement(197.9, "VHF", [m]) is m

    def test_empty_measurements_returns_none(self):
        assert _match_measurement(95.5, "FM", []) is None

    def test_closest_wins_when_multiple_in_tolerance(self):
        m_close = _make_measurement(95.5, band="FM")  # 0.02 MHz from query
        m_far = _make_measurement(95.4, band="FM")  # 0.12 MHz from query
        # Query at 95.48 — both within ±0.15 but 95.5 is genuinely closer
        result = _match_measurement(95.48, "FM", [m_far, m_close])
        assert result is m_close

    def test_fm_tolerance_tighter_than_vhf(self):
        """FM tolerance (0.15 MHz) must be tighter than VHF/UHF tolerance (4 MHz)."""
        assert MEASUREMENT_TOLERANCE_MHZ["FM"] < MEASUREMENT_TOLERANCE_MHZ["VHF"]
        assert MEASUREMENT_TOLERANCE_MHZ["FM"] < MEASUREMENT_TOLERANCE_MHZ["UHF"]


# ── process_and_rank with measurements ───────────────────────────────────────


class TestProcessAndRankMeasurements:
    def test_matched_tower_has_measured_true(self):
        m = _make_measurement(95.5, band="FM", snr_db=28.5, score=0.75, power_db=-62.0, obw_fraction=0.03)
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, measurements=[m])
        t = result[0]
        assert t["measured"] is True
        assert t["snr_db"] == pytest.approx(28.5)
        assert t["score"] == pytest.approx(0.75)
        assert t["power_db"] == pytest.approx(-62.0)
        assert t["obw_fraction"] == pytest.approx(0.03)

    def test_matched_tower_sets_frequency_matched(self):
        """A measurement match should also set frequency_matched=True."""
        m = _make_measurement(95.5, band="FM")
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, measurements=[m])
        assert result[0]["frequency_matched"] is True

    def test_unmatched_tower_excluded_when_measurements_provided(self):
        # Measurement is on 101.1 MHz; tower is on 95.5 MHz — outside FM tolerance.
        # The SDR can't see this tower, so it must be dropped from the results entirely.
        m = _make_measurement(101.1, band="FM")
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, measurements=[m])
        assert result == [], "A tower with no matching measurement should be excluded — the SDR cannot see it"

    def test_only_matched_towers_returned_when_measurements_provided(self):
        # Two towers: one on the measured frequency, one not.
        # Only the matched tower should appear in results.
        m = _make_measurement(95.5, band="FM")
        other_device = _device(freq_mhz=101.1, lat=33.93, lon=-84.388, callsign="KOTHER")
        other_system = _system([other_device])
        result = process_and_rank([_FM_SYSTEM, other_system], _USER_LAT, _USER_LON, measurements=[m])
        assert len(result) == 1
        assert result[0]["callsign"] == "WXYZ"
        assert result[0]["measured"] is True

    def test_empty_measurements_list_behaves_as_no_measurements(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, measurements=[])
        t = result[0]
        assert t["measured"] is False
        assert t["snr_db"] is None


def test_allowed_bands_for_region():
    # ATSC-capable regions get all bands; everyone else gets FM only.
    import services.tower_ranking as tower_ranking

    assert tower_ranking.allowed_bands_for_region("us") == frozenset({"FM", "VHF", "UHF"})
    assert tower_ranking.allowed_bands_for_region("au") == frozenset({"FM"})


# ── User frequency parsing (GET ?frequencies=) ───────────────────────────────


class TestParseUserFrequencies:
    def test_empty_string(self):
        assert parse_user_frequencies("") == []

    def test_single_freq(self):
        assert parse_user_frequencies("95.5") == [95.5]

    def test_multiple_freqs(self):
        assert parse_user_frequencies("95.5, 177.5, 500") == [95.5, 177.5, 500]

    def test_trailing_comma(self):
        assert parse_user_frequencies("95.5,") == [95.5]

    def test_invalid_values_skipped(self):
        assert parse_user_frequencies("abc, 95.5, xyz") == [95.5]

    def test_unicode_decimal_digits_parse_as_the_number_they_spell(self):
        """float() accepts Unicode decimal digits, so these parse rather than
        being rejected. Left as-is deliberately: the result is a bounded float
        used only for ranking and echoed back, so there is nothing for an
        unusual spelling to exploit, and rejecting it would silently narrow
        input the service accepts today."""
        assert parse_user_frequencies("९२.5") == [92.5]  # Devanagari "92.5"

    def test_max_10_enforced(self):
        assert len(parse_user_frequencies(",".join(str(i) for i in range(1, 20)))) == 10

    def test_zero_skipped(self):
        assert parse_user_frequencies("0, 95.5") == [95.5]

    def test_negative_skipped(self):
        assert parse_user_frequencies("-5, 95.5") == [95.5]

    def test_a_long_junk_run_does_not_hide_a_later_valid_value(self):
        """Junk never trips max_count, so only a ceiling on how much is
        examined could stop the scan before 95.5. There is none, by design."""
        raw = ",".join(["junk"] * 10_000 + ["95.5"])
        assert parse_user_frequencies(raw) == [95.5]

    def test_a_sparse_comma_grid_still_yields_its_one_value(self):
        """A client building the list from a fixed channel grid sends mostly
        empty slots. At 215 bytes this is far inside every transport limit, so
        the one value set must survive however many empty siblings precede
        it."""
        raw = ",".join([""] * 211 + ["95.5"])
        assert parse_user_frequencies(raw) == [95.5]

    def test_oversized_token_is_read_whole_never_truncated(self):
        """An absurdly long number is rejected for being out of range, not cut
        down to a shorter one: 33 nines must yield nothing, never 9.9 or
        999."""
        assert parse_user_frequencies("9" * 33) == []

    def test_a_long_winded_spelling_of_a_valid_value_still_counts(self):
        """95.5 padded to 44 characters is a silly way to write it but an
        unambiguous one, and the range gate already stops every over-long
        number."""
        assert parse_user_frequencies("95." + "5" + "0" * 40) == [95.5]

    def test_an_oversized_value_does_not_discard_its_siblings(self):
        """An oversized value must discard only itself, so the result cannot
        depend on the order the caller sent them in."""
        huge = "9" * 5000
        assert parse_user_frequencies([huge, "95.5"]) == [95.5]
        assert parse_user_frequencies(["95.5", huge]) == [95.5]

    def test_occurrence_list_and_comma_string_agree(self):
        """The comma-separated form predates the repeated key and is still
        what retina-server's frontend sends; both spellings of the same
        request must give the same answer, at any length."""
        assert parse_user_frequencies("95.5,101.1") == parse_user_frequencies(["95.5", "101.1"])
        assert parse_user_frequencies(["95.5,101.1", "88.1"]) == [95.5, 101.1, 88.1]

        many = ["junk"] * 400 + ["95.5"]
        assert parse_user_frequencies(",".join(many)) == parse_user_frequencies(many)

    def test_absent_input_is_empty_not_an_error(self):
        """None reaches this from any caller that models the parameter as
        optional; it must read as "nothing supplied", not raise."""
        assert parse_user_frequencies(None) == []
        assert parse_user_frequencies([]) == []
        assert parse_user_frequencies("") == []


# ── User frequencies in ranking ──────────────────────────────────────────────


class TestUserFrequencyRanking:
    """Boost semantics: matched towers sort first, nothing is dropped —
    unlike ``measurements``, which filters to what the SDR can see."""

    # A second FM tower whose frequency is far from every user frequency used
    # below, same location so distance cannot decide the order.
    _OTHER_DEVICE = _device(freq_mhz=107.9, lat=33.93, lon=-84.388, callsign="KFAR")
    _OTHER_SYSTEM = _system([_OTHER_DEVICE], licence_type="Broadcast", licence_subtype="FM")

    def test_matched_tower_flagged_and_sorted_first(self):
        result = process_and_rank(
            [self._OTHER_SYSTEM, _FM_SYSTEM],
            _USER_LAT,
            _USER_LON,
            user_frequencies=[95.5],
        )
        assert len(result) == 2  # boost, not filter
        assert result[0]["callsign"] == "WXYZ"
        assert result[0]["frequency_matched"] is True
        assert result[1]["frequency_matched"] is False

    def test_match_within_tolerance(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, user_frequencies=[99.0])
        assert result[0]["frequency_matched"] is True  # |95.5 - 99.0| <= 5 MHz

    def test_no_match_outside_tolerance(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, user_frequencies=[101.1])
        assert result[0]["frequency_matched"] is False

    def test_decimal_exact_boundary_matches(self):
        """abs(507.2 - 512.2) is 5.000000000000057 in IEEE-754, not 5.0, a
        raw float `<=` against FREQUENCY_MATCH_TOLERANCE_MHZ (5.0) rejects
        this boundary pair even though the two are exactly 5.0 MHz apart."""
        result = process_and_rank([_UHF_BOUNDARY_SYSTEM], _USER_LAT, _USER_LON, user_frequencies=[512.2])
        assert result[0]["frequency_matched"] is True

    def test_user_freqs_never_set_measured(self):
        result = process_and_rank([_FM_SYSTEM], _USER_LAT, _USER_LON, user_frequencies=[95.5])
        t = result[0]
        assert t["frequency_matched"] is True
        assert t["measured"] is False
        assert t["snr_db"] is None

    def test_user_freqs_do_not_exempt_from_measurement_filter(self):
        # A measurement matching only WXYZ must still drop KFAR, even when a
        # user frequency matches KFAR: hand-typed values say nothing about
        # what the SDR can actually see.
        m = {
            "freq_mhz": 95.5,
            "snr_db": 30.0,
            "obw_fraction": 0.03,
            "score": 0.75,
            "power_db": -62.0,
            "band": "FM",
        }
        result = process_and_rank(
            [_FM_SYSTEM, self._OTHER_SYSTEM],
            _USER_LAT,
            _USER_LON,
            measurements=[m],
            user_frequencies=[107.9],
        )
        assert [t["callsign"] for t in result] == ["WXYZ"]
