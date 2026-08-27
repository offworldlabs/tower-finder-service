"""Tests for coverage-area-added scoring (services/tower_coverage.py) and its
wiring into process_and_rank's sort order.

Ported from the monolith's tests of the same name. The fleet geometries are
built from this service's own NodeGeometry (the monolith's comes from
retina_analytics, which is not a dependency here) and the config-restoring
fixture replaces the monolith's autouse conftest one.
"""

import json

import pytest
from services import tower_ranking
from services.tower_coverage import NodeGeometry, annotate_coverage_added
from services.tower_ranking import process_and_rank, reload_config

from tests._helpers import device as _device
from tests._helpers import system as _system

# Tiny grid per the spec: the test must run in milliseconds.
_MAX_RANGE_KM = 20.0
_GRID_STEP_KM = 5.0

# User RX at the equator/prime-meridian — cos(lat) == 1, so lat/lon degrees
# scale identically and the geometry below is easy to reason about in bearings.
_RX_LAT = 0.0
_RX_LON = 0.0


@pytest.fixture()
def restore_config():
    """Put every module-level setting back after a test that re-applies one.

    This service has no autouse equivalent, so a test that reloads config would
    otherwise leave its own ranking in place for whatever runs next.
    """
    saved = {name: getattr(tower_ranking, name) for name in tower_ranking.CONFIG_SETTINGS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(tower_ranking, name, value)


def _geo(node_id, beam_azimuth_deg, beam_width_deg, max_range_km=_MAX_RANGE_KM):
    """A fleet node sited at the user RX, aimed as given."""
    return NodeGeometry(
        node_id=node_id,
        rx_lat=_RX_LAT,
        rx_lon=_RX_LON,
        beam_azimuth_deg=beam_azimuth_deg,
        beam_width_deg=beam_width_deg,
        max_range_km=max_range_km,
    )


def _towers(n=2):
    return [{"callsign": f"T{i}"} for i in range(n)]


class TestNoOp:
    def test_empty_towers_no_op(self):
        towers = []
        geo = _geo("a", 0.0, 80.0)
        annotate_coverage_added(
            towers, _RX_LAT, _RX_LON, {"a": geo}, grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM
        )
        assert towers == []

    def test_empty_geometries_no_op(self):
        towers = _towers()
        annotate_coverage_added(towers, _RX_LAT, _RX_LON, {}, grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM)
        for t in towers:
            assert "coverage_area_added_km2" not in t
            assert "coverage_area_n3_km2" not in t
            assert "coverage_best_azimuth_deg" not in t


class TestNoCoverageAnywhere:
    def test_fleet_entirely_out_of_reach_scores_zero(self):
        """A fleet node nowhere near the candidate disk covers none of its
        cells, so every cell is existing==0 and no azimuth can add n>=2 area."""
        towers = _towers()
        far_geo = _geo("far", 0.0, 80.0)
        far_geo.rx_lat, far_geo.rx_lon = 80.0, 80.0  # nowhere near the candidate disk
        annotate_coverage_added(
            towers, _RX_LAT, _RX_LON, {"far": far_geo}, grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM
        )
        for t in towers:
            assert t["coverage_area_added_km2"] == 0.0
            assert t["coverage_area_n3_km2"] == 0.0
            assert isinstance(t["coverage_best_azimuth_deg"], float)


class TestThreeRegionGeometry:
    """Two fleet nodes carve the candidate disk into three bands by bearing
    from the user RX: [-20, 80) single-covered (node A only), [80, 140]
    double-covered (both A and B), and the remaining bearings uncovered."""

    def setup_method(self):
        self.node_a = _geo("a", beam_azimuth_deg=60.0, beam_width_deg=160.0)  # [-20, 140]
        self.node_b = _geo("b", beam_azimuth_deg=120.0, beam_width_deg=80.0)  # [80, 160]
        self.geometries = {"a": self.node_a, "b": self.node_b}

    def test_area_added_positive(self):
        towers = _towers()
        annotate_coverage_added(
            towers, _RX_LAT, _RX_LON, self.geometries, grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM
        )
        assert towers[0]["coverage_area_added_km2"] > 0

    def test_best_azimuth_points_into_single_covered_band(self):
        # Single-covered band spans bearings [-20, 80); its midpoint is 30.
        towers = _towers()
        annotate_coverage_added(
            towers, _RX_LAT, _RX_LON, self.geometries, grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM
        )
        az = towers[0]["coverage_best_azimuth_deg"]
        circular_diff = abs((az - 30.0 + 180) % 360 - 180)
        assert circular_diff <= 45.0

    def test_all_towers_get_identical_values(self):
        towers = _towers(3)
        annotate_coverage_added(
            towers, _RX_LAT, _RX_LON, self.geometries, grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM
        )
        added = {t["coverage_area_added_km2"] for t in towers}
        n3 = {t["coverage_area_n3_km2"] for t in towers}
        az = {t["coverage_best_azimuth_deg"] for t in towers}
        assert len(added) == 1
        assert len(n3) == 1
        assert len(az) == 1


class TestConcentricGeometryUpgradesN3:
    """One fleet node's whole footprint is a narrow wedge; a second node
    double-covers the middle of it. A candidate beam sized to the wedge can't
    avoid the doubly-covered core, so the winning azimuth carries n3 area too."""

    def test_n3_area_positive_when_unavoidable(self):
        outer = _geo("outer", beam_azimuth_deg=0.0, beam_width_deg=42.0)  # [-21, 21], n>=1
        inner = _geo("inner", beam_azimuth_deg=0.0, beam_width_deg=20.0)  # [-10, 10], n>=2 there
        towers = _towers()
        annotate_coverage_added(
            towers,
            _RX_LAT,
            _RX_LON,
            {"outer": outer, "inner": inner},
            grid_step_km=_GRID_STEP_KM,
            max_range_km=_MAX_RANGE_KM,
        )
        assert towers[0]["coverage_area_added_km2"] > 0
        assert towers[0]["coverage_area_n3_km2"] > 0


# ── process_and_rank integration ─────────────────────────────────────────────

_USER_LAT = 33.749
_USER_LON = -84.388


def _raw(callsign, eirp_watts):
    # Same location for both towers → identical distance_km/fspl, so
    # received_power_dbm differs only by EIRP.
    return _device(95.5, 33.85, -84.388, callsign=callsign, eirp=eirp_watts)


class TestProcessAndRankCoverageIntegration:
    def test_coverage_scorer_dominates_default_sort_order(self, tmp_path, monkeypatch, restore_config):
        """Two co-located towers (same distance/band, so only EIRP drives
        received_power_dbm) — the higher-EIRP one sorts first under the
        pre-existing rules. A stub coverage_scorer assigns coverage inversely
        to that old order; with coverage_area_added_km2 prepended
        (descending) to sort_order, the weak tower must now sort first."""
        cfg = {
            "receiver": {"rx_antenna_gain_dbi": 6.0, "sensitivity_dbm": -120.0},
            "broadcast_bands": {"FM": [[87.8, 108.0]]},
            "ranking": {
                "band_priority": {"FM": 0},
                "distance_classes": [{"label": "Ideal", "min_km": 0, "max_km": None}],
                "distance_priority": {"Ideal": 0},
                "sort_order": [
                    {"field": "coverage_area_added_km2", "ascending": False},
                    {"field": "band_priority", "ascending": True},
                    {"field": "distance_priority", "ascending": True},
                    {"field": "received_power_dbm", "ascending": False},
                ],
            },
            "search": {"default_radius_km": 80, "default_limit": 20},
        }
        fake_path = tmp_path / "tower_config.json"
        fake_path.write_text(json.dumps(cfg))
        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", fake_path)
        reload_config()

        raw = [_system([_raw("KSTRONG", 10000.0), _raw("KWEAK", 10.0)])]

        def stub_scorer(towers):
            for t in towers:
                t["coverage_area_added_km2"] = 500.0 if t["callsign"] == "KWEAK" else 10.0

        # Sanity check: without the scorer, the strong tower wins on
        # received_power_dbm (both coverage values are then absent → 0).
        baseline = process_and_rank(raw, _USER_LAT, _USER_LON)
        assert baseline[0]["callsign"] == "KSTRONG"

        result = process_and_rank(raw, _USER_LAT, _USER_LON, coverage_scorer=stub_scorer)
        assert result[0]["callsign"] == "KWEAK", (
            "coverage_area_added_km2 is prepended to sort_order, so the "
            "stub-scored weak tower must now outrank the strong one"
        )

    def test_coverage_scorer_exception_does_not_break_ranking(self):
        """A coverage_scorer that raises must not prevent towers from being
        returned — scoring is best-effort, wrapped in try/except."""
        raw = [_system([_device(95.5, 33.93, -84.388, callsign="WXYZ", eirp=60.0)])]

        def broken_scorer(towers):
            raise RuntimeError("boom")

        result = process_and_rank(raw, _USER_LAT, _USER_LON, coverage_scorer=broken_scorer)
        assert len(result) == 1
        assert result[0]["callsign"] == "WXYZ"

    def test_real_scorer_annotates_towers_end_to_end(self):
        """The ported module and the engine hook fit together unmodified."""
        raw = [_system([_device(95.5, 33.93, -84.388, callsign="WXYZ", eirp=60.0)])]
        geometries = {"a": NodeGeometry("a", _USER_LAT, _USER_LON, beam_azimuth_deg=90.0, beam_width_deg=160.0)}

        result = process_and_rank(
            raw,
            _USER_LAT,
            _USER_LON,
            coverage_scorer=lambda ts: annotate_coverage_added(
                ts, _USER_LAT, _USER_LON, geometries, grid_step_km=_GRID_STEP_KM, max_range_km=_MAX_RANGE_KM
            ),
        )

        assert result[0]["coverage_area_added_km2"] > 0
        assert isinstance(result[0]["coverage_best_azimuth_deg"], float)

    def test_measurement_ranking_still_works_with_a_scorer(self):
        """The superset claim: analyser `score` ranking is unaffected by the
        coverage hook when no coverage field is named in sort_order."""
        raw = [_system([_raw("KSTRONG", 10000.0), _raw("KWEAK", 10.0)])]
        measurements = [
            {"freq_mhz": 95.5, "snr_db": 20.0, "score": 0.9, "power_db": -60.0, "obw_fraction": 0.02},
        ]

        result = process_and_rank(
            raw,
            _USER_LAT,
            _USER_LON,
            measurements=measurements,
            coverage_scorer=lambda ts: [t.update(coverage_area_added_km2=1.0) for t in ts],
        )

        assert result, "measured towers must survive"
        for t in result:
            assert t["measured"] is True
            assert t["score"] == pytest.approx(0.9)
