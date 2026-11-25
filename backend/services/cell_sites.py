"""Cell sites near a point, read from the cellular SQLite database.

The database is built offline by scripts/build_cell_db.py and copied onto the
cells volume (TOWER_FINDER_CELL_DB, default data/cells/cells.sqlite, relative to
the working directory). This module only ever reads it. Every call opens the
file read-only and closes it again, so a database copied over the old one is
served from the next call, and a missing file is reported rather than created
empty, which is what a plain sqlite3.connect() would do.

Whatever is wrong with the file (absent, not SQLite, another schema, no rows)
comes back as status "unavailable" with a sentence a caller can show, never as
an exception: cell rows are an addition to a tower search, so a bad file costs
the search its cell rows, not the request a 500.

A search reads its candidates through ``cell_candidates``: every row inside
the search box, as columns of an in-memory index of the whole file, with the
full rows of a site read from the file only when the ranking scores that site.
The index is built once per database file (keyed on its identity, like the
snapshot) and holds the numbers the ranking bounds a carrier with.

Blocking I/O throughout: an async handler calls these through
run_in_threadpool, as routes/cellular.py does.
"""

import logging
import math
import os
import sqlite3
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

ENABLED_ENV = "TOWER_FINDER_CELLULAR_ENABLED"
DB_ENV = "TOWER_FINDER_CELL_DB"
DEFAULT_DB_PATH = "data/cells/cells.sqlite"

SCHEMA_VERSION = "1"
COUNTRIES = ("us", "ca", "au")

# The cells table of schema v1, in column order. Selected by name, never *, so a
# later builder that adds a column cannot change what a row carries here.
CELL_COLUMNS = (
    "id",
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

_ON_VALUES = frozenset({"1", "true", "yes"})

# km per degree of latitude (and of longitude at the equator) on the mean sphere.
_KM_PER_DEG = 111.2
# Keeps the longitude half-width finite at a pole. Any cosine this small gives a
# half-width past 180 degrees for every real radius, so the box then spans all
# longitudes, which is the right answer there.
_MIN_COS_LAT = 1e-6

# Served on a public API, so they never name the file; the path goes to the log.
_UNAVAILABLE = "Cellular data unavailable: the cell database"
_MISSING = f"{_UNAVAILABLE} has not been installed."
_UNREADABLE = f"{_UNAVAILABLE} could not be read."
_SCHEMA = f"{_UNAVAILABLE} has an unsupported schema (expected version {SCHEMA_VERSION})."
_EMPTY = f"{_UNAVAILABLE} is empty."
_FAILED = "Cellular data unavailable: ranking the cell rows failed for this search."


def cellular_enabled() -> bool:
    """Whether cellular data is switched on. Read per call, not at import."""
    return os.getenv(ENABLED_ENV, "").strip().lower() in _ON_VALUES


def cell_db_path() -> str:
    """Where the cell database is expected. Read per call; an empty value counts as unset."""
    return os.getenv(DB_ENV, "").strip() or DEFAULT_DB_PATH


@dataclass(frozen=True)
class CellLookup:
    """One search's view of the cell database.

    ``status`` is "ok" or "unavailable". ``detail`` is None when ok, otherwise a
    sentence starting "Cellular data unavailable" that is safe to show.
    ``attribution`` covers the sources behind ``rows``, sorted by source, as
    {"source", "licence", "text"}. ``rows`` are cells rows keyed by their schema
    v1 column names with values as stored (``modelled`` is the raw comma list,
    NULL is None), nearest first. An unavailable lookup has no rows, no
    attribution and no built_at.
    """

    status: str
    detail: str | None
    built_at: str | None
    attribution: list[dict]
    rows: "list[dict] | CellCandidates"


def _no_countries() -> dict[str, int]:
    return dict.fromkeys(COUNTRIES, 0)


@dataclass(frozen=True)
class _Snapshot:
    """Everything about the file except its rows. ``detail`` is None when it is usable."""

    detail: str | None
    reason: str = ""  # for the log only
    built_at: str | None = None
    countries: dict[str, int] = field(default_factory=_no_countries)
    sources: dict[str, dict] = field(default_factory=dict)  # source -> {"rows", "licence", "source_date"}
    attribution: dict[str, dict] = field(default_factory=dict)  # source -> {"source", "licence", "text"}, sorted


# The snapshot of the last file read, keyed by that file's identity. One entry:
# the service reads one database. Only states read from the file's content are
# kept; an open or read error is retried on the next call, since a lock or a
# permission can clear without the file changing.
_cache: tuple[tuple, _Snapshot] | None = None
_cache_lock = threading.Lock()


def _connect(path: Path) -> sqlite3.Connection:
    # mode=ro: SQLite opens for reading only and fails on a missing file rather
    # than creating it. as_uri() percent-encodes the path, so a "?" or "#" in it
    # cannot be read as URI syntax.
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # No rows for a missing table, so a missing table reads as missing columns.
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _read_snapshot(path: Path) -> _Snapshot:
    """Check the file against schema v1 and count its rows per country and source.

    The counts are one GROUP BY over the whole cells table: a full scan, paid
    once per database file by the first call after it appears or changes (the
    snapshot is cached on the file's identity), and again only when it changes.
    It runs in the calling thread, which for both routes is a threadpool worker,
    never the event loop.
    """
    conn = _connect(path)
    try:
        if not {"key", "value"} <= _columns(conn, "meta"):
            return _Snapshot(_SCHEMA, "no meta table")
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        if str(meta.get("schema_version")) != SCHEMA_VERSION:
            return _Snapshot(_SCHEMA, f"schema_version {meta.get('schema_version')!r}")
        missing = sorted(set(CELL_COLUMNS) - _columns(conn, "cells"))
        if missing:
            return _Snapshot(_SCHEMA, f"cells lacks {', '.join(missing)}")
        groups = conn.execute(
            "SELECT country, source, licence, COUNT(*) FROM cells GROUP BY country, source, licence"
        ).fetchall()
    finally:
        conn.close()
    if not groups:
        return _Snapshot(_EMPTY, "no rows in cells")

    countries = _no_countries()
    rows_by_source: dict[str, int] = {}
    licences: dict[str, set[str]] = {}
    for country, source, licence, count in groups:
        if country in countries:
            countries[country] += count
        # NOT NULL in schema v1, but only a file written by the builder holds to
        # that, and a NULL here must not turn into a sort error and a 500.
        if source is None:
            continue
        source = str(source)
        rows_by_source[source] = rows_by_source.get(source, 0) + count
        if licence is not None:
            licences.setdefault(source, set()).add(str(licence))

    sources, attribution = {}, {}
    for source in sorted(rows_by_source):
        # One licence per source from the builder. Should a source ever carry two,
        # both are named: an attribution that drops a licence is the wrong one.
        licence = ", ".join(sorted(licences.get(source, ())))
        sources[source] = {
            "rows": rows_by_source[source],
            "licence": licence,
            "source_date": meta.get(f"source_date:{source}"),
        }
        attribution[source] = {"source": source, "licence": licence, "text": meta.get(f"attribution:{source}")}
    return _Snapshot(None, "", meta.get("built_at"), countries, sources, attribution)


def _snapshot(configured: str) -> tuple[Path | None, _Snapshot]:
    """The database file and what it holds, or a snapshot saying why it is unusable."""
    global _cache
    shown = configured
    try:
        path = Path(configured).resolve()
        shown = str(path)
        info = path.stat()
    except FileNotFoundError:
        snapshot = _Snapshot(_MISSING, "no such file")
    except (OSError, RuntimeError, ValueError) as exc:  # RuntimeError: a symlink loop on 3.12
        snapshot = _Snapshot(_UNREADABLE, repr(exc))
    else:
        if not stat.S_ISREG(info.st_mode):
            # A bind mount of a missing host file leaves a directory here.
            snapshot = _Snapshot(_UNREADABLE, "not a regular file")
        elif info.st_size == 0:
            # SQLite opens a zero-byte file as a database with no tables; the
            # usual cause is a copy that never landed, which "empty" describes.
            snapshot = _Snapshot(_EMPTY, "zero bytes")
        else:
            # ctime and inode on top of mtime and size: docker cp keeps the
            # source file's mtime, and a same-size rebuild is possible, but a
            # write always moves ctime.
            identity = (shown, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            with _cache_lock:
                if _cache is not None and _cache[0] == identity:
                    snapshot = _cache[1]
                else:
                    try:
                        snapshot = _read_snapshot(path)
                    except (sqlite3.Error, OSError) as exc:
                        snapshot = _Snapshot(_UNREADABLE, repr(exc))
                    else:
                        _cache = (identity, snapshot)
            if snapshot.detail is None:
                return path, snapshot
    logger.warning("Cellular data unavailable from %s: %s", shown, snapshot.reason)
    return None, snapshot


def _box(lat: float, lon: float, radius_km: float) -> tuple[float, float, list[tuple[float, float]]]:
    """(lat_min, lat_max, lon ranges) of a box holding the radius; two lon ranges across +/-180."""
    dlat = radius_km / _KM_PER_DEG
    dlon = radius_km / (_KM_PER_DEG * max(math.cos(math.radians(lat)), _MIN_COS_LAT))
    lat_min, lat_max = max(-90.0, lat - dlat), min(90.0, lat + dlat)
    # A box that reaches a pole holds every longitude near it.
    if dlon >= 180 or lat_min <= -90 or lat_max >= 90:
        return lat_min, lat_max, [(-180.0, 180.0)]
    low, high = lon - dlon, lon + dlon
    if low < -180:
        return lat_min, lat_max, [(low + 360, 180.0), (-180.0, high)]
    if high > 180:
        return lat_min, lat_max, [(low, 180.0), (-180.0, high - 360)]
    return lat_min, lat_max, [(low, high)]


def _box_sql(lat: float, lon: float, radius_km: float) -> tuple[str, dict]:
    """The WHERE clause of the rows inside the box, and its parameters."""
    lat_min, lat_max, lon_ranges = _box(lat, lon, radius_km)
    params: dict[str, float] = {"lat_min": lat_min, "lat_max": lat_max, "lat": lat, "lon": lon}
    params["kx"] = math.cos(math.radians(lat)) ** 2
    for i, (low, high) in enumerate(lon_ranges):
        params[f"lo{i}"], params[f"hi{i}"] = low, high
    in_lon = " OR ".join(f"lon BETWEEN :lo{i} AND :hi{i}" for i in range(len(lon_ranges)))
    return f"lat BETWEEN :lat_min AND :lat_max AND ({in_lon})", params


def _nearest_rows(
    conn: sqlite3.Connection, lat: float, lon: float, radius_km: float, max_rows: int | None
) -> list[dict]:
    where, params = _box_sql(lat, lon, radius_km)
    # LIMIT -1 is SQLite's "no limit"; a negative max_rows means no rows, not every row.
    params["limit"] = -1 if max_rows is None else max(0, int(max_rows))
    # Equirectangular distance squared, in degrees of latitude; the longitude
    # difference is taken the short way round so a box split at +/-180 still
    # orders by real distance. The lat range is what cells_lat_lon serves.
    # row_order_key is the same sum in the same float arithmetic.
    dlon = "MIN(ABS(lon - :lon), 360 - ABS(lon - :lon))"
    sql = (
        f"SELECT {', '.join(CELL_COLUMNS)} FROM cells WHERE {where} "
        f"ORDER BY (lat - :lat) * (lat - :lat) + :kx * {dlon} * {dlon}, id LIMIT :limit"
    )
    return [dict(zip(CELL_COLUMNS, row)) for row in conn.execute(sql, params)]


def row_order_key(row: dict, lat: float, lon: float) -> tuple[float, int]:
    """Where ``row`` sorts in lookup_cells' order around (lat, lon): its ORDER BY, in Python's floats.

    SQLite's REAL arithmetic is IEEE double, and the sum is written in the
    same order, so the key is the one the query sorts on.
    """
    kx = math.cos(math.radians(lat)) ** 2
    dlon = min(abs(row["lon"] - lon), 360 - abs(row["lon"] - lon))
    return ((row["lat"] - lat) * (row["lat"] - lat) + kx * dlon * dlon, row["id"])


def lookup_cells(lat: float, lon: float, radius_km: float, max_rows: int | None = None) -> CellLookup:
    """The cells inside a box around ``radius_km`` of (lat, lon), nearest first, in any country.

    Every row of the box, or the ``max_rows`` nearest. Nearest is the reader's
    order (row_order_key), the order the ranking's ties fall back on. No exact
    radius cut: the box can hold rows a little past the radius, and the
    ranking cuts by distance. Rows are read from the file on every call.
    """
    path, snapshot = _snapshot(cell_db_path())
    if path is None:
        return CellLookup("unavailable", snapshot.detail, None, [], [])
    try:
        conn = _connect(path)
        try:
            rows = _nearest_rows(conn, lat, lon, radius_km, max_rows)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Cellular data unavailable from %s: %r", path, exc)
        return CellLookup("unavailable", _UNREADABLE, None, [], [])
    used = sorted({str(row["source"]) for row in rows if row["source"] is not None})
    # A file swapped between the snapshot and this read can hold a source the
    # snapshot lacks; the next call reads the new file whole.
    attribution = [dict(snapshot.attribution[source]) for source in used if source in snapshot.attribution]
    return CellLookup("ok", None, snapshot.built_at, attribution, rows)


# ── Candidates for a search: an index of the whole file ─────────────────────
#
# A cellular search ranks every carrier inside its radius, which in a dense
# city is a few hundred thousand rows; reading them from SQLite into Python
# takes longer than the search may. The ranking needs only a few numbers per
# row to bound a carrier's score (tower_ranking), and the full rows only of
# the sites it goes on to score. So the numbers of every row are held in
# memory as columns, sorted by latitude, built once per database file and
# keyed on its identity like the snapshot; the full rows are read by id.

# The index columns, read in one pass over the table.
_INDEX_COLUMNS = (
    "id, lat, lon, freq_mhz, bandwidth_mhz, eirp_dbm, height_m, azimuth_deg, beamwidth_deg, tilt_deg, "
    "radio, source, site_id"
)
_INDEX_CHUNK = 20_000


class CellDataChanged(RuntimeError):
    """The database file changed between building the index and reading a row of it."""


def _identity(path: Path) -> tuple:
    info = path.stat()
    return (str(path), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True, eq=False)
class CellIndex:
    """Every row of one database file, as columns sorted by latitude.

    Positions index every column alike. ``lat``, ``lon``, ``freq`` and
    ``bandwidth`` are the stored doubles; ``eirp``, ``height``, ``azimuth``,
    ``beamwidth`` and ``tilt`` are float32 copies, which a bound has to allow
    for (about 1e-7 of the value). NULL is NaN. ``radio`` and ``source`` are
    codes into ``radios`` and ``sources`` (a NULL radio is None there), and
    ``site`` is a hash of the (source, site_id) pair, the unit a carrier is
    deduped within: rows of one site always share it, and two sites that
    happen to share one are only read together.
    """

    identity: tuple
    id: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    freq: np.ndarray
    bandwidth: np.ndarray
    eirp: np.ndarray
    height: np.ndarray
    azimuth: np.ndarray
    beamwidth: np.ndarray
    tilt: np.ndarray
    radio: np.ndarray
    radios: tuple
    source: np.ndarray
    sources: tuple
    site: np.ndarray

    @property
    def nbytes(self) -> int:
        return sum(getattr(self, name).nbytes for name in _INDEX_ARRAYS)


_INDEX_ARRAYS = ("id", "lat", "lon", "freq", "bandwidth", "eirp", "height", "azimuth", "beamwidth", "tilt")
_INDEX_ARRAYS += ("radio", "source", "site")
_INDEX_DTYPES = dict(
    zip(_INDEX_ARRAYS, (np.int64,) + (np.float64,) * 4 + (np.float32,) * 5 + (np.int8, np.int8, np.int64))
)

_index: CellIndex | None = None
_index_lock = threading.Lock()


def _read_index(path: Path, identity: tuple) -> CellIndex:
    """One pass over the table, in chunks, into columns; then sorted by latitude."""
    radios: dict = {}
    sources: dict = {}
    conn = _connect(path)
    try:
        # One read transaction, so the count and the rows come from the same
        # file: each chunk goes straight into columns sized by the count, and
        # the peak of the build is about one column over the index itself.
        conn.execute("BEGIN")
        (size,) = conn.execute("SELECT COUNT(*) FROM cells").fetchone()
        columns = {name: np.empty(size, dtype=dtype) for name, dtype in _INDEX_DTYPES.items()}
        cursor = conn.execute(f"SELECT {_INDEX_COLUMNS} FROM cells")
        filled = 0
        while chunk := cursor.fetchmany(_INDEX_CHUNK):
            at = slice(filled, filled + len(chunk))
            filled = at.stop
            if filled > size:
                raise ValueError("more rows in cells than it counted")
            cols = list(zip(*chunk))
            for name, col in zip(_INDEX_ARRAYS[:10], cols[:10]):
                columns[name][at] = np.array(col, dtype=_INDEX_DTYPES[name])
            radio_codes = [radios.setdefault(r, len(radios)) for r in cols[10]]
            source_codes = [sources.setdefault(v, len(sources)) for v in cols[11]]
            # Before the cast: a code past the int8 range would raise OverflowError there.
            if len(radios) > 127 or len(sources) > 127:
                raise ValueError("more distinct radio or source values than the index codes")
            columns["radio"][at] = np.array(radio_codes, dtype=np.int8)
            columns["source"][at] = np.array(source_codes, dtype=np.int8)
            # A hash, not a number from a table of every site: that table held
            # about 100 MB more at the peak of the build.
            columns["site"][at] = np.array([hash(key) for key in zip(cols[11], cols[12])], dtype=np.int64)
    finally:
        conn.close()
    if filled != size:
        raise ValueError("fewer rows in cells than it counted")
    if not size:
        raise ValueError("no rows in cells")
    order = np.argsort(columns["lat"], kind="stable")
    for name in _INDEX_ARRAYS:
        columns[name] = columns[name][order]
        columns[name].setflags(write=False)
    return CellIndex(
        identity=identity,
        radios=tuple(radios),
        sources=tuple(sources),
        **columns,
    )


def _cell_index(path: Path) -> CellIndex:
    """The index of ``path``, built on the first call after the file appears or changes."""
    global _index
    identity = _identity(path)
    with _index_lock:
        if _index is None or _index.identity != identity:
            # Dropped before the new one is read, so two never sit in memory at once.
            _index = None
            _index = _read_index(path, identity)
        return _index


def warm_index() -> bool:
    """Build the index of the configured database before any search asks for it; True when it is ready.

    For a thread started at startup. A search that arrives meanwhile waits on
    the index lock for this build and takes its index, so the file is read
    once. A file that cannot be indexed is only logged here: the next search
    tries again and answers as it would have.
    """
    path, snapshot = _snapshot(cell_db_path())
    if path is None:
        logger.warning("Cell index not built at startup: %s", snapshot.reason)
        return False
    try:
        _cell_index(path)
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Cell index not built at startup from %s: %r", path, exc)
        return False
    return True


@dataclass(eq=False)
class CellCandidates:
    """Every row inside one search's box, as positions into the file's index.

    ``rows_of`` reads the full rows (schema v1 columns, as lookup_cells gives
    them) of given positions, and ``all_rows`` every row of the box in
    lookup_cells' order. Both read through one connection opened on first use
    and checked against the file the index was built from, so a database
    copied over the old one mid-search raises CellDataChanged rather than
    mixing two files; so does a file that can no longer be read through (gone,
    another schema, not sqlite). close() releases it.
    """

    lat: float
    lon: float
    radius_km: float
    index: CellIndex
    at: np.ndarray
    path: Path
    _conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5, check_same_thread=False)
            try:
                # A read transaction for the rest of the search: what it reads
                # is the file as it is now, whatever is renamed over it later.
                conn.execute("BEGIN")
                conn.execute("SELECT COUNT(*) FROM meta").fetchone()
                if _identity(self.path) != self.index.identity:
                    raise CellDataChanged(f"{self.path} changed since its index was built")
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def rows_of(self, positions) -> list[dict]:
        ids = [int(i) for i in self.index.id[np.asarray(positions, dtype=np.int64)]]
        rows = []
        try:
            conn = self._connection()
            for start in range(0, len(ids), 900):
                chunk = ids[start : start + 900]
                marks = ", ".join("?" * len(chunk))
                sql = f"SELECT {', '.join(CELL_COLUMNS)} FROM cells WHERE id IN ({marks})"
                rows += [dict(zip(CELL_COLUMNS, row)) for row in conn.execute(sql, chunk)]
        except (sqlite3.Error, OSError) as exc:
            raise CellDataChanged(f"{self.path} could not be read: {exc!r}") from exc
        if len(rows) != len(ids):
            raise CellDataChanged(f"{self.path} no longer holds every row of its index")
        return rows

    def all_rows(self) -> list[dict]:
        try:
            return _nearest_rows(self._connection(), self.lat, self.lon, self.radius_km, None)
        except (sqlite3.Error, OSError) as exc:
            raise CellDataChanged(f"{self.path} could not be read: {exc!r}") from exc

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _in_box(index: CellIndex, lat: float, lon: float, radius_km: float) -> np.ndarray:
    """Positions of the index rows inside the box _box draws, in index order (the same rows lookup_cells reads)."""
    lat_min, lat_max, lon_ranges = _box(lat, lon, radius_km)
    first = int(np.searchsorted(index.lat, lat_min, side="left"))
    last = int(np.searchsorted(index.lat, lat_max, side="right"))
    lons = index.lon[first:last]
    inside = np.zeros(lons.size, dtype=bool)
    for low, high in lon_ranges:
        inside |= (lons >= low) & (lons <= high)
    return first + np.flatnonzero(inside)


def cell_candidates(lat: float, lon: float, radius_km: float) -> CellLookup:
    """lookup_cells for the ranking: ``rows`` is a CellCandidates over every row of the box.

    The first call per database file builds the index, a full scan of the
    table (~190 MB for 2.7 million rows, about 2.5 times that at the peak of
    the build), in the calling thread, unless warm_index has built it or is
    building it, in which case the call waits for that build.
    """
    path, snapshot = _snapshot(cell_db_path())
    if path is None:
        return CellLookup("unavailable", snapshot.detail, None, [], [])
    try:
        index = _cell_index(path)
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("Cellular data unavailable from %s: %r", path, exc)
        return CellLookup("unavailable", _UNREADABLE, None, [], [])
    at = _in_box(index, lat, lon, radius_km)
    used = sorted(index.sources[code] for code in np.unique(index.source[at]).tolist())
    attribution = [dict(snapshot.attribution[source]) for source in used if source in snapshot.attribution]
    return CellLookup("ok", None, snapshot.built_at, attribution, CellCandidates(lat, lon, radius_km, index, at, path))


def unreadable_lookup() -> CellLookup:
    """The lookup of a database that could not be read through: no rows, and the sentence that says so."""
    return CellLookup("unavailable", _UNREADABLE, None, [], [])


def failed_lookup() -> CellLookup:
    """The lookup of a search whose cell ranking failed: no rows, and the sentence that says so."""
    return CellLookup("unavailable", _FAILED, None, [], [])


def cellular_block(lookup: CellLookup) -> dict:
    """The ``cellular`` block of a /api/towers response."""
    return {
        "status": lookup.status,
        "detail": lookup.detail,
        "built_at": lookup.built_at,
        "attribution": [dict(entry) for entry in lookup.attribution],
    }


def cellular_status() -> dict:
    """The GET /api/cellular body, served while the flag is on.

    Counts and attribution cover the whole database. When it is unavailable,
    every country counts 0 and there are no sources, attribution or built_at.
    """
    _, snapshot = _snapshot(cell_db_path())
    return {
        "enabled": True,
        "status": "ok" if snapshot.detail is None else "unavailable",
        "detail": snapshot.detail,
        "built_at": snapshot.built_at,
        "countries": dict(snapshot.countries),
        "sources": {source: dict(entry) for source, entry in snapshot.sources.items()},
        "attribution": [dict(entry) for entry in snapshot.attribution.values()],
    }
