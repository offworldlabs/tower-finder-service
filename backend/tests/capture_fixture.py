"""
capture_fixture.py — one-shot script to hit the FCC API and save a tower
fixture for the replay-matching integration tests.

Run from the project root:
    python backend/tests/capture_fixture.py

Saves:
    backend/tests/fixtures/towers_raw.json     — raw FCC system dicts
    backend/tests/fixtures/towers_matched.json — process_and_rank() output
                                                  with parsed measurements

The raw fixture is used by test_replay_matching.py::Layer3 so the tests
never hit the network again.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow running as a script from the project root
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "backend"))

from clients.fcc import fetch_fcc_broadcast_systems
from services.tower_ranking import process_and_rank
from tests.sweep_parser import DEFAULT_NDJSON, parse_sweep_events, summarise

# ── Recording location ────────────────────────────────────────────────────────
RECORDING_LAT = 34.8526
RECORDING_LON = -82.3940
RADIUS_KM = 80

FIXTURES_DIR = Path(__file__).parent / "fixtures"


async def _capture() -> None:
    print(f"Fetching FCC towers near ({RECORDING_LAT}, {RECORDING_LON}), radius={RADIUS_KM} km …")
    raw_systems = await fetch_fcc_broadcast_systems(
        RECORDING_LAT, RECORDING_LON, radius_km=RADIUS_KM
    )
    print(f"  → {len(raw_systems)} raw systems returned from FCC")

    # Save raw fixture
    raw_path = FIXTURES_DIR / "towers_raw.json"
    with open(raw_path, "w") as f:
        json.dump(raw_systems, f, indent=2)
    print(f"  ✓ Saved {raw_path}")

    # Parse the recording
    measurements = parse_sweep_events(DEFAULT_NDJSON)
    print()
    print(summarise(measurements))
    print()

    # Run matching — without measurements (baseline)
    baseline = process_and_rank(raw_systems, RECORDING_LAT, RECORDING_LON, radius_km=RADIUS_KM)
    print(f"Towers in DB within {RADIUS_KM} km: {len(baseline)}")

    # Run matching — with measurements
    enriched = process_and_rank(
        raw_systems,
        RECORDING_LAT,
        RECORDING_LON,
        radius_km=RADIUS_KM,
        measurements=measurements,
    )

    matched   = [t for t in enriched if t["measured"]]
    unmatched_towers = [t for t in enriched if not t["measured"]]

    matched_path = FIXTURES_DIR / "towers_matched.json"
    with open(matched_path, "w") as f:
        json.dump(enriched, f, indent=2)
    print(f"  ✓ Saved {matched_path}")

    # ── Coverage report ───────────────────────────────────────────────────────
    from collections import Counter

    print()
    print("── Match coverage ───────────────────────────────────────────")
    print(f"  Signals detected in recording:   {len(measurements)}")
    print(f"  Towers in DB (≤{RADIUS_KM} km):        {len(baseline)}")
    print(f"  Matched towers:                  {len(matched)}")

    band_match  = Counter(t["band"] for t in matched)
    band_total  = Counter(t["band"] for t in enriched)
    for band in ("FM", "VHF", "UHF"):
        print(f"    {band:3s}: {band_match[band]:2d} / {band_total[band]:2d} towers matched")

    print()
    print("  All matched towers (ranked):")
    for t in matched:
        print(
            f"    #{t['rank']:2d} {t['callsign']:10s}  {t['frequency_mhz']:6.1f} MHz  "
            f"{t['band']:3s}  score={t['score']:.3f}  "
            f"snr={t['snr_db'] if t['snr_db'] is not None else 'N/A':>6}  "
            f"dist={t['distance_km']:5.1f} km  {t['bearing_cardinal']:3s}"
        )

    print()
    print("  Detected signals with NO matching DB tower (phantoms or out-of-area):")
    # Find measurements that didn't match anything
    matched_freqs = {
        (t["frequency_mhz"], t["band"])
        for t in matched
    }
    for m in sorted(measurements, key=lambda x: x["freq_mhz"]):
        from services.tower_ranking import MEASUREMENT_TOLERANCE_MHZ

        tol = MEASUREMENT_TOLERANCE_MHZ.get(m["band"], 1.0)
        hit = any(
            abs(t["frequency_mhz"] - m["freq_mhz"]) <= tol and t["band"] == m["band"]
            for t in matched
        )
        if not hit:
            print(
                f"    {m['band']:3s} {m['freq_mhz']:6.1f} MHz  score={m['score']:.3f}"
                + (f"  snr={m['snr_db']:.1f} dB" if m["snr_db"] is not None else "")
            )

    print("─────────────────────────────────────────────────────────────")
    print()
    print("Next step: review the list above, then run:")
    print("  pytest backend/tests/test_replay_matching.py -v -m integration")


def main() -> None:
    asyncio.run(_capture())


if __name__ == "__main__":
    main()
