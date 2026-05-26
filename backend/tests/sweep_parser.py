"""
sweep_parser.py — convert retina-spectrum sweep-events NDJSON into
Measurement-compatible dicts for the tower-finder service.

The NDJSON format is a sequence of SSE events:
  data: {"type":"start", ...}
  data: {"type":"step",  "step":N, "fc_mhz":X, "channels":[...], ...}
  data: {"type":"complete"}

Channels embedded in each step have two shapes:

  FM:
    {"band":"fm", "fc_mhz":89.3, "snr_db":55.2, "obw_fraction":0.45, "score":1.0}

  TV (UHF / VHF-Hi):
    {"band":"uhf"|"vhf_hi", "fc_mhz":545, "pilot_mhz":542.31,
     "channel_power_db":-31.0, "score":0.984, "peaks":[...]}

The retina-spectrum ring buffer accumulates 5 passes before gating,
so a single NDJSON file may contain more than one complete sweep.
Deduplication keeps the highest-scored observation per (freq_mhz, band).
"""

from __future__ import annotations

import json
from pathlib import Path

# Map retina-spectrum band labels → tower-finder band labels
_BAND_MAP: dict[str, str] = {
    "fm": "FM",
    "uhf": "UHF",
    "vhf_hi": "VHF",
}

# Fixtures directory (same folder as this module)
FIXTURES_DIR = Path(__file__).parent / "fixtures"
DEFAULT_NDJSON = FIXTURES_DIR / "sweep-events.ndjson"


def parse_sweep_events(
    path: str | Path = DEFAULT_NDJSON,
    *,
    min_score: float = 0.0,
) -> list[dict]:
    """Parse a retina-spectrum sweep-events NDJSON into Measurement dicts.

    Args:
        path: Path to the NDJSON file.
        min_score: Discard channels at or below this score (default 0.0 drops
                   hard-gated-out signals).

    Returns:
        List of dicts matching the ``Measurement`` Pydantic model fields:
        ``freq_mhz``, ``band``, ``snr_db``, ``obw_fraction``, ``score``,
        ``power_db``.  One entry per unique (freq_mhz, band) pair, keeping
        the highest-scored observation when the file contains multiple sweeps.
    """
    # best[(freq_mhz, band)] = measurement dict
    best: dict[tuple[float, str], dict] = {}

    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue

            if event.get("type") != "step":
                continue

            for ch in event.get("channels", []):
                raw_band = ch.get("band", "")
                band = _BAND_MAP.get(raw_band)
                if band is None:
                    continue  # unknown band — skip

                score = float(ch.get("score", 0.0))
                if score <= min_score:
                    continue  # gated out

                freq_mhz = float(ch["fc_mhz"])
                key = (freq_mhz, band)

                if band == "FM":
                    m = {
                        "freq_mhz": freq_mhz,
                        "band": "FM",
                        "snr_db": ch.get("snr_db"),
                        "obw_fraction": ch.get("obw_fraction"),
                        "score": score,
                        "power_db": None,  # FM pipeline doesn't expose raw power
                    }
                else:
                    # UHF / VHF — TV channel shape
                    m = {
                        "freq_mhz": freq_mhz,
                        "band": band,
                        "snr_db": None,
                        "obw_fraction": None,
                        "score": score,
                        "power_db": ch.get("channel_power_db"),
                    }

                # Keep the highest-scored observation for this frequency
                if key not in best or score > best[key]["score"]:
                    best[key] = m

    return list(best.values())


def summarise(measurements: list[dict]) -> str:
    """Return a human-readable summary of a parsed measurement list."""
    from collections import Counter

    band_counts = Counter(m["band"] for m in measurements)
    lines = [f"Total measurements: {len(measurements)}"]
    for band in ("FM", "VHF", "UHF"):
        count = band_counts.get(band, 0)
        if count:
            band_m = [m for m in measurements if m["band"] == band]
            top = max(band_m, key=lambda m: m["score"])
            lines.append(
                f"  {band}: {count} signals — "
                f"strongest {top['freq_mhz']} MHz (score={top['score']:.3f})"
            )
    return "\n".join(lines)
