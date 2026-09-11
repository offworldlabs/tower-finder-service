"""Tests for the fleet feedback store and the shrinkage it applies.

Every test points ``tower_feedback._DB_PATH`` at a tmp file, the same way the
config tests point ``tower_ranking._CONFIG_PATH`` at one: the module reads the
global on each operation, so nothing here can touch the real runtime overlay.
"""

import math
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from models.feedback import TowerOutcome
from services import tower_feedback

K = 5.0  # apply_feedback's default shrinkage constant, spelled out for the maths below

RX_LAT, RX_LON = 34.0500, -118.2500
TX_LAT, TX_LON = 34.2300, -118.0600
FC_HZ = 98_700_000.0


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(tower_feedback, "_DB_PATH", tmp_path / "feedback.db")
    return tmp_path / "feedback.db"


def row(**overrides) -> dict:
    """A calibration row, overridable per test."""
    base = {
        "node_id": "node-1",
        "rx_lat": RX_LAT,
        "rx_lon": RX_LON,
        "tx_lat": TX_LAT,
        "tx_lon": TX_LON,
        "fc_hz": FC_HZ,
        "callsign": "KABC",
        "source": "calibration",
        "outcome": "confirmed_track",
    }
    base.update(overrides)
    return base


def tower(**overrides) -> dict:
    base = {
        "latitude": TX_LAT,
        "longitude": TX_LON,
        "frequency_mhz": FC_HZ / 1e6,
        "expected_area_km2": 1000.0,
    }
    base.update(overrides)
    return base


# ── Storage ──────────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_stores_and_summarises(self, store):
        assert tower_feedback.record_many([row(), row(node_id="node-2", callsign="KXYZ")]) == 2

        summary = tower_feedback.summary(10)
        assert len(summary) == 1
        entry = summary[0]
        assert entry["rows"] == 2
        assert entry["nodes"] == 2
        assert entry["receiver_cells"] == 1
        assert entry["callsigns"] == ["KABC", "KXYZ"]
        assert entry["mean_multiplier"] == pytest.approx(1.5)
        assert entry["tx_lat"] == pytest.approx(TX_LAT, abs=1e-3)
        assert entry["last_observed_at"]

    def test_accepts_a_validated_model_dump_unchanged(self, store):
        """The route hands the store TowerOutcome.model_dump(); every field in
        the model must have a column, or ingest silently drops it."""
        model = TowerOutcome(
            node_id="node-1",
            rx_lat=RX_LAT,
            rx_lon=RX_LON,
            tx_lat=TX_LAT,
            tx_lon=TX_LON,
            fc_hz=FC_HZ,
            callsign="KABC",
            source="archive",
            outcome="observed",
            verified_range_p85_km=120.5,
            adsb_match_rate=0.75,
            snr_median_db=12.0,
            hours_observed=6.0,
        )
        assert tower_feedback.record(model.model_dump()) == 1

        conn = sqlite3.connect(store)
        conn.row_factory = sqlite3.Row
        stored = conn.execute("SELECT * FROM tower_outcomes").fetchone()
        conn.close()
        assert stored["verified_range_p85_km"] == pytest.approx(120.5)
        assert stored["adsb_match_rate"] == pytest.approx(0.75)
        assert stored["snr_median_db"] == pytest.approx(12.0)
        assert stored["hours_observed"] == pytest.approx(6.0)
        # Absent from the payload, so the server dated it rather than leaving
        # a row nothing can order.
        assert stored["observed_at"]
        assert stored["received_at"]

    def test_empty_batch_is_not_a_write(self, store):
        assert tower_feedback.record_many([]) == 0
        assert not store.exists()

    def test_summary_of_an_empty_store(self, store):
        assert tower_feedback.summary(10) == []

    def test_row_cap_drops_the_oldest(self, store, monkeypatch):
        monkeypatch.setattr(tower_feedback, "MAX_ROWS", 5)
        for i in range(12):
            tower_feedback.record(row(node_id=f"node-{i}"))

        conn = sqlite3.connect(store)
        count, first = conn.execute("SELECT COUNT(*), MIN(node_id) FROM tower_outcomes").fetchone()
        conn.close()
        assert count == 5
        assert first != "node-0"

    def test_import_creates_nothing(self, tmp_path):
        """`process_and_rank` imports this module on every start, including in
        tests that never post a row; an import that made a file would drop a
        database wherever the CWD happened to be."""
        repo_root = Path(__file__).resolve().parents[2]
        subprocess.run(
            [sys.executable, "-c", "import sys; sys.path[:0] = ['.', 'backend']; import services.tower_feedback"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{repo_root}:{repo_root / 'backend'}"},
            check=True,
        )
        assert list(tmp_path.iterdir()) == []


# ── Keys ─────────────────────────────────────────────────────────────────────


class TestKeys:
    def test_metres_apart_is_one_tower(self):
        """FCC and Maprad disagree about a site by metres, and the node rounds
        its tune frequency; all three must land in one bucket."""
        assert tower_feedback.tower_key(34.2300, -118.0600, 98_700_000) == tower_feedback.tower_key(
            34.23004, -118.06003, 98_712_000
        )

    def test_a_neighbouring_station_is_a_different_tower(self):
        assert tower_feedback.tower_key(34.23, -118.06, 98_700_000) != tower_feedback.tower_key(
            34.23, -118.06, 99_100_000
        )

    def test_negative_zero_does_not_fork_the_key(self):
        """round() gives -0.0 back for a small negative, which formats as
        '-0.000' and would key one tower two ways across the equator."""
        assert tower_feedback.tower_key(-0.0001, -0.0001, 1e8) == tower_feedback.tower_key(0.0001, 0.0001, 1e8)


# ── Shrinkage ────────────────────────────────────────────────────────────────


class TestApplyFeedback:
    def test_no_data_is_a_neutral_factor(self, store):
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert towers[0]["feedback_n"] == 0.0
        assert towers[0]["feedback_factor"] == 1.0
        assert towers[0]["expected_area_km2"] == 1000.0

    def test_one_confirmed_track_nudges_the_area(self, store):
        tower_feedback.record(row())
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)

        expected = math.exp((1 / (1 + K)) * math.log(1.5))
        assert towers[0]["feedback_n"] == 1.0
        assert towers[0]["feedback_factor"] == pytest.approx(expected)
        assert towers[0]["expected_area_km2"] == pytest.approx(1000.0 * expected)

    def test_one_failure_does_not_erase_the_tower(self, store):
        """Shrinkage is the whole point: 0.1 as a raw multiplier would bury a
        tower on one node's bad afternoon."""
        tower_feedback.record(row(outcome="no_confirmed_track"))
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert 0.6 < towers[0]["feedback_factor"] < 1.0

    def test_many_rows_converge_on_the_multiplier(self, store):
        tower_feedback.record_many([row(node_id=f"node-{i}") for i in range(100)])
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)

        assert towers[0]["feedback_n"] == 100.0
        assert towers[0]["feedback_factor"] == pytest.approx(math.exp((100 / 105) * math.log(1.5)))
        assert towers[0]["feedback_factor"] > 1.45

    def test_a_distant_receiver_is_not_evidence(self, store):
        """Past ~30 km the terrain and the tower's own pattern differ enough
        that the row says nothing about what this node will see."""
        tower_feedback.record(row(node_id="far", rx_lat=RX_LAT + 1.0))
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert towers[0]["feedback_n"] == 0.0
        assert towers[0]["feedback_factor"] == 1.0

    def test_a_receiver_inside_the_radius_counts(self, store):
        tower_feedback.record(row(node_id="near", rx_lat=RX_LAT + 0.1))  # ~11 km
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert towers[0]["feedback_n"] == 1.0

    def test_outcomes_without_a_verdict_are_ignored(self, store):
        """'skipped_no_time' describes the run, not the tower."""
        tower_feedback.record_many([row(outcome=o) for o in ("skipped_no_time", "not_reached", "tuned")])
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert towers[0]["feedback_n"] == 0.0
        assert towers[0]["feedback_factor"] == 1.0

    def test_archive_rows_weigh_by_hours(self, store):
        tower_feedback.record(row(source="archive", outcome="observed", adsb_match_rate=1.0, hours_observed=10.0))
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)

        # rate 1.0 / pivot 0.5 = 2.0, weighted 10.
        assert towers[0]["feedback_n"] == 10.0
        assert towers[0]["feedback_factor"] == pytest.approx(math.exp((10 / 15) * math.log(2.0)))

    def test_archive_hours_are_capped(self, store):
        tower_feedback.record(row(source="archive", outcome="observed", adsb_match_rate=1.0, hours_observed=1000.0))
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert towers[0]["feedback_n"] == tower_feedback.ARCHIVE_HOURS_CAP

    def test_archive_multiplier_is_clamped(self, store):
        tower_feedback.record(row(source="archive", outcome="observed", adsb_match_rate=0.0))
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert towers[0]["feedback_factor"] > 0.0
        assert towers[0]["feedback_factor"] < 1.0

    def test_an_archive_row_without_a_match_rate_says_nothing_yet(self, store):
        """Provisional: the residual reads the match rate, so a row carrying
        only a verified range is stored and waits for the real model."""
        tower_feedback.record(row(source="archive", outcome="observed", verified_range_p85_km=150.0))
        towers = [tower()]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert towers[0]["feedback_n"] == 0.0

    def test_a_tower_without_a_model_area_still_gets_the_fields(self, store):
        tower_feedback.record(row())
        towers = [tower()]
        del towers[0]["expected_area_km2"]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert towers[0]["feedback_factor"] > 1.0
        assert "expected_area_km2" not in towers[0]

    def test_only_the_matching_tower_is_corrected(self, store):
        tower_feedback.record(row())
        other = tower(latitude=35.5, longitude=-119.9, frequency_mhz=101.1)
        towers = [tower(), other]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert towers[0]["feedback_factor"] > 1.0
        assert towers[1]["feedback_factor"] == 1.0
        assert towers[1]["expected_area_km2"] == 1000.0

    def test_an_unplaceable_tower_is_left_alone(self, store):
        towers = [{"latitude": None, "longitude": None, "frequency_mhz": None}]
        tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)
        assert "feedback_factor" not in towers[0]

    def test_a_broken_store_costs_the_correction_not_the_towers(self, store, monkeypatch, caplog):
        """Feedback is a correction. A corrupt database must not take the tower
        list down with it, and must say so in the log."""

        def boom():
            raise sqlite3.DatabaseError("file is not a database")

        monkeypatch.setattr(tower_feedback, "_connect", boom)
        towers = [tower()]
        with caplog.at_level("WARNING"):
            tower_feedback.apply_feedback(towers, RX_LAT, RX_LON)

        assert towers == [tower()]
        assert "Feedback lookup failed" in caplog.text

    def test_no_towers_is_not_a_query(self, store):
        tower_feedback.apply_feedback([], RX_LAT, RX_LON)
        assert not store.exists()
