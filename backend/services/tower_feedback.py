"""Fleet feedback store: what the nodes actually got out of a tower we ranked.

The ranking models how much area a tower is expected to light up
(``expected_area_km2``). Nothing has ever corrected that number against what a
node saw when it tuned there. This module closes that loop: nodes and the
archive job post outcomes, and `apply_feedback` folds them back into the model
area at request time as a multiplicative correction.

Storage is a SQLite file in the same runtime overlay as tower_config.json, so
it survives a redeploy and is override-able with TOWER_FINDER_RUNTIME_DIR. No
schema, directory or connection is created at import: `process_and_rank` imports
this module on every start, including in tests that never post a row, and an
import that made a file would put a database wherever the CWD happened to be.

The correction (from the design note):

    factor = exp( n / (n + k) * r̄ )

``r̄`` is the weight-weighted mean log-multiplier over the rows for that tower
seen from nearby receivers, and ``n`` is their total weight. The n/(n+k)
shrinkage is the whole point: one node saying "nothing there" must nudge the
area, not erase the tower, while a hundred rows saying it converge on what they
say. k = 5 means a single confirmed track moves the area about 7%.
"""

import logging
import math
import os
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Same overlay as tower_ranking._CONFIG_PATH, and read the same way: a test
# points the module attribute at a tmp path, so every access below goes through
# the module global rather than a value bound at import.
_RUNTIME_DIR = Path(os.environ.get("TOWER_FINDER_RUNTIME_DIR", "data/runtime"))
_DB_PATH = _RUNTIME_DIR / "feedback.db"

# Bounded growth. The volume is small and nothing prunes it on a schedule, so
# the table keeps its newest MAX_ROWS and drops the rest on insert. At fleet
# scale (a few rows per node per calibration) this is years of history.
MAX_ROWS = 200_000

# Receivers this far from the requesting node count as evidence about the same
# tower. Beyond it terrain and the tower's own pattern differ enough that the
# row says little about what this node will see.
FEEDBACK_RADIUS_KM = 30.0

# Calibration verdict -> area multiplier. A confirmed track says the tower lights
# up more than modelled; no track at the same site says much less; an overload
# says the model is badly wrong about it. Outcomes absent here (tuned,
# tuning_not_applied, skipped_no_time, not_reached) are stored but carry no
# residual: they describe the run, not the tower.
OUTCOME_MULTIPLIERS: dict[str, float] = {
    "confirmed_track": 1.5,
    "no_confirmed_track": 0.1,
    "unstable_overload": 0.02,
}

# PROVISIONAL. The design note's archive residual is
#     r = log( (pi * verified_range_p85_km**2) / expected_area_km2_at_request_time )
# but the model area is not known at ingest (it depends on the config in force
# when the request was served), and recomputing it here would fork the ranking.
# So archive rows store the raw fields and stand in the ADS-B match rate as the
# proxy: half the tracks matched is "as modelled", all matched is twice the area.
# Replace this with the real residual once the request-time area is carried on
# the row or recomputed from a stored config version.
ARCHIVE_MATCH_RATE_PIVOT = 0.5
ARCHIVE_MULTIPLIER_MIN = 0.05
ARCHIVE_MULTIPLIER_MAX = 3.0
# An archive window longer than a day is not proportionally more evidence about
# a tower: propagation cycles daily, so the extra hours are correlated.
ARCHIVE_HOURS_CAP = 24.0

EARTH_RADIUS_KM = 6371.0

_COLUMNS = (
    "node_id",
    "rx_lat",
    "rx_lon",
    "tx_lat",
    "tx_lon",
    "fc_hz",
    "callsign",
    "source",
    "outcome",
    "max_evidence",
    "max_detections",
    "duration_s",
    "gain_a",
    "gain_b",
    "lna_state",
    "verified_range_p85_km",
    "adsb_match_rate",
    "snr_median_db",
    "hours_observed",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tower_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    node_id TEXT NOT NULL,
    rx_lat REAL NOT NULL,
    rx_lon REAL NOT NULL,
    tx_lat REAL NOT NULL,
    tx_lon REAL NOT NULL,
    fc_hz REAL NOT NULL,
    callsign TEXT,
    source TEXT NOT NULL,
    outcome TEXT NOT NULL,
    max_evidence INTEGER,
    max_detections INTEGER,
    duration_s REAL,
    gain_a INTEGER,
    gain_b INTEGER,
    lna_state INTEGER,
    verified_range_p85_km REAL,
    adsb_match_rate REAL,
    snr_median_db REAL,
    hours_observed REAL,
    tower_key TEXT NOT NULL,
    rx_cell TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tower_outcomes_rx ON tower_outcomes (rx_lat, rx_lon);
CREATE INDEX IF NOT EXISTS idx_tower_outcomes_key ON tower_outcomes (tower_key);
"""

# One writer at a time. FastAPI runs this service on a single worker, but route
# handlers hand the blocking DB work to the threadpool, so two requests really
# can be in here at once; SQLite would answer the second with "database is
# locked" rather than serialising it for us.
_LOCK = threading.Lock()


# ── Keys ─────────────────────────────────────────────────────────────────────


def _norm(value: float, digits: int) -> float:
    # round() gives back -0.0 for small negatives, which formats as "-0.000" and
    # would key the same tower two ways either side of the equator/meridian.
    rounded = round(value, digits)
    return 0.0 if rounded == 0 else rounded


def tower_key(tx_lat: float, tx_lon: float, fc_hz: float) -> str:
    """Group key for one transmitter: ~100 m of position, 100 kHz of frequency.

    Coarse enough that two records of the same site (FCC and Maprad disagree by
    metres) and a node's rounded tune frequency land in one bucket, fine enough
    that two stations on the same tower stay apart.
    """
    return f"{_norm(tx_lat, 3):.3f}|{_norm(tx_lon, 3):.3f}|{round(fc_hz / 1e5) * 100_000:.0f}"


def receiver_cell(rx_lat: float, rx_lon: float) -> str:
    """Group key for a receiver location: ~1 km. Only used for grouping.

    Radius tests use the stored coordinates, not this; the cell exists so a
    later per-region fit has something cheap to group on, and so the summary can
    say how many distinct sites are behind a tower's rows.
    """
    return f"{_norm(rx_lat, 2):.2f}|{_norm(rx_lon, 2):.2f}"


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km.

    Duplicated from tower_ranking rather than imported: tower_ranking calls
    `apply_feedback`, so importing it back here would be a cycle at import time,
    and it also reloads the ranking config as a side effect of being imported.
    """
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(a))


# ── Store ────────────────────────────────────────────────────────────────────


def _connect() -> sqlite3.Connection:
    """Open the store, creating the directory and schema if they are missing."""
    path = _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # IF NOT EXISTS throughout, so this is the first-use creation and a no-op
    # every time after. Cheaper than tracking whether we have seeded this path,
    # which a test's monkeypatch of _DB_PATH would invalidate anyway.
    conn.executescript(_SCHEMA)
    return conn


def _isoformat(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return datetime.now(timezone.utc).isoformat()


def record_many(rows: Iterable[dict]) -> int:
    """Insert outcome rows. Returns how many were stored.

    Rows arrive already validated (models.feedback.TowerOutcome), so this only
    fills observed_at/received_at and derives the group keys.
    """
    rows = list(rows)
    if not rows:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    params = []
    for row in rows:
        values = [row.get(name) for name in _COLUMNS]
        params.append(
            (
                now,
                _isoformat(row.get("observed_at")),
                *values,
                tower_key(row["tx_lat"], row["tx_lon"], row["fc_hz"]),
                receiver_cell(row["rx_lat"], row["rx_lon"]),
            )
        )

    placeholders = ", ".join("?" * (len(_COLUMNS) + 4))
    sql = (
        f"INSERT INTO tower_outcomes (received_at, observed_at, {', '.join(_COLUMNS)}, tower_key, rx_cell) "
        f"VALUES ({placeholders})"
    )
    with _LOCK:
        conn = _connect()
        try:
            with conn:
                conn.executemany(sql, params)
                _prune(conn)
        finally:
            conn.close()
    return len(rows)


def record(row: dict) -> int:
    """Insert one outcome row."""
    return record_many([row])


def _prune(conn: sqlite3.Connection) -> None:
    """Drop everything older than the newest MAX_ROWS rows."""
    # By id, not by timestamp: ids are monotonic per insert, so this is an index
    # seek and cannot be confused by a node whose clock is wrong.
    cutoff = conn.execute(
        "SELECT id FROM tower_outcomes ORDER BY id DESC LIMIT 1 OFFSET ?",
        (MAX_ROWS,),
    ).fetchone()
    if cutoff is not None:
        conn.execute("DELETE FROM tower_outcomes WHERE id <= ?", (cutoff[0],))


# ── Residuals ────────────────────────────────────────────────────────────────


def _residual(row) -> tuple[float, float] | None:
    """(log multiplier, weight) for one stored row, or None when it says nothing."""
    if row["source"] == "archive":
        rate = row["adsb_match_rate"]
        if rate is None:
            # See ARCHIVE_MATCH_RATE_PIVOT: the match rate is the only archive
            # field the provisional residual can read. A row with only a
            # verified range is kept for when the real residual lands.
            return None
        multiplier = min(ARCHIVE_MULTIPLIER_MAX, max(ARCHIVE_MULTIPLIER_MIN, rate / ARCHIVE_MATCH_RATE_PIVOT))
        hours = row["hours_observed"]
        weight = 1.0 if hours is None else min(ARCHIVE_HOURS_CAP, float(hours))
        if weight <= 0:
            return None
        return math.log(multiplier), weight

    multiplier = OUTCOME_MULTIPLIERS.get(row["outcome"])
    if multiplier is None:
        return None
    return math.log(multiplier), 1.0


def _bounding_box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """A lat/lon box that contains the radius. Widened, never narrowed."""
    dlat = radius_km / 110.574
    cos_lat = math.cos(math.radians(lat))
    # Near the poles (and across the antimeridian below) the box degenerates.
    # Scanning the whole longitude range is slower but still correct, because
    # the haversine filter is what actually decides membership.
    if abs(cos_lat) < 1e-6:
        dlon = 360.0
    else:
        dlon = min(360.0, radius_km / (111.320 * abs(cos_lat)))
    lon_min, lon_max = lon - dlon, lon + dlon
    if lon_min < -180 or lon_max > 180:
        lon_min, lon_max = -180.0, 180.0
    return (max(-90.0, lat - dlat), min(90.0, lat + dlat), lon_min, lon_max)


def _nearby_rows(rx_lat: float, rx_lon: float, radius_km: float) -> list[sqlite3.Row]:
    """Every row from a receiver within `radius_km`, in one query."""
    lat_min, lat_max, lon_min, lon_max = _bounding_box(rx_lat, rx_lon, radius_km)
    with _LOCK:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT tower_key, source, outcome, adsb_match_rate, hours_observed, rx_lat, rx_lon "
                "FROM tower_outcomes WHERE rx_lat BETWEEN ? AND ? AND rx_lon BETWEEN ? AND ?",
                (lat_min, lat_max, lon_min, lon_max),
            ).fetchall()
        finally:
            conn.close()
    return [r for r in rows if haversine(rx_lat, rx_lon, r["rx_lat"], r["rx_lon"]) <= radius_km]


def _aggregate(rows: Sequence[sqlite3.Row]) -> dict[str, tuple[float, float]]:
    """tower_key -> (total weight, weighted mean log-multiplier)."""
    sums: dict[str, list[float]] = {}
    for row in rows:
        residual = _residual(row)
        if residual is None:
            continue
        log_mult, weight = residual
        acc = sums.setdefault(row["tower_key"], [0.0, 0.0])
        acc[0] += weight
        acc[1] += weight * log_mult
    return {key: (weight, weighted_sum / weight) for key, (weight, weighted_sum) in sums.items() if weight > 0}


def apply_feedback(towers: list[dict], rx_lat: float, rx_lon: float, *, k: float = 5.0) -> None:
    """Stamp `feedback_n` / `feedback_factor` on each tower and correct its area.

    Mutates `towers` in place. `expected_area_km2`, when the ranking put one on
    the tower, is multiplied by the factor.

    Never raises into the request path: feedback is a correction, so a corrupt
    or unreadable database must cost the caller the correction, not the tower
    list. On any fault the towers are left exactly as they came in (no feedback
    keys at all), which is also how a caller can tell "no data" from "no store".
    """
    if not towers:
        return

    try:
        # One query per request, not one per tower: the row count in a 30 km
        # radius is small, and grouping in Python costs less than N round trips
        # inside a request handler.
        groups = _aggregate(_nearby_rows(rx_lat, rx_lon, FEEDBACK_RADIUS_KM))
    except Exception:
        logger.warning("Feedback lookup failed; ranking towers without it", exc_info=True)
        return

    for tower in towers:
        try:
            key = tower_key(tower["latitude"], tower["longitude"], float(tower["frequency_mhz"]) * 1e6)
        except (KeyError, TypeError, ValueError):
            # A tower the ranking could not place or tune has nothing to join
            # on. Leave it untouched rather than inventing a key it shares
            # with every other unplaceable tower.
            continue
        weight, mean_log = groups.get(key, (0.0, 0.0))
        factor = math.exp(weight / (weight + k) * mean_log) if weight > 0 else 1.0
        tower["feedback_n"] = float(weight)
        tower["feedback_factor"] = factor
        area = tower.get("expected_area_km2")
        if isinstance(area, (int, float)) and not isinstance(area, bool):
            tower["expected_area_km2"] = area * factor


# ── Admin summary ────────────────────────────────────────────────────────────


def summary(limit: int = 50) -> list[dict]:
    """Per-tower rollup of what the fleet has reported, busiest towers first."""
    with _LOCK:
        conn = _connect()
        try:
            heads = conn.execute(
                "SELECT tower_key, COUNT(*) AS rows_n, COUNT(DISTINCT node_id) AS nodes_n, "
                "COUNT(DISTINCT rx_cell) AS cells_n, MAX(observed_at) AS last_observed_at, "
                "GROUP_CONCAT(DISTINCT callsign) AS callsigns "
                "FROM tower_outcomes GROUP BY tower_key ORDER BY rows_n DESC, tower_key LIMIT ?",
                (limit,),
            ).fetchall()
            if not heads:
                return []
            keys = [h["tower_key"] for h in heads]
            # Only the rows behind the keys being reported: the residual model
            # lives in Python, so SQL cannot do this mean, but the scan stays
            # bounded by `limit` towers rather than the whole table.
            placeholders = ", ".join("?" * len(keys))
            detail = conn.execute(
                "SELECT tower_key, source, outcome, adsb_match_rate, hours_observed "
                f"FROM tower_outcomes WHERE tower_key IN ({placeholders})",
                keys,
            ).fetchall()
        finally:
            conn.close()

    groups = _aggregate(detail)
    out = []
    for head in heads:
        key = head["tower_key"]
        lat, lon, fc_hz = key.split("|")
        weight, mean_log = groups.get(key, (0.0, 0.0))
        raw_callsigns = head["callsigns"] or ""
        out.append(
            {
                "tower_key": key,
                "tx_lat": float(lat),
                "tx_lon": float(lon),
                "fc_hz": float(fc_hz),
                "rows": head["rows_n"],
                "nodes": head["nodes_n"],
                "receiver_cells": head["cells_n"],
                "weight": float(weight),
                "mean_multiplier": math.exp(mean_log) if weight > 0 else 1.0,
                "last_observed_at": head["last_observed_at"],
                "callsigns": sorted(c for c in raw_callsigns.split(",") if c),
            }
        )
    return out
