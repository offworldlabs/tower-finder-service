"""The cell site database builder and its four sources (ISED, ACMA, FCC ULS, OpenCelliD), on made-up records."""

import collections
import importlib
import importlib.util
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "scripts"))
_spec = importlib.util.spec_from_file_location("build_cell_db", _REPO / "scripts" / "build_cell_db.py")
bcd = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bcd
_spec.loader.exec_module(bcd)
ised = importlib.import_module("cell_sources.ised")
acma = importlib.import_module("cell_sources.acma")
fcc = importlib.import_module("cell_sources.fcc")
ocid = importlib.import_module("cell_sources.opencellid")
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cell_sources"


def _row(freq):
    row = dict.fromkeys(bcd.COLUMNS)
    row.update(country="ca", source="ised", licence="OGL-Canada-2.0", site_id="S1", lat=43.65, lon=-79.38)
    row.update(freq_mhz=freq, modelled="", confidence=0.9)
    return row


def test_missing_source_is_skipped_and_named_in_counts(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ised.zip").write_bytes(b"x")
    present = SimpleNamespace(
        SOURCE="ised",
        LICENCE="OGL-Canada-2.0",
        ATTRIBUTION="Contains data under the Open Government Licence - Canada 2.0",
        input_paths=lambda r: [raw / "ised.zip"],
        source_date=lambda r: "2026-01-02",
        iter_rows=lambda r, stats: iter([_row(881.5), _row(1960.0), _row(1000.0)]),
    )
    ready = raw / "opencellid" / "READY"
    absent = SimpleNamespace(SOURCE="opencellid", LICENCE="CC-BY-SA-4.0", input_paths=lambda r: [ready])
    out = tmp_path / "cells.sqlite"
    counts = bcd.build(raw, out, sources=[present, absent])
    con = sqlite3.connect(out)
    meta = dict(con.execute("SELECT key, value FROM meta"))
    assert con.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == 2
    assert "dropped outside cellular bands: 1" in counts
    assert f"opencellid: missing {ready}" in counts
    assert "US: FCC-only (OpenCelliD files not ready)" in counts
    assert meta["schema_version"] == "1" and "attribution:ised" in meta
    assert "attribution:opencellid" not in meta
    assert not Path(f"{out}.tmp").exists()


def test_ised_rows_are_cellular_transmitters_without_contact_data(tmp_path):
    (tmp_path / "ised").mkdir()
    with zipfile.ZipFile(tmp_path / "ised" / "Site_Data_Extract_FX.zip", "w") as zf:
        zf.write(_FIXTURES / "ised" / "Site_Data_Extract_FX.csv", "Site_Data_Extract_FX.csv")
    stats = collections.Counter()
    lte, nr = sorted(ised.iter_rows(tmp_path, stats), key=lambda r: r["radio"])
    assert (lte["radio"], lte["freq_mhz"], lte["bandwidth_mhz"], lte["eirp_dbm"]) == ("LTE", 1967.5, 20.0, 63.0)
    assert (lte["azimuth_deg"], lte["beamwidth_deg"], lte["tilt_deg"], lte["height_m"]) == (135.0, 65.0, 4.0, 35.0)
    assert lte["site_id"] == "100001:43.65340,-79.38410" and lte["cell_id"] == "302610-12345-123456789"
    assert (lte["operator"], lte["updated"], lte["modelled"]) == ("Example Mobile Inc.", "2025-06-30", "")
    assert (nr["radio"], nr["freq_mhz"], nr["eirp_dbm"], nr["azimuth_deg"]) == ("NR", 3500.0, 73.8, 255.0)
    assert (nr["tilt_deg"], stats["superseded by a newer upload"], stats["non-cellular technology"]) == (3.0, 2, 1)
    values = [str(v) for row in (lte, nr) for v in row.values()]
    assert not {"Jane Example", "5550100", "jane@example.com"} & set(values)
    assert not any("@" in v for v in values)


def test_acma_keeps_carrier_transmitters_and_hides_natural_persons(tmp_path):
    fixtures = _FIXTURES / "acma"
    zip_path = tmp_path / "acma" / "spectra_rrl.zip"
    zip_path.parent.mkdir()
    with zipfile.ZipFile(zip_path, "w") as zf:
        for csv_file in sorted(fixtures.glob("*.csv")):
            zf.write(csv_file, csv_file.name)
    stats = collections.Counter()

    rows = list(acma.iter_rows(tmp_path, stats))

    keys = ("site_id", "cell_id", "operator", "lat", "lon", "freq_mhz", "bandwidth_mhz", "eirp_dbm", "height_m")
    assert [tuple(row[k] for k in keys) for row in rows] == [
        ("100", "1001", "Example Telco Pty Ltd", -33.87, 151.21, 778.0, 10.0, 60.0, 30.5),
        ("200", "EFL2001", None, -33.88, 151.2, 2140.0, 5.0, 60.01, 30.0),
    ]
    keys = ("azimuth_deg", "beamwidth_deg", "tilt_deg", "modelled", "updated", "country", "radio", "confidence")
    assert [tuple(row[k] for k in keys) for row in rows] == [
        (120.0, 65.0, 2.0, "", "2024-05-06", "au", None, 0.85),
        (None, None, None, "bandwidth,height", "2021-03-04", "au", None, 0.85),
    ]
    assert "Jane Example" not in repr(rows)
    assert stats["operator nulled: natural person"] == 1
    assert (stats["receiver"], stats["not a carrier base station"]) == (1, 1)


def test_acma_azimuth_0_on_an_antenna_without_beamwidth_is_unknown_unless_it_is_a_sector(tmp_path):
    # Antenna 20 has no beamwidth. At site 200 the licence also points it at 120, so its 0 there is a real sector.
    devices = (("1005", "100", "0"), ("1006", "200", "0"), ("1007", "200", "120"))
    extra = "".join(
        f"L1,{c},,2024-05-06,778000000,10000000,T,40,W,{s},20,{a},30.5,2,1,1000,W,\n" for c, s, a in devices
    )
    (tmp_path / "acma").mkdir()
    with zipfile.ZipFile(tmp_path / "acma" / "spectra_rrl.zip", "w") as zf:
        for table in (_FIXTURES / "acma").glob("*.csv"):
            zf.writestr(table.name, table.read_text() + (extra if table.name == "device_details.csv" else ""))
    stats = collections.Counter()
    azimuths = {row["cell_id"]: row["azimuth_deg"] for row in acma.iter_rows(tmp_path, stats)}
    assert (azimuths["1005"], azimuths["1006"], azimuths["1007"]) == (None, 0.0, 120.0)
    assert stats["azimuth nulled: placeholder 0"] == 1


def test_fcc_cellular_rows_are_active_downlink_transmitters(tmp_path):
    (tmp_path / "fcc").mkdir()
    with zipfile.ZipFile(tmp_path / "fcc" / "l_cell.zip", "w") as zf:
        zf.writestr("counts", "File Creation Date: Sun Nov 15 09:39:36 EST 2026\n")
        for table in sorted((_FIXTURES / "fcc").glob("*.dat")):
            zf.write(table, table.name)
    stats = collections.Counter()
    rows = list(fcc.iter_rows(tmp_path, stats))
    assert rows == [
        {
            "country": "us",
            "source": "fcc_uls",
            "licence": "US-Gov-PD",
            "site_id": "KZZ901:1",
            "cell_id": "1:1",
            "operator": "Example Cellular LLC",
            "radio": None,
            "lat": 39.75,
            "lon": -104.99,
            "freq_mhz": 874.515,
            "bandwidth_mhz": 10.95,
            "eirp_dbm": 52.15,
            "height_m": 40.0,
            "azimuth_deg": 120.0,
            "beamwidth_deg": 65.0,
            "tilt_deg": None,
            "modelled": "",
            "confidence": 0.6,
            "updated": "2025-03-04",
        }
    ]
    assert stats == collections.Counter({"inactive licence": 1, "uplink frequency": 1})
    assert fcc.source_date(tmp_path) == "2026-11-15"


def test_opencellid_cells_cluster_into_sites_snapped_to_asr(tmp_path):
    (tmp_path / "opencellid").mkdir()
    (tmp_path / "opencellid" / "READY").write_text("")
    shutil.copy(_FIXTURES / "opencellid" / "cells.csv", tmp_path / "opencellid" / "cells.csv")
    (tmp_path / "fcc").mkdir()
    with zipfile.ZipFile(tmp_path / "fcc" / "r_tower.zip", "w") as zf:
        for member in ("RA.dat", "CO.dat"):
            zf.write(_FIXTURES / "opencellid" / member, member)
    stats = collections.Counter()
    rows = {row["site_id"]: row for row in ocid.iter_rows(tmp_path, stats)}
    assert sorted(rows) == ["LTE:310-260-200000", "LTE:310-410-100000"]
    snapped, estimated = rows["LTE:310-410-100000"], rows["LTE:310-260-200000"]
    assert (snapped["lat"], snapped["lon"], snapped["height_m"]) == (39.74, -104.99, 45.0)
    assert snapped["modelled"] == "freq,bandwidth,eirp,height"
    assert (snapped["confidence"], snapped["operator"], snapped["updated"]) == (0.4, "AT&T", "2025-11-01")
    assert (estimated["lat"], estimated["lon"], estimated["height_m"]) == (39.7805, -104.9005, 30.0)
    assert estimated["modelled"] == "freq,bandwidth,eirp,height,position"
    assert (estimated["confidence"], estimated["operator"]) == (0.25, "T-Mobile")
    assert (snapped["freq_mhz"], snapped["bandwidth_mhz"], snapped["eirp_dbm"]) == (2132.5, 10.0, 64.0)
    assert stats["not LTE/NR"] == 1 and stats["not a US MCC"] == 1


def _built(raw, query):
    counts = bcd.build(raw, raw / "cells.sqlite")
    return counts, sqlite3.connect(raw / "cells.sqlite").execute(query).fetchall()


def test_ised_implausible_gain_or_eirp_gets_the_modelled_eirp(tmp_path):
    header, _, lte, _, nr, _ = (_FIXTURES / "ised" / "Site_Data_Extract_FX.csv").read_text("utf-8-sig").splitlines()
    gsm = lte.replace('"LTE"', '"GSM"').replace('"1967.5","1887.5"', '"881.6","836.6"')
    gsm = gsm.replace('"20","20M0G7W","46"', '"9.2","9M20G7W","51"')
    records = [header, lte.replace('"17.5","17.5"', '"46","46"'), nr.replace('"49"', '"59"'), gsm]
    (tmp_path / "ised").mkdir()
    with zipfile.ZipFile(tmp_path / "ised" / "Site_Data_Extract_FX.zip", "w") as zf:
        zf.writestr("Site_Data_Extract_FX.csv", "\n".join(records))
    counts, rows = _built(tmp_path, "SELECT radio, bandwidth_mhz, eirp_dbm, modelled FROM cells ORDER BY radio")
    assert rows == [("GSM", 0.2, 61.0, "bandwidth,eirp"), ("LTE", 20.0, 67.02, "eirp"), ("NR", 50.0, 71.0, "eirp")]
    assert "antenna gain implausible, ignored: 1" in counts and "eirp implausible, ignored: 2" in counts


def test_row_wider_than_one_carrier_is_cut_to_one_carrier(tmp_path):
    wide = "L1,1004,,2024-05-06,2140000000,60000000,T,40,W,100,10,240,30.5,2,1,1000,W,\n"
    (tmp_path / "acma").mkdir()
    with zipfile.ZipFile(tmp_path / "acma" / "spectra_rrl.zip", "w") as zf:
        for table in (_FIXTURES / "acma").glob("*.csv"):
            zf.writestr(table.name, table.read_text() + (wide if table.name == "device_details.csv" else ""))
    header, _, lte = (_FIXTURES / "ised" / "Site_Data_Extract_FX.csv").read_text("utf-8-sig").splitlines()[:3]
    gsm = lte.replace('"LTE"', '"GSM"').replace('"1967.5","1887.5"', '"881.6","836.6"')
    gsm = gsm.replace('"20","20M0G7W","46"', '"9.2","9M20G7W","41.6"')
    (tmp_path / "ised").mkdir()
    with zipfile.ZipFile(tmp_path / "ised" / "Site_Data_Extract_FX.zip", "w") as zf:
        zf.writestr("Site_Data_Extract_FX.csv", f"{header}\n{gsm}")
    where = "cell_id IN ('1004', '302610-12345-123456789') ORDER BY source"
    counts, rows = _built(tmp_path, f"SELECT source, bandwidth_mhz, eirp_dbm, modelled FROM cells WHERE {where}")
    assert rows == [("acma", 20.0, 55.23, "bandwidth,eirp"), ("ised", 0.2, 58.6, "bandwidth")]
    assert counts.count("wider than one carrier, reduced: 1") == 2


def test_opencellid_structure_under_3_m_gets_the_default_height(tmp_path):
    (tmp_path / "opencellid").mkdir()
    (tmp_path / "opencellid" / "READY").write_text("")
    shutil.copy(_FIXTURES / "opencellid" / "cells.csv", tmp_path / "opencellid" / "cells.csv")
    (tmp_path / "fcc").mkdir()
    with zipfile.ZipFile(tmp_path / "fcc" / "r_tower.zip", "w") as zf:
        zf.writestr("RA.dat", (_FIXTURES / "opencellid" / "RA.dat").read_text().replace("|45.0|", "|2.5|"))
        zf.write(_FIXTURES / "opencellid" / "CO.dat", "CO.dat")
    counts, rows = _built(tmp_path, "SELECT lat, lon, height_m, modelled FROM cells WHERE operator = 'AT&T'")
    assert rows == [(39.74, -104.99, 30.0, "freq,bandwidth,eirp,height")]
    assert "ASR height under 3 m, defaulted: 1" in counts
