"""
test_replay_matching.py — end-to-end validation of tower matching against a
real RF recording from a known location.

Recording: backend/tests/fixtures/sweep-events.ndjson
Location:  34.8526° N, 82.3940° W  (Greenville, SC area)

Validation layers
─────────────────
Layer 1 — Parser unit tests (this file, always run, no network needed)
  Verifies that sweep_parser.parse_sweep_events() correctly converts the
  NDJSON recording into Measurement-compatible dicts.

Layer 3 — Integration regression test (marked ``integration``)
  Loads a saved FCC tower fixture, calls process_and_rank() with the parsed
  measurements, and asserts that the expected towers are matched.

  Run once to generate the fixture:
      python backend/tests/capture_fixture.py

  Then run the full suite:
      pytest backend/tests/test_replay_matching.py -v
  Or only the fast layer:
      pytest backend/tests/test_replay_matching.py -v -m "not integration"
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.sweep_parser import DEFAULT_NDJSON, parse_sweep_events
from services.tower_ranking import (
    MEASUREMENT_TOLERANCE_MHZ,
    classify_band,
    process_and_rank,
)

# ── Recording metadata ────────────────────────────────────────────────────────

RECORDING_LAT = 34.8526
RECORDING_LON = -82.3940

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TOWER_FIXTURE = FIXTURES_DIR / "towers_raw.json"

# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — Parser unit tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSweepParser:
    """Validate that parse_sweep_events() produces well-formed Measurement dicts."""

    @pytest.fixture(scope="class")
    def measurements(self):
        return parse_sweep_events(DEFAULT_NDJSON)

    # ── Basic shape ───────────────────────────────────────────────────────────

    def test_returns_list(self, measurements):
        assert isinstance(measurements, list)

    def test_nonempty(self, measurements):
        assert len(measurements) > 0

    def test_expected_total_count(self, measurements):
        # 27 FM + 22 UHF (ch14-36, ch35 gated out) + 6 VHF pass the score > 0 gate
        assert len(measurements) == 55, f"Expected 55 measurements (27 FM, 22 UHF, 6 VHF), got {len(measurements)}"

    def test_all_required_fields_present(self, measurements):
        required = {"freq_mhz", "band", "snr_db", "obw_fraction", "score", "power_db"}
        for m in measurements:
            assert required.issubset(m.keys()), f"Missing fields in {m}"

    # ── Band normalisation ────────────────────────────────────────────────────

    def test_only_valid_bands(self, measurements):
        valid = {"FM", "UHF", "VHF"}
        for m in measurements:
            assert m["band"] in valid, f"Unknown band '{m['band']}' in {m}"

    def test_no_raw_band_labels(self, measurements):
        """'fm', 'uhf', 'vhf_hi' must be normalised to uppercase tower-finder labels."""
        raw_labels = {"fm", "uhf", "vhf_hi"}
        for m in measurements:
            assert m["band"] not in raw_labels

    def test_band_counts(self, measurements):
        from collections import Counter

        counts = Counter(m["band"] for m in measurements)
        assert counts["FM"] == 27, f"Expected 27 FM, got {counts['FM']}"
        assert counts["UHF"] == 22, f"Expected 22 UHF (ch14–36, ch35 gated out), got {counts['UHF']}"
        assert counts["VHF"] == 6, f"Expected 6 VHF, got {counts['VHF']}"

    # ── Score gate ────────────────────────────────────────────────────────────

    def test_no_zero_score_signals(self, measurements):
        """Gated-out channels (score ≤ 0) must be dropped."""
        for m in measurements:
            assert m["score"] > 0.0, f"Zero-score signal leaked through: {m}"

    def test_scores_in_unit_range(self, measurements):
        for m in measurements:
            assert 0.0 < m["score"] <= 1.0, f"Score out of range: {m}"

    # ── Deduplication ─────────────────────────────────────────────────────────

    def test_no_duplicate_freq_band(self, measurements):
        """Two sweeps in the file — each (freq_mhz, band) must appear once."""
        keys = [(m["freq_mhz"], m["band"]) for m in measurements]
        assert len(keys) == len(set(keys)), "Duplicate (freq_mhz, band) pairs found"

    def test_dedup_keeps_best_score(self):
        """When the NDJSON has two sweeps for the same channel, the higher-scored
        entry must win (ring buffer accumulates → later sweep is more reliable)."""
        # Parse a second time restricting to a single pass (by score) is hard to
        # test in isolation, but we can verify the known ch26 score matches the
        # better of its two sweep values in the recording.
        measurements = parse_sweep_events(DEFAULT_NDJSON)
        uhf_26 = next((m for m in measurements if m["freq_mhz"] == 545.0 and m["band"] == "UHF"), None)
        assert uhf_26 is not None, "UHF ch26 (545 MHz) missing from parsed output"
        # Both sweep instances of ch26 had score ≥ 0.984; dedup should keep ≥ 0.984
        assert uhf_26["score"] >= 0.984

    # ── Frequency range sanity ────────────────────────────────────────────────

    def test_fm_freqs_in_band(self, measurements):
        for m in [m for m in measurements if m["band"] == "FM"]:
            assert classify_band(m["freq_mhz"]) == "FM", f"FM measurement freq {m['freq_mhz']} MHz not in FM band"

    def test_uhf_freqs_in_band(self, measurements):
        for m in [m for m in measurements if m["band"] == "UHF"]:
            assert classify_band(m["freq_mhz"]) == "UHF", f"UHF measurement freq {m['freq_mhz']} MHz not in UHF band"

    def test_vhf_freqs_in_band(self, measurements):
        for m in [m for m in measurements if m["band"] == "VHF"]:
            assert classify_band(m["freq_mhz"]) == "VHF", f"VHF measurement freq {m['freq_mhz']} MHz not in VHF band"

    # ── Known strong signals ──────────────────────────────────────────────────

    def test_fm_89_3_present_and_top_scored(self, measurements):
        """89.3 MHz was the strongest FM signal (score=1.0) in this recording."""
        fm_89 = next((m for m in measurements if m["freq_mhz"] == 89.3 and m["band"] == "FM"), None)
        assert fm_89 is not None, "FM 89.3 MHz missing from parsed output"
        assert fm_89["score"] == pytest.approx(1.0)

    def test_fm_94_5_present(self, measurements):
        fm = next((m for m in measurements if m["freq_mhz"] == 94.5 and m["band"] == "FM"), None)
        assert fm is not None, "FM 94.5 MHz missing"

    def test_fm_104_9_present(self, measurements):
        fm = next((m for m in measurements if m["freq_mhz"] == 104.9 and m["band"] == "FM"), None)
        assert fm is not None, "FM 104.9 MHz missing"

    def test_vhf_ch8_183_mhz_top_scored(self, measurements):
        """VHF channel 8 (183 MHz) was the strongest TV signal in this recording (score=1.0)."""
        vhf8 = next((m for m in measurements if m["freq_mhz"] == 183.0 and m["band"] == "VHF"), None)
        assert vhf8 is not None, "VHF ch8 (183 MHz) missing"
        assert vhf8["score"] == pytest.approx(1.0)

    def test_vhf_ch11_201_mhz_present(self, measurements):
        vhf11 = next((m for m in measurements if m["freq_mhz"] == 201.0 and m["band"] == "VHF"), None)
        assert vhf11 is not None, "VHF ch11 (201 MHz) missing"
        assert vhf11["score"] > 0.9

    def test_uhf_ch26_545_mhz_strongest_uhf(self, measurements):
        """UHF ch26 (545 MHz) must be the strongest UHF signal in this recording."""
        uhf = [m for m in measurements if m["band"] == "UHF"]
        strongest = max(uhf, key=lambda m: m["score"])
        assert strongest["freq_mhz"] == 545.0, (
            f"Expected UHF ch26 (545 MHz) to be strongest, got {strongest['freq_mhz']} MHz"
        )
        assert strongest["score"] >= 0.984

    # ── Field types per band ──────────────────────────────────────────────────

    def test_fm_has_snr_and_obw(self, measurements):
        """FM measurements must carry snr_db and obw_fraction (from FM DSP pipeline)."""
        for m in [m for m in measurements if m["band"] == "FM"]:
            assert m["snr_db"] is not None, f"FM snr_db is None: {m}"
            assert m["obw_fraction"] is not None, f"FM obw_fraction is None: {m}"
            assert m["power_db"] is None, f"FM power_db should be None: {m}"

    def test_tv_has_power_not_snr(self, measurements):
        """TV measurements carry channel_power_db but not SNR (different DSP path)."""
        for m in [m for m in measurements if m["band"] in ("UHF", "VHF")]:
            assert m["power_db"] is not None, f"TV power_db is None: {m}"
            assert m["snr_db"] is None, f"TV snr_db should be None: {m}"
            assert m["obw_fraction"] is None, f"TV obw_fraction should be None: {m}"

    def test_fm_snr_values_are_positive(self, measurements):
        for m in [m for m in measurements if m["band"] == "FM"]:
            assert m["snr_db"] > 0, f"Unexpected non-positive SNR: {m}"

    def test_tv_power_values_are_negative_dbfs(self, measurements):
        """TV channel power is in dBFS — should be negative for real signals."""
        for m in [m for m in measurements if m["band"] in ("UHF", "VHF")]:
            assert m["power_db"] < 0, f"TV power_db should be negative dBFS: {m}"

    # ── min_score kwarg ───────────────────────────────────────────────────────

    def test_min_score_filter_reduces_count(self):
        all_m = parse_sweep_events(DEFAULT_NDJSON, min_score=0.0)
        high_m = parse_sweep_events(DEFAULT_NDJSON, min_score=0.5)
        assert len(high_m) < len(all_m)

    def test_min_score_all_above_threshold(self):
        threshold = 0.7
        filtered = parse_sweep_events(DEFAULT_NDJSON, min_score=threshold)
        for m in filtered:
            assert m["score"] > threshold


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — Integration regression test
# Needs: python backend/tests/capture_fixture.py  (run once, saves towers_raw.json)
# ─────────────────────────────────────────────────────────────────────────────


def _load_tower_fixture() -> list[dict] | None:
    """Return the saved FCC raw-systems fixture, or None if not yet captured."""
    if not TOWER_FIXTURE.exists():
        return None
    with open(TOWER_FIXTURE) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def ranked_with_measurements():
    """process_and_rank() result using the real recording + saved tower fixture."""
    raw_systems = _load_tower_fixture()
    if raw_systems is None:
        pytest.skip("Tower fixture not found. Run:  python backend/tests/capture_fixture.py")
    measurements = parse_sweep_events(DEFAULT_NDJSON)
    return process_and_rank(
        raw_systems,
        RECORDING_LAT,
        RECORDING_LON,
        measurements=measurements,
    )


@pytest.fixture(scope="module")
def ranked_without_measurements():
    """Baseline: same towers without any measurement enrichment."""
    raw_systems = _load_tower_fixture()
    if raw_systems is None:
        pytest.skip("Tower fixture not found. Run:  python backend/tests/capture_fixture.py")
    return process_and_rank(raw_systems, RECORDING_LAT, RECORDING_LON)


@pytest.mark.integration
class TestMatchRecall:
    """Every strong detected signal must match a real tower in the DB."""

    def test_fm_89_3_matched(self, ranked_with_measurements):
        """FM 89.3 MHz (score=1.0) must match at least one tower with measured=True."""
        tol = MEASUREMENT_TOLERANCE_MHZ["FM"]
        matched = [
            t
            for t in ranked_with_measurements
            if t["band"] == "FM" and abs(t["frequency_mhz"] - 89.3) <= tol and t["measured"]
        ]
        assert matched, "No tower matched FM 89.3 MHz — check FCC data or tolerance"

    def test_fm_94_5_matched(self, ranked_with_measurements):
        tol = MEASUREMENT_TOLERANCE_MHZ["FM"]
        matched = [
            t
            for t in ranked_with_measurements
            if t["band"] == "FM" and abs(t["frequency_mhz"] - 94.5) <= tol and t["measured"]
        ]
        assert matched, "No tower matched FM 94.5 MHz"

    def test_fm_104_9_matched(self, ranked_with_measurements):
        tol = MEASUREMENT_TOLERANCE_MHZ["FM"]
        matched = [
            t
            for t in ranked_with_measurements
            if t["band"] == "FM" and abs(t["frequency_mhz"] - 104.9) <= tol and t["measured"]
        ]
        assert matched, "No tower matched FM 104.9 MHz"

    def test_vhf_ch8_183_matched(self, ranked_with_measurements):
        tol = MEASUREMENT_TOLERANCE_MHZ["VHF"]
        matched = [
            t
            for t in ranked_with_measurements
            if t["band"] == "VHF" and abs(t["frequency_mhz"] - 183.0) <= tol and t["measured"]
        ]
        assert matched, "No tower matched VHF ch8 (183 MHz)"

    def test_uhf_ch26_545_matched(self, ranked_with_measurements):
        tol = MEASUREMENT_TOLERANCE_MHZ["UHF"]
        matched = [
            t
            for t in ranked_with_measurements
            if t["band"] == "UHF" and abs(t["frequency_mhz"] - 545.0) <= tol and t["measured"]
        ]
        assert matched, "No tower matched UHF ch26 (545 MHz)"


@pytest.mark.integration
class TestMeasurementFieldPropagation:
    """Matched towers must carry the right analyser quality fields."""

    def _matched(self, ranked):
        return [t for t in ranked if t["measured"]]

    def test_matched_towers_have_score(self, ranked_with_measurements):
        for t in self._matched(ranked_with_measurements):
            assert t["score"] is not None, f"Missing score on matched tower {t['callsign']}"
            assert 0 < t["score"] <= 1.0

    def test_fm_matched_towers_have_snr(self, ranked_with_measurements):
        fm_matched = [t for t in self._matched(ranked_with_measurements) if t["band"] == "FM"]
        for t in fm_matched:
            assert t["snr_db"] is not None, f"FM tower {t['callsign']} missing snr_db"
            assert t["snr_db"] > 0

    def test_tv_matched_towers_have_power(self, ranked_with_measurements):
        tv_matched = [t for t in self._matched(ranked_with_measurements) if t["band"] in ("UHF", "VHF")]
        for t in tv_matched:
            assert t["power_db"] is not None, f"TV tower {t['callsign']} missing power_db"

    def test_only_matched_towers_in_output(self, ranked_with_measurements):
        """When measurements are provided, every tower in the output must be matched.
        Unmatched towers are invisible to the SDR and must be excluded entirely."""
        unmatched = [t for t in ranked_with_measurements if not t["measured"]]
        assert unmatched == [], f"{len(unmatched)} unmatched tower(s) leaked into results: " + ", ".join(
            f"{t['callsign']} @ {t['frequency_mhz']} MHz" for t in unmatched
        )

    def test_all_output_towers_have_analyser_fields(self, ranked_with_measurements):
        """Every tower in the output must carry real analyser quality data."""
        for t in ranked_with_measurements:
            assert t["measured"] is True
            assert t["frequency_matched"] is True
            assert t["score"] is not None and t["score"] > 0


@pytest.mark.integration
class TestMatchCoverage:
    """Summary-level checks — these print diagnostics, not just pass/fail."""

    def test_match_recall_above_floor(self, ranked_with_measurements):
        """At least 80% of the 47 detected signals should match a DB tower.

        Failures here mean the FCC data is missing entries, the frequency
        tolerance is too tight, or the recording location is wrong.
        """
        measurements = parse_sweep_events(DEFAULT_NDJSON)
        matched_count = sum(1 for t in ranked_with_measurements if t["measured"])
        total_detected = len(measurements)
        recall = matched_count / total_detected if total_detected else 0
        assert recall >= 0.80, (
            f"Match recall {recall:.0%} is below 80% ({matched_count}/{total_detected} signals matched a DB tower)"
        )

    def test_each_output_tower_has_a_measurement_within_tolerance(self, ranked_with_measurements):
        """Every tower in the output must have a real measurement within the
        band-specific frequency tolerance — i.e. the match is genuine, not
        a coincidence of tolerances producing a false positive."""
        measurements = parse_sweep_events(DEFAULT_NDJSON)
        for t in ranked_with_measurements:
            tol = MEASUREMENT_TOLERANCE_MHZ.get(t["band"], 1.0)
            supporting = [
                m for m in measurements if m["band"] == t["band"] and abs(m["freq_mhz"] - t["frequency_mhz"]) <= tol
            ]
            assert supporting, (
                f"Tower {t['callsign']} @ {t['frequency_mhz']} MHz ({t['band']}) "
                "is in the output but no measurement is within tolerance — "
                "this is a phantom match"
            )

    def test_match_summary(self, ranked_with_measurements, ranked_without_measurements, capsys):
        """Print a coverage table — always passes, used for eyeball review."""
        measurements = parse_sweep_events(DEFAULT_NDJSON)

        from collections import Counter

        band_counts = Counter(t["band"] for t in ranked_with_measurements)

        print("\n── Match coverage report ────────────────────────────────")
        print(f"  Signals in recording:       {len(measurements)}")
        print(f"  Towers in DB (≤80km):       {len(ranked_without_measurements)}")
        print(f"  Towers visible to SDR:      {len(ranked_with_measurements)}")
        for band in ("FM", "VHF", "UHF"):
            print(f"    {band}: {band_counts[band]}")
        print()
        print("  Ranked towers (SDR-visible only):")
        for t in ranked_with_measurements[:10]:
            print(
                f"    #{t['rank']:2d} {t['callsign']:10s} {t['frequency_mhz']:6.1f} MHz "
                f"{t['band']:3s}  score={t['score']:.3f}  dist={t['distance_km']} km  "
                f"{t['bearing_cardinal']}"
            )
        print()
        # Signals that didn't match any DB tower
        tols = MEASUREMENT_TOLERANCE_MHZ
        unmatched_signals = [
            m
            for m in measurements
            if not any(
                m["band"] == t["band"] and abs(m["freq_mhz"] - t["frequency_mhz"]) <= tols.get(m["band"], 1.0)
                for t in ranked_with_measurements
            )
        ]
        if unmatched_signals:
            print(f"  Detected signals with no DB match ({len(unmatched_signals)}):")
            for m in sorted(unmatched_signals, key=lambda x: x["freq_mhz"]):
                print(f"    {m['band']:3s} {m['freq_mhz']:6.1f} MHz  score={m['score']:.3f}")
        print("────────────────────────────────────────────────────────")
