"""Tests for the bistatic detection-area model (services/tower_scoring.py).

These pin the physics the ranking now leans on, not the arithmetic: each one
states a claim an operator would recognise ("a tower over the horizon is worth
less than one you can see") and would notice being wrong.
"""

import math
import random
import time
from dataclasses import replace

import pytest
from services.tower_scoring import (
    DEFAULT_ANTENNA_HEIGHT_M,
    FleetPrediction,
    ScoringParams,
    class_detection_areas,
    expected_detection_area,
    horizon_loss_db,
    radio_horizon_km,
    score_towers,
)

_PARAMS = ScoringParams()

# Atlanta, GA — the same receiver position the ranking tests use.
_RX_LAT, _RX_LON = 33.749, -84.388


def _tower(
    *,
    callsign="KXXX",
    band="UHF",
    freq_mhz=550.0,
    eirp_dbm=80.0,
    lat=_RX_LAT,
    lon=_RX_LON,
    antenna_height_m=300.0,
    **extra,
) -> dict:
    """A tower dict shaped like the ones process_and_rank builds."""
    return {
        "callsign": callsign,
        "band": band,
        "frequency_mhz": freq_mhz,
        "eirp_dbm": eirp_dbm,
        "latitude": lat,
        "longitude": lon,
        "antenna_height_m": antenna_height_m,
        **extra,
    }


def _north_of_rx(km: float) -> float:
    """Latitude `km` north of the receiver, in the model's flat-earth scaling."""
    return _RX_LAT + km / 111.2


def _area(distance_km, *, band="UHF", freq_mhz=550.0, eirp_dbm=80.0, height_m=400.0, params=_PARAMS) -> float:
    """Detection area for a tower due north at `distance_km`."""
    return expected_detection_area(
        0.0, float(distance_km), eirp_dbm, freq_mhz, band, height_m, params
    ).expected_area_km2


# ── Radio horizon ────────────────────────────────────────────────────────────


class TestHorizon:
    def test_four_thirds_earth_formula(self):
        # 3.57 * (sqrt(h_t) + sqrt(h_r)), metres in, km out.
        assert radio_horizon_km(100.0, 10.0) == pytest.approx(3.57 * (10.0 + math.sqrt(10.0)))

    def test_taller_mast_sees_further(self):
        assert radio_horizon_km(400.0) > radio_horizon_km(100.0)

    def test_zero_and_negative_heights_are_floored_not_trusted(self):
        # A null/0/negative antennaHeight is a gap in the upstream record, not
        # a tower lying on the ground: both floor at 1 m rather than 0, so the
        # horizon stays finite and positive.
        assert radio_horizon_km(0.0, 0.0) == pytest.approx(3.57 * 2)
        assert radio_horizon_km(-50.0, -1.0) == radio_horizon_km(0.0, 0.0)

    def test_no_loss_inside_the_horizon(self):
        horizon = radio_horizon_km(400.0, 10.0)
        assert horizon_loss_db(horizon - 1.0, 400.0, 10.0) == 0.0
        assert horizon_loss_db(0.0, 400.0, 10.0) == 0.0

    def test_twenty_db_at_the_horizon_then_half_a_db_per_km(self):
        horizon = radio_horizon_km(400.0, 10.0)
        assert horizon_loss_db(horizon + 1e-9, 400.0, 10.0) == pytest.approx(20.0, abs=1e-6)
        assert horizon_loss_db(horizon + 10.0, 400.0, 10.0) == pytest.approx(25.0)

    def test_a_tower_past_the_horizon_scores_below_one_in_it(self):
        """Same EIRP, same spot, different mast height.

        This is the check plain FSPL does not make: at 60 km a 100 m mast is
        over the horizon (~47 km) and a 900 m one is not.
        """
        assert radio_horizon_km(100.0) < 60.0 < radio_horizon_km(900.0)
        over = _area(60.0, height_m=100.0)
        under = _area(60.0, height_m=900.0)
        assert over < under


# ── The core claim: near and loud is not the same as useful ──────────────────


class TestGeometryBeatsLoudness:
    def test_a_megawatt_next_door_scores_below_the_same_tower_far_away(self):
        """A 1 MW transmitter at 5 km is the loudest signal in the band and a
        poor illuminator: its direct path floods the surveillance channel and
        the geometry it offers is a sliver around the baseline. The old
        received-power ranking put it first."""
        near = _area(5.0, eirp_dbm=90.0, height_m=300.0)
        far = _area(50.0, eirp_dbm=90.0, height_m=300.0)
        assert near < far

    def test_area_rises_then_falls_with_distance(self):
        """DPI dominates close in, path loss and the horizon dominate far out,
        so the useful illuminators sit in the middle of the sweep."""
        distances = [2, 5, 10, 15, 20, 30, 40, 50, 60, 80, 100, 120]
        areas = [_area(d) for d in distances]

        peak = areas.index(max(areas))
        assert 0 < peak < len(areas) - 1, f"peak at an end of the sweep: {list(zip(distances, areas))}"
        assert areas[:peak] == sorted(areas[:peak]), "area should rise up to the peak"
        assert areas[peak:] == sorted(areas[peak:], reverse=True), "area should fall after the peak"

    def test_more_eirp_is_still_better_at_a_fixed_distance(self):
        # The model must not have inverted power altogether: at one distance,
        # more EIRP is more area.
        assert _area(30.0, eirp_dbm=90.0) > _area(30.0, eirp_dbm=70.0)


# ── Band handling ────────────────────────────────────────────────────────────


class TestBands:
    def test_tv_beats_fm_at_equal_eirp_and_distance(self):
        """What the old hard band tier was standing in for: 6 MHz of bandwidth
        over half a second is ~18 dB of processing gain an FM carrier cannot
        match. It is a margin now, not a rule no power can overcome."""
        fm = _area(20.0, band="FM", freq_mhz=98.0, height_m=300.0)
        vhf = _area(20.0, band="VHF", freq_mhz=195.0, height_m=300.0)
        uhf = _area(20.0, band="UHF", freq_mhz=550.0, height_m=300.0)
        assert fm < vhf
        assert fm < uhf

    def test_a_positive_band_offset_increases_area(self):
        plain = _area(30.0, eirp_dbm=70.0)
        boosted = _area(
            30.0,
            eirp_dbm=70.0,
            params=replace(_PARAMS, band_offset_db={"UHF": 10.0, "VHF": 0.0, "FM": 0.0}),
        )
        assert boosted > plain

    def test_the_offset_only_moves_its_own_band(self):
        params = replace(_PARAMS, band_offset_db={"UHF": 10.0, "VHF": 0.0, "FM": 0.0})
        assert _area(30.0, band="VHF", freq_mhz=195.0, params=params) == _area(30.0, band="VHF", freq_mhz=195.0)


# ── score_towers: the fields, and what it does with bad input ────────────────


class TestScoreTowers:
    def test_stamps_all_three_fields_on_every_tower(self):
        towers = [_tower(lat=_north_of_rx(20.0)), _tower(callsign="KYYY", lat=_north_of_rx(40.0))]
        score_towers(towers, _RX_LAT, _RX_LON, _PARAMS)
        for t in towers:
            assert t["expected_area_km2"] > 0
            assert 0 <= t["best_azimuth_deg"] < 360
            assert t["horizon_km"] > 0

    def test_values_are_plain_floats(self):
        """numpy scalars would sail through every assertion here and then fail
        json.dumps in the route, which is a 500 on the towers endpoint."""
        towers = [_tower(lat=_north_of_rx(20.0))]
        score_towers(towers, _RX_LAT, _RX_LON, _PARAMS)
        for key in ("expected_area_km2", "best_azimuth_deg", "horizon_km"):
            assert type(towers[0][key]) is float, key

    def test_unknown_band_scores_zero_rather_than_raising(self):
        towers = [_tower(band="DAB", freq_mhz=220.0, lat=_north_of_rx(20.0))]
        score_towers(towers, _RX_LAT, _RX_LON, _PARAMS)
        assert towers[0]["expected_area_km2"] == 0.0
        assert towers[0]["horizon_km"] > 0  # height is known, so this one still means something

    def test_missing_eirp_scores_zero(self):
        towers = [_tower(eirp_dbm=None, lat=_north_of_rx(20.0))]
        score_towers(towers, _RX_LAT, _RX_LON, _PARAMS)
        assert towers[0]["expected_area_km2"] == 0.0

    def test_empty_list_is_a_no_op(self):
        score_towers([], _RX_LAT, _RX_LON, _PARAMS)

    @pytest.mark.parametrize("height", [None, 0, -10.0, "300", True])
    def test_unusable_antenna_height_falls_back_to_the_default_mast(self, height):
        """antennaHeight arrives absent, null, 0 or as a string. None of those
        may produce a NaN, and none may score a real tower 0 for being 'short'."""
        towers = [_tower(antenna_height_m=height, lat=_north_of_rx(20.0))]
        score_towers(towers, _RX_LAT, _RX_LON, _PARAMS)
        t = towers[0]
        assert math.isfinite(t["expected_area_km2"])
        assert math.isfinite(t["horizon_km"])
        assert t["expected_area_km2"] > 0
        assert t["horizon_km"] == pytest.approx(round(radio_horizon_km(DEFAULT_ANTENNA_HEIGHT_M, 10.0), 1))

    def test_a_tower_at_the_receivers_exact_position_is_finite(self):
        """d = 0 makes FSPL -inf and the bistatic geometry degenerate. It
        happens: a node sited at the mast it is listening to."""
        towers = [_tower(lat=_RX_LAT, lon=_RX_LON)]
        score_towers(towers, _RX_LAT, _RX_LON, _PARAMS)
        t = towers[0]
        assert math.isfinite(t["expected_area_km2"])
        assert math.isfinite(t["best_azimuth_deg"])
        assert t["expected_area_km2"] >= 0.0

    def test_no_nan_anywhere_over_a_scatter_of_towers(self):
        rng = random.Random(11)
        towers = []
        for i in range(60):
            band, freq = rng.choice([("FM", 98.0), ("VHF", 195.0), ("UHF", 550.0)])
            towers.append(
                _tower(
                    callsign=f"K{i:03d}",
                    band=band,
                    freq_mhz=freq,
                    eirp_dbm=rng.uniform(30.0, 95.0),
                    lat=_RX_LAT + rng.uniform(-0.7, 0.7),
                    lon=_RX_LON + rng.uniform(-0.7, 0.7),
                    antenna_height_m=rng.choice([None, 0, 15.0, 120.0, 600.0]),
                )
            )
        score_towers(towers, _RX_LAT, _RX_LON, _PARAMS)
        for t in towers:
            for key in ("expected_area_km2", "best_azimuth_deg", "horizon_km"):
                assert math.isfinite(t[key]), f"{t['callsign']} {key} = {t[key]}"

    def test_direct_power_override_replaces_the_modelled_dpi(self):
        """Phase 2 wiring: a measured direct path says what the DPI really is.
        A much stronger one than the model assumed must cost the tower area."""
        base = _tower(lat=_north_of_rx(40.0))
        swamped = _tower(lat=_north_of_rx(40.0), direct_power_dbm_override=10.0)
        towers = [base, swamped]
        score_towers(towers, _RX_LAT, _RX_LON, _PARAMS)
        assert swamped["expected_area_km2"] < base["expected_area_km2"]

    def test_two_hundred_towers_score_in_well_under_a_second(self):
        """This runs inside the request. The pure-Python reference takes ~13 s
        for this many towers, which is why the model is vectorised."""
        rng = random.Random(3)
        towers = [
            _tower(
                callsign=f"K{i:03d}",
                band=b,
                freq_mhz=f,
                eirp_dbm=rng.uniform(40.0, 95.0),
                lat=_RX_LAT + rng.uniform(-0.7, 0.7),
                lon=_RX_LON + rng.uniform(-0.7, 0.7),
                antenna_height_m=rng.uniform(30.0, 600.0),
            )
            for i in range(200)
            for b, f in [rng.choice([("FM", 98.0), ("VHF", 195.0), ("UHF", 550.0)])]
        ]
        started = time.perf_counter()
        score_towers(towers, _RX_LAT, _RX_LON, _PARAMS)
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"scoring 200 towers took {elapsed:.2f}s"


# ── Per aircraft class ───────────────────────────────────────────────────────

# RCS offsets in dB relative to a narrowbody, in the shape the calibration carries.
_CLASS_OFFSETS_DB = {
    "military_transport": -1.9,
    "widebody": 0.6,
    "regional_jet": -1.1,
    "narrowbody": 0.0,
    "turboprop": -2.6,
    "bizjet": -3.2,
    "light_GA": -3.6,
    "helicopter": -2.0,
}

# A UHF tower 25 km north-east of the receiver; its best boresight is not north.
_CLASS_TOWER = (20.0, 15.0, 75.0, 550.0, "UHF", 400.0, _PARAMS)


class TestClassDetectionAreas:
    def test_the_narrowbody_class_is_the_ranking_area_at_its_boresight(self):
        score, classes = class_detection_areas(*_CLASS_TOWER, _CLASS_OFFSETS_DB)
        assert score == expected_detection_area(*_CLASS_TOWER)
        assert score.expected_area_km2 == 8632.0
        assert classes["narrowbody"]["area_km2"] == 8632.0

    def test_a_larger_class_offset_never_detects_less(self):
        _, classes = class_detection_areas(*_CLASS_TOWER, _CLASS_OFFSETS_DB)
        assert list(classes) == list(_CLASS_OFFSETS_DB)
        assert all(f["area_km2_low"] is None and f["range_50pct_km_high"] is None for f in classes.values())

        areas = [classes[name]["area_km2"] for name in sorted(_CLASS_OFFSETS_DB, key=_CLASS_OFFSETS_DB.get)][::-1]
        assert areas == sorted(areas, reverse=True)
        assert areas[0] > areas[-1]

    def test_the_fleet_calibration_sets_each_class_area_range_and_band(self):
        """A tower on the receiver keeps it doable by hand: R_t = R_r, no horizon
        loss, one boresight on a flat pattern, nothing cut. 2 km cells to 6 km sit
        at 2, 2.8, 4, 4.5 (8 of them), 5.7 and 6 km (4 each): rings of 4, 8, 16.
        The 42 degree beam north holds one of each, at 2, 4 and 6 km, so those
        three set the range. The ranking scores the tower at a 50 dBm stand-in
        plus a 5 dB UHF offset; the classes take the 70 dBm the system constant
        is referenced to."""
        params = replace(
            _PARAMS,
            n_azimuths=1,
            yagi_front_to_back_db=0.0,
            max_bistatic_angle_deg=180.0,
            max_range_km=6.0,
            band_offset_db={"UHF": 5.0, "VHF": 0.0, "FM": 0.0},
        )
        fleet = FleetPrediction(
            system_constant_db=67.5, prediction_sigma_db=3.5, threshold_db=12.0, processing_gain_db=64.0, eirp_dbm=70.0
        )
        _, classes = class_detection_areas(0.0, 0.0, 50.0, 600.0, "UHF", 300.0, params, _CLASS_OFFSETS_DB, fleet)

        # The edge falls between the 4.5 and 5.7 km cells, so the 6 km one is missed.
        assert classes["narrowbody"] == {
            "area_km2": 80.0,
            "range_50pct_km": 4.0,
            "range_50pct_bound": "crossed",
            "area_km2_low": 32.0,
            "area_km2_high": 112.0,
            "range_50pct_km_low": 2.0,
            "range_50pct_km_high": 6.0,
        }
        # 3.6 dB smaller: the 2.8 km half of ring 2, so the 4 km cell is missed.
        assert classes["light_GA"] == {
            "area_km2": 32.0,
            "range_50pct_km": 2.0,
            "range_50pct_bound": "crossed",
            "area_km2_low": 16.0,
            "area_km2_high": 80.0,
            "range_50pct_km_low": 2.0,
            "range_50pct_km_high": 4.0,
        }
        for f in classes.values():
            assert f["area_km2_low"] <= f["area_km2"] <= f["area_km2_high"]
            assert f["range_50pct_km_low"] <= f["range_50pct_km"] <= f["range_50pct_km_high"]
            assert all(type(v) is float for k, v in f.items() if k != "range_50pct_bound")

    def test_the_range_is_walked_over_the_beam_alone(self):
        """A node measures its range from the aircraft in its view. Off the main
        lobe a cell needs up to 20 dB more, so over whole rings the range would
        end where the back lobe stops (12 km here), while the beam still sees
        out to 36 km. The areas count every cell. Tower on the receiver,
        boresight north, the shipped grid and Yagi."""
        params = replace(_PARAMS, n_azimuths=1)
        fleet = FleetPrediction(
            system_constant_db=85.0, prediction_sigma_db=3.0, threshold_db=6.0, processing_gain_db=61.8, eirp_dbm=80.0
        )
        _, classes = class_detection_areas(0.0, 0.0, 60.0, 550.0, "UHF", 300.0, params, {"narrowbody": 0.0}, fleet)
        assert classes["narrowbody"] == {
            "area_km2": 988.0,
            "range_50pct_km": 36.0,
            "range_50pct_bound": "crossed",
            "area_km2_low": 700.0,
            "area_km2_high": 1404.0,
            "range_50pct_km_low": 30.0,
            "range_50pct_km_high": 42.0,
        }

    def test_the_fleet_figures_do_not_charge_the_horizon_loss_twice(self):
        """retina measures S against a free-space link, so a node's own path to
        its tower, horizon included, is already inside it. This VHF tower sits
        60 km south on a 100 m mast, 13 km past its 47 km horizon. The default
        mode charges it 26.5 dB; charged again in the fleet mode it would wipe
        out every cell. Boresight north, away from the tower, the shipped grid."""
        params = replace(_PARAMS, n_azimuths=1)
        tower = (0.0, -60.0, 80.0, 195.0, "VHF", 100.0, params, {"narrowbody": 0.0})
        fleet = FleetPrediction(
            system_constant_db=80.0, prediction_sigma_db=3.0, threshold_db=5.0, processing_gain_db=60.0, eirp_dbm=80.0
        )
        default = class_detection_areas(*tower)[1]["narrowbody"]
        assert (default["area_km2"], default["range_50pct_km"]) == (1092.0, 42.0)
        calibrated = class_detection_areas(*tower, fleet)[1]["narrowbody"]
        assert (calibrated["area_km2"], calibrated["range_50pct_km"]) == (232.0, 22.0)
