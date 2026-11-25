"""The cell database reader and GET /api/cellular.

``write_cell_db`` builds a database in schema v1, spelled out here rather than
taken from the builder, so the reader is held to the schema and not to whatever
the builder writes today.
"""

import sqlite3
import threading
from pathlib import Path

import pytest
from app import create_app
from fastapi.testclient import TestClient
from services import cell_sites

SCHEMA_V1 = """
CREATE TABLE cells (
  id INTEGER PRIMARY KEY, country TEXT NOT NULL, source TEXT NOT NULL, licence TEXT NOT NULL,
  site_id TEXT NOT NULL, cell_id TEXT, operator TEXT, radio TEXT, lat REAL NOT NULL, lon REAL NOT NULL,
  freq_mhz REAL NOT NULL, bandwidth_mhz REAL, eirp_dbm REAL, height_m REAL, azimuth_deg REAL,
  beamwidth_deg REAL, tilt_deg REAL, modelled TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL, updated TEXT
);
CREATE INDEX cells_lat_lon ON cells(lat, lon);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
ROW_DEFAULTS = {"country": "ca", "source": "ised", "licence": "OGL-Canada-2.0", "site_id": "S1"}
ROW_DEFAULTS |= {"freq_mhz": 2140.0, "confidence": 1.0, "modelled": ""}
FCC = {"country": "us", "source": "fcc_uls", "licence": "US-Gov-PD"}
ACMA = {"country": "au", "source": "acma", "licence": "ACMA-RRL"}
META = {"schema_version": "1", "built_at": "2026-01-01T00:00:00Z", "attribution:ised": "Example ised attribution."}


def write_cell_db(path, rows: list[dict], meta: dict | None = None) -> Path:
    """A new cell database at ``path``. Each row needs lat and lon; the other
    required columns default to an ISED row in Canada. ``meta`` replaces the
    default meta table when given."""
    path = Path(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_V1)
        with conn:
            for row in rows:
                row = {**ROW_DEFAULTS, **row}
                marks = ", ".join("?" * len(row))
                conn.execute(f"INSERT INTO cells ({', '.join(row)}) VALUES ({marks})", list(row.values()))
            conn.executemany("INSERT INTO meta VALUES (?, ?)", (META if meta is None else meta).items())
    finally:
        conn.close()
    return path


def test_the_flag_is_on_only_for_1_true_or_yes(monkeypatch):
    def enabled(value):
        if value is None:
            monkeypatch.delenv("TOWER_FINDER_CELLULAR_ENABLED", raising=False)
        else:
            monkeypatch.setenv("TOWER_FINDER_CELLULAR_ENABLED", value)
        return cell_sites.cellular_enabled()

    assert [enabled(v) for v in ("1", "true", "yes", "TRUE", " yes ")] == [True] * 5
    assert [enabled(v) for v in (None, "", "0", "false", "no", "on")] == [False] * 6


def unavailable_detail(monkeypatch, path) -> str:
    monkeypatch.setenv("TOWER_FINDER_CELL_DB", str(path))
    lookup = cell_sites.lookup_cells(43.6532, -79.3832, 50.0)
    assert (lookup.status, lookup.built_at, lookup.attribution, lookup.rows) == ("unavailable", None, [], [])
    assert lookup.detail.startswith("Cellular data unavailable")
    return lookup.detail


def test_a_missing_database_is_unavailable_and_not_created(tmp_path, monkeypatch):
    assert "has not been installed" in unavailable_detail(monkeypatch, tmp_path / "cells.sqlite")
    assert not (tmp_path / "cells.sqlite").exists()


def test_a_file_that_is_not_sqlite_is_unavailable(tmp_path, monkeypatch):
    (tmp_path / "cells.sqlite").write_text("not a database\n" * 100)
    assert "could not be read" in unavailable_detail(monkeypatch, tmp_path / "cells.sqlite")


def test_a_database_without_rows_is_unavailable(tmp_path, monkeypatch):
    assert "is empty" in unavailable_detail(monkeypatch, write_cell_db(tmp_path / "cells.sqlite", []))


def test_another_schema_version_is_unavailable(tmp_path, monkeypatch):
    path = write_cell_db(tmp_path / "cells.sqlite", [{"lat": 43.6532, "lon": -79.3832}], {"schema_version": "2"})
    assert "unsupported schema (expected version 1)" in unavailable_detail(monkeypatch, path)


def test_more_than_127_distinct_radio_values_make_the_database_unavailable(tmp_path, monkeypatch):
    rows = [{"lat": 43.6 + i * 0.001, "lon": -79.4, "radio": f"r{i}", "site_id": f"S{i}"} for i in range(129)]
    monkeypatch.setenv("TOWER_FINDER_CELL_DB", str(write_cell_db(tmp_path / "cells.sqlite", rows)))
    monkeypatch.setattr(cell_sites, "_index", None)
    lookup = cell_sites.cell_candidates(43.6, -79.4, 10.0)
    assert lookup.status == "unavailable" and lookup.detail.endswith("could not be read.")
    assert cell_sites.warm_index() is False


def test_lookup_returns_the_nearest_rows_in_the_box(tmp_path, monkeypatch):
    rows = [
        {"site_id": "far", "lat": 43.7032, "lon": -79.3832},  # 5.6 km
        {"site_id": "outside", "lat": 44.1532, "lon": -79.3832},  # 56 km, past a 50 km box
        {"site_id": "near", "lat": 43.6542, "lon": -79.3832, "radio": "LTE", "modelled": "eirp,height"},
        {"site_id": "mid", "lat": 43.6532, "lon": -79.3732, **FCC},  # 0.8 km
        {"site_id": "sydney", "lat": -33.8688, "lon": 151.2093, **ACMA},
    ]
    path = write_cell_db(tmp_path / "cells.sqlite", rows, {**META, "attribution:fcc_uls": "Example fcc attribution."})
    monkeypatch.setenv("TOWER_FINDER_CELL_DB", str(path))

    lookup = cell_sites.lookup_cells(43.6532, -79.3832, 50.0)
    assert (lookup.status, lookup.detail, lookup.built_at) == ("ok", None, "2026-01-01T00:00:00Z")
    assert [row["site_id"] for row in lookup.rows] == ["near", "mid", "far"]
    assert lookup.rows[0] == {
        **{"id": 3, "country": "ca", "source": "ised", "licence": "OGL-Canada-2.0", "site_id": "near"},
        **{"cell_id": None, "operator": None, "radio": "LTE", "lat": 43.6542, "lon": -79.3832, "freq_mhz": 2140.0},
        **{"bandwidth_mhz": None, "eirp_dbm": None, "height_m": None, "azimuth_deg": None, "beamwidth_deg": None},
        **{"tilt_deg": None, "modelled": "eirp,height", "confidence": 1.0, "updated": None},
    }
    assert lookup.attribution == [
        {"source": "fcc_uls", "licence": "US-Gov-PD", "text": "Example fcc attribution."},
        {"source": "ised", "licence": "OGL-Canada-2.0", "text": "Example ised attribution."},
    ]
    capped = cell_sites.lookup_cells(43.6532, -79.3832, 50.0, max_rows=2)
    assert [row["site_id"] for row in capped.rows] == ["near", "mid"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """With a built UI, as deployed, so an unknown /api path ends in the catch-all's refusal."""
    (tmp_path / "dist" / "assets").mkdir(parents=True)
    (tmp_path / "dist" / "index.html").write_text("<!doctype html><div id=root></div>")
    monkeypatch.setenv("TOWER_FINDER_FRONTEND_DIST", str(tmp_path / "dist"))
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def test_cellular_status_counts_rows_per_country_and_source(client, tmp_path, monkeypatch):
    rows = [
        {"lat": 43.6532, "lon": -79.3832},
        {"lat": 45.4215, "lon": -75.6972},
        {"lat": -33.8688, "lon": 151.2093, **ACMA},
        {"lat": 40.7128, "lon": -74.0060, **FCC},
    ]
    meta = {**META, "source_date:ised": "2025-12-01", "attribution:acma": "Example acma attribution."}
    monkeypatch.setenv("TOWER_FINDER_CELL_DB", str(write_cell_db(tmp_path / "cells.sqlite", rows, meta)))
    monkeypatch.setenv("TOWER_FINDER_CELLULAR_ENABLED", "true")

    response = client.get("/api/cellular")
    assert response.status_code == 200
    assert response.json() == {
        **{"enabled": True, "status": "ok", "detail": None, "built_at": "2026-01-01T00:00:00Z"},
        "countries": {"us": 1, "ca": 2, "au": 1},
        "sources": {
            "acma": {"rows": 1, "licence": "ACMA-RRL", "source_date": None},
            "fcc_uls": {"rows": 1, "licence": "US-Gov-PD", "source_date": None},
            "ised": {"rows": 2, "licence": "OGL-Canada-2.0", "source_date": "2025-12-01"},
        },
        "attribution": [
            {"source": "acma", "licence": "ACMA-RRL", "text": "Example acma attribution."},
            {"source": "fcc_uls", "licence": "US-Gov-PD", "text": None},
            {"source": "ised", "licence": "OGL-Canada-2.0", "text": "Example ised attribution."},
        ],
    }


def test_with_the_flag_off_cellular_is_the_unknown_api_path_404(client, monkeypatch):
    monkeypatch.delenv("TOWER_FINDER_CELLULAR_ENABLED", raising=False)
    response, unknown = client.get("/api/cellular"), client.get("/api/nope")
    assert (response.status_code, response.content) == (404, b'{"detail":"Not Found"}')
    assert (response.headers, response.content) == (unknown.headers, unknown.content)


def test_startup_builds_the_index_in_the_background_while_health_answers(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOWER_FINDER_CELL_DB", str(write_cell_db(tmp_path / "cells.sqlite", [{"lat": 43.6, "lon": -79.4}]))
    )
    monkeypatch.setenv("TOWER_FINDER_CELLULAR_ENABLED", "true")
    monkeypatch.setattr(cell_sites, "_index", None)
    release, reads, read = threading.Event(), [], cell_sites._read_index
    monkeypatch.setattr(cell_sites, "_read_index", lambda *args: (reads.append(args), release.wait(10), read(*args))[2])
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200 and cell_sites._index is None
        release.set()
        next(t for t in threading.enumerate() if t.name == "cell-index").join(30)
        assert cell_sites._index is not None
        assert cell_sites.cell_candidates(43.6, -79.4, 10.0).rows.at.tolist() == [0]
    assert len(reads) == 1


def test_with_the_flag_off_startup_reads_no_cell_data(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TOWER_FINDER_CELL_DB", str(write_cell_db(tmp_path / "cells.sqlite", [{"lat": 43.6, "lon": -79.4}]))
    )
    monkeypatch.delenv("TOWER_FINDER_CELLULAR_ENABLED", raising=False)
    monkeypatch.setattr(cell_sites, "_index", None)
    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert not [t for t in threading.enumerate() if t.name == "cell-index"] and cell_sites._index is None
