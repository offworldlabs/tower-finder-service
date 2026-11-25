"""Build the cell site database (SQLite) from public regulator and crowd datasets.

For the operator, offline. The database feeds the tower finder's cellular
search; this script is the only writer of its schema (version 1).

  uv run python scripts/build_cell_db.py --raw-dir <dir> --out <file.sqlite> [--counts <COUNTS.txt>]

Sources, each read from files already placed under --raw-dir:

  ised        ised/Site_Data_Extract_FX.zip           Canada, ISED site data
  acma        acma/spectra_rrl.zip                    Australia, ACMA RRL
  fcc_uls     fcc/l_cell.zip                          United States, FCC ULS Cellular licences
  opencellid  opencellid/*.csv[.gz]                   crowd-sourced cells, read only
                                                      when opencellid/READY exists; sites
                                                      are snapped to FCC ASR structures
                                                      from fcc/r_tower.zip when present

A source with a missing input file is skipped and named in the COUNTS report.
Zips are streamed, never extracted, and nothing is fetched from the network.
Only rows whose downlink frequency falls inside a cellular band group (BANDS)
are kept; rows outside every group, and rows failing basic validation, are
dropped and counted per source.

A row is one carrier. A kept row wider than one carrier (NR 100 MHz, LTE 20,
UMTS 5, CDMA 1.25, GSM 0.2; no radio: 100 MHz in CELL3500, 20 MHz in the other
groups) is cut to that width and 'bandwidth' is added to modelled. A row with a
radio keeps its EIRP: ISED files the licensed block as the bandwidth (9 to 10 MHz
for most GSM records) but the power of one carrier. A row without one (ACMA,
FCC ULS) registers the licensing bandwidth, and ACMA a power that grows with it,
so its EIRP is lowered by 10*log10(width / bandwidth) (the same power density,
one carrier's share) and 'eirp' is added to modelled too. Counted per source as
"wider than one carrier, reduced".

tilt_deg is the downtilt in degrees, positive below the horizon.

Confidence is fixed per source and position quality:

  ised                                   0.9
  acma                                   0.85
  fcc_uls                                0.6
  opencellid, snapped to an FCC ASR structure   0.4
  opencellid, estimated position          0.25

The database is written to <out>.tmp and renamed over <out> when complete, so a
failed build never leaves a partial file. COUNTS is printed to stdout and
written to --counts when given.

Exit status: 0 on success, 1 when no source has all its input files.
"""

from __future__ import annotations

import argparse
import collections
import importlib
import math
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cell_sources import CARRIER_MAX_MHZ  # noqa: E402

SCHEMA_VERSION = "1"
BATCH_SIZE = 10000
PROGRESS_EVERY = 200000
SOURCE_NAMES = ("ised", "acma", "fcc", "opencellid")
COUNTRIES = ("au", "ca", "us")

# Downlink MHz, inclusive.
BANDS: tuple[tuple[str, float, float], ...] = (
    ("CELL600", 617, 652),
    ("CELL700", 716, 803),
    ("CELL850", 859, 894),
    ("CELL900", 925, 960),
    ("CELL1800", 1805, 1880),
    ("CELL1900", 1930, 1995),
    ("CELL2100", 2110, 2200),
    ("CELL2300", 2300, 2400),
    ("CELL2600", 2496, 2690),
    ("CELL3500", 3300, 4200),
)

# Widest single carrier, MHz, by radio in CARRIER_MAX_MHZ. A row without a radio may be one NR carrier only in NR_BANDS.
NO_RADIO_CARRIER_MAX_MHZ = 20.0
NR_BANDS = frozenset({"CELL3500"})
MODELLED_ORDER = ("freq", "bandwidth", "eirp", "height", "azimuth", "position")

COLUMNS = (
    "country",
    "source",
    "licence",
    "site_id",
    "cell_id",
    "operator",
    "radio",
    "lat",
    "lon",
    "freq_mhz",
    "bandwidth_mhz",
    "eirp_dbm",
    "height_m",
    "azimuth_deg",
    "beamwidth_deg",
    "tilt_deg",
    "modelled",
    "confidence",
    "updated",
)
_COLUMN_SET = frozenset(COLUMNS)

CELLS_DDL = """CREATE TABLE cells (
  id            INTEGER PRIMARY KEY,
  country       TEXT NOT NULL,   -- 'us' | 'ca' | 'au'
  source        TEXT NOT NULL,   -- 'ised' | 'acma' | 'fcc_uls' | 'opencellid'
  licence       TEXT NOT NULL,   -- 'OGL-Canada-2.0' | 'ACMA-RRL' | 'US-Gov-PD' | 'CC-BY-SA-4.0'
  site_id       TEXT NOT NULL,   -- stable site key inside its source, never empty
  cell_id       TEXT,            -- sector/cell key inside the site (ECGI, PCI, antenna no.), NULL if unknown
  operator      TEXT,            -- licensee/network name (a company), never a person; NULL if unknown
  radio         TEXT,            -- 'GSM' | 'UMTS' | 'CDMA' | 'LTE' | 'NR' | NULL
  lat           REAL NOT NULL,
  lon           REAL NOT NULL,
  freq_mhz      REAL NOT NULL,   -- downlink centre frequency
  bandwidth_mhz REAL,            -- channel bandwidth
  eirp_dbm      REAL,            -- per carrier, per sector
  height_m      REAL,            -- antenna height above ground
  azimuth_deg   REAL,            -- NULL = unknown / omni
  beamwidth_deg REAL,            -- horizontal 3 dB beamwidth, NULL = unknown
  tilt_deg      REAL,
  modelled      TEXT NOT NULL DEFAULT '',  -- comma list of modelled fields: freq,bandwidth,eirp,height,azimuth,position
  confidence    REAL NOT NULL,   -- 0..1
  updated       TEXT             -- source record date, ISO yyyy-mm-dd, NULL if unknown
)"""
INDEX_DDL = "CREATE INDEX cells_lat_lon ON cells(lat, lon)"
META_DDL = "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
INSERT_SQL = f"INSERT INTO cells ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"


def default_sources():
    return [importlib.import_module(f"cell_sources.{n}") for n in SOURCE_NAMES]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid(row, source) -> bool:
    if not isinstance(row, dict) or row.keys() != _COLUMN_SET:
        return False
    site_id = row["site_id"]
    if not isinstance(site_id, str) or not site_id:
        return False
    lat, lon = row["lat"], row["lon"]
    if not (_is_number(lat) and _is_number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    if not _is_number(row["freq_mhz"]):
        return False
    return row["source"] == source.SOURCE and row["licence"] == source.LICENCE


def _band(freq_mhz: float) -> str | None:
    return next((name for name, lo, hi in BANDS if lo <= freq_mhz <= hi), None)


def _one_carrier(row: dict, band: str) -> bool:
    """Cut a row wider than one carrier to one carrier, flagged; True when cut.

    A row with a radio keeps its per-carrier EIRP; a row without one keeps the same power density.
    """
    radio = row["radio"]
    if radio is None:
        ceiling = CARRIER_MAX_MHZ["NR"] if band in NR_BANDS else NO_RADIO_CARRIER_MAX_MHZ
    else:
        ceiling = CARRIER_MAX_MHZ[radio]
    bandwidth = row["bandwidth_mhz"]
    if not _is_number(bandwidth) or bandwidth <= ceiling:
        return False
    row["bandwidth_mhz"] = ceiling
    flagged = {"bandwidth"}
    if radio is None and _is_number(row["eirp_dbm"]):
        row["eirp_dbm"] = round(row["eirp_dbm"] + 10 * math.log10(ceiling / bandwidth), 2)
        flagged.add("eirp")
    fields = {f for f in (row["modelled"] or "").split(",") if f} | flagged
    row["modelled"] = ",".join([f for f in MODELLED_ORDER if f in fields] + sorted(fields - set(MODELLED_ORDER)))
    return True


def _share(modelled: int, rows: int) -> str:
    return f"{100.0 * modelled / rows if rows else 0.0:.1f}%"


def _insert_source(con, source, raw_dir, drops, extra) -> None:
    stats: collections.Counter = collections.Counter()
    batch = []
    read = 0
    for row in source.iter_rows(raw_dir, stats):
        read += 1
        if read % PROGRESS_EVERY == 0:
            print(f"{source.SOURCE}: {read} rows read", file=sys.stderr, flush=True)
        if not _valid(row, source):
            drops["invalid row"] += 1
            continue
        band = _band(row["freq_mhz"])
        if band is None:
            drops["outside cellular bands"] += 1
            continue
        if _one_carrier(row, band):
            stats["wider than one carrier, reduced"] += 1
        batch.append(tuple(row[c] for c in COLUMNS))
        if len(batch) >= BATCH_SIZE:
            con.executemany(INSERT_SQL, batch)
            batch.clear()
    if batch:
        con.executemany(INSERT_SQL, batch)
    extra.update(stats)


def _counts_text(con, out, built_at, used, skipped, per_source) -> str:
    by_country = dict(con.execute("SELECT country, COUNT(*) FROM cells GROUP BY country"))
    total = con.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    modelled = con.execute("SELECT COUNT(*) FROM cells WHERE modelled != ''").fetchone()[0]
    lines = ["Cell site database", f"built_at: {built_at}", f"schema_version: {SCHEMA_VERSION}", f"db: {out}", ""]
    lines.append("Rows per country")
    for country in COUNTRIES:
        lines.append(f"  {country}: {by_country.get(country, 0)}")
    lines.append(f"  total: {total}")
    lines.append(f"  modelled share: {_share(modelled, total)}")
    lines += ["", "Per source"]
    for source, date in used:
        rows, sites, modelled_rows = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT site_id), COALESCE(SUM(modelled != ''), 0) FROM cells WHERE source = ?",
            (source.SOURCE,),
        ).fetchone()
        drops, extra = per_source[source.SOURCE]
        lines.append(
            f"  {source.SOURCE} ({source.LICENCE}), source date {date or 'unknown'}: "
            f"{rows} rows, {sites} sites, modelled share {_share(modelled_rows, rows)}"
        )
        lines.append(f"    dropped outside cellular bands: {drops['outside cellular bands']}")
        lines.append(f"    dropped invalid rows: {drops['invalid row']}")
        for key in sorted(extra):
            lines.append(f"    {key}: {extra[key]}")
    lines += ["", "Skipped sources"]
    if skipped:
        lines += [f"  {source.SOURCE}: missing {', '.join(str(p) for p in missing)}" for source, missing in skipped]
    else:
        lines.append("  none")
    used_opencellid = any(source.SOURCE == "opencellid" for source, _ in used)
    lines += ["", "US: FCC ULS CL + OpenCelliD" if used_opencellid else "US: FCC-only (OpenCelliD files not ready)"]
    return "\n".join(lines) + "\n"


def build(raw_dir: Path, out: Path, counts_path: Path | None = None, sources=None) -> str:
    """Build the database at `out` and return the COUNTS text."""
    raw_dir, out = Path(raw_dir), Path(out)
    if sources is None:
        sources = default_sources()
    available, skipped = [], []
    for source in sources:
        missing = [p for p in source.input_paths(raw_dir) if not Path(p).exists()]
        if missing:
            skipped.append((source, missing))
        else:
            available.append(source)
    if not available:
        detail = "; ".join(f"{s.SOURCE}: missing {', '.join(str(p) for p in m)}" for s, m in skipped)
        raise ValueError(f"no source has all its input files under {raw_dir} ({detail})")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    built_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute(CELLS_DDL)
        used, per_source = [], {}
        for source in available:
            date = source.source_date(raw_dir)
            drops: collections.Counter = collections.Counter()
            extra: collections.Counter = collections.Counter()
            _insert_source(con, source, raw_dir, drops, extra)
            used.append((source, date))
            per_source[source.SOURCE] = (drops, extra)
        con.commit()
        con.execute(INDEX_DDL)
        con.execute(META_DDL)
        meta = [("schema_version", SCHEMA_VERSION), ("built_at", built_at)]
        for source, date in used:
            if date is not None:
                meta.append((f"source_date:{source.SOURCE}", date))
            meta.append((f"attribution:{source.SOURCE}", source.ATTRIBUTION))
        con.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta)
        con.commit()
        text = _counts_text(con, out, built_at, used, skipped, per_source)
        con.close()
        os.replace(tmp, out)
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    if counts_path is not None:
        counts_path = Path(counts_path)
        counts_path.parent.mkdir(parents=True, exist_ok=True)
        counts_path.write_text(text, encoding="utf-8")
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the cell site database from the raw source files.")
    ap.add_argument("--raw-dir", required=True, type=Path, help="directory holding the downloaded source files")
    ap.add_argument("--out", required=True, type=Path, help="SQLite file to write")
    ap.add_argument("--counts", type=Path, help="also write the COUNTS report to this file")
    args = ap.parse_args(argv)
    try:
        text = build(args.raw_dir, args.out, args.counts)
    except ValueError as exc:
        print(f"build_cell_db: {exc}", file=sys.stderr)
        return 1
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
