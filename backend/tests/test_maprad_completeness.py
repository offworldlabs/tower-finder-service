"""scripts/maprad_completeness.py: the pure diff logic and the CLI, no network.

scripts/ is not a package and the image does not ship it, so the script is
loaded by path rather than imported.
"""

import argparse
import importlib.util
import io
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "maprad_completeness.py"
_spec = importlib.util.spec_from_file_location("maprad_completeness", _PATH)
mc = importlib.util.module_from_spec(_spec)
# Registered before exec: dataclasses resolves annotations through sys.modules.
sys.modules[_spec.name] = mc
_spec.loader.exec_module(mc)


def doc(callsign, eirp=None, freq_hz=None, site="Toronto"):
    """An index record shaped like the facet endpoint's (list-valued fields)."""
    d = {"i_site_name": [site]}
    if callsign is not None:
        d["i_callsign"] = [callsign]
    if eirp is not None:
        d["i_eirp"] = [eirp]
    if freq_hz is not None:
        d["i_frequency"] = [freq_hz]
    return d


# ── normalisation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CKFM-FM", "CKFM-FM"),
        ("CKFM-FM-AX1", "CKFM-FM"),
        ("CKFM-FM-AX2", "CKFM-FM"),
        ("CFMZ-HD", "CFMZ-FM"),
        ("CBLA-HD-AX1", "CBLA-FM"),
        ("CJKX-HD-2", "CJKX-FM-2"),
        ("CIMA-FM(TP)", "CIMA-FM"),
        (" ckfm-fm ", "CKFM-FM"),
        ("CFTO-DT", "CFTO-DT"),
        # Numbered rebroadcasters are their own stations.
        ("CJKX-FM-1", "CJKX-FM-1"),
        ("CHIN-1-FM", "CHIN-1-FM"),
        # Only whole segments: an -AX or -HD inside a longer segment is left alone.
        ("CHDX-FM", "CHDX-FM"),
        ("CKAXE-FM", "CKAXE-FM"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_callsign(raw, expected):
    assert mc.normalize_callsign(raw) == expected


def test_parse_doc_reads_list_fields_and_converts_hz():
    st = mc.parse_doc(doc("CKFM-FM-AX1", 45.58469, 99_900_000, "Toronto"))
    assert (st.base, st.callsign, st.eirp_dbw, st.freq_mhz, st.site) == (
        "CKFM-FM",
        "CKFM-FM-AX1",
        45.58469,
        99.9,
        "Toronto",
    )


def test_parse_doc_tolerates_scalars_missing_power_and_no_callsign():
    st = mc.parse_doc({"i_callsign": "CIUT-FM", "i_eirp": "n/a"})
    assert st.base == "CIUT-FM" and st.eirp_dbw is None and st.freq_mhz is None and st.site == ""
    assert mc.parse_doc(doc(None, 40.0)) is None
    assert mc.parse_doc({"i_callsign": []}) is None


def test_index_stations_keeps_the_strongest_record_per_station():
    stations, unnamed = mc.index_stations(
        [
            doc("CKFM-FM-AX1", 30.0, site="Aux"),
            doc("CKFM-FM", 45.6, site="Main"),
            doc("CKFM-HD", None, site="HD"),
            doc(None, 50.0),
        ]
    )
    assert unnamed == 1
    assert list(stations) == ["CKFM-FM"]
    ckfm = stations["CKFM-FM"]
    assert (ckfm.site, ckfm.eirp_dbw, ckfm.records) == ("Main", 45.6, 3)


def test_service_callsigns_counts_primary_and_shared():
    towers = [
        {"callsign": "CKFM-FM", "shared_callsigns": ["CFMZ-HD", "CHFI-FM-AX1"]},
        {"callsign": "", "shared_callsigns": None},
        {"callsign": "CFTO-DT"},
    ]
    assert mc.service_callsigns(towers) == {"CKFM-FM", "CFMZ-FM", "CHFI-FM", "CFTO-DT"}


# ── the diff and the gate ────────────────────────────────────────────────────


def test_diff_counts_and_orders_missing_strongest_first():
    docs = [
        doc("CKFM-FM", 45.6),
        doc("CKFM-FM-AX1", 20.0),
        doc("CIXL-FM", 47.0),
        doc("CHFI-FM", 44.0),
        doc("CIUT-FM", 41.8),
        doc("CJLO-FM", None),
        doc("CHES-FM", 10.0),
    ]
    d = mc.diff("FM", 9, docs, present={"CHFI-FM", "CIUT-FM"})
    assert (d.subtype, d.records, d.fetched, d.stations, d.present) == ("FM", 9, 7, 6, 2)
    assert [st.base for st in d.missing] == ["CIXL-FM", "CKFM-FM", "CHES-FM", "CJLO-FM"]


def test_diff_complete_when_service_has_everything():
    d = mc.diff("DTV", 2, [doc("CFTO-DT", 50.0), doc("CBLT-DT", 52.0)], present={"CFTO-DT", "CBLT-DT", "EXTRA"})
    assert d.missing == [] and d.present == d.stations == 2


def test_strong_missing_is_inclusive_and_ignores_unknown_power():
    missing = mc.diff("FM", 4, [doc("A-FM", 40.0), doc("B-FM", 39.99), doc("C-FM", None), doc("D-FM", 47.0)], set())
    assert [st.base for st in mc.strong_missing(missing.missing, 40.0)] == ["D-FM", "A-FM"]
    assert mc.strong_missing(missing.missing, 50.0) == []


def test_format_diff_marks_strong_and_notes_truncation():
    d = mc.diff("FM", 100, [doc("CKFM-FM", 45.6, 99_900_000), doc("CHES-FM", 10.0), doc(None)], set())
    lines = mc.format_diff(d, 40.0, show=1)
    assert "STRONG MISSING" in lines[0]
    assert any("only 3/100 records" in line for line in lines)
    assert any("1 records without a callsign" in line for line in lines)
    assert any(line.lstrip().startswith("! CKFM-FM") and "99.900 MHz" in line for line in lines)
    assert lines[-1].strip() == "... and 1 more"


# ── arguments ────────────────────────────────────────────────────────────────


def test_parse_args_defaults_and_presets():
    args = mc.parse_args(["toronto", "UBC", "45.5,-73.6"])
    assert [t.name for t in args.targets] == ["toronto", "ubc", "45.5000,-73.6000"]
    assert (args.targets[1].lat, args.targets[1].lon) == (49.26482627154048, -123.25016353304554)
    assert (args.targets[2].lat, args.targets[2].lon) == (45.5, -73.6)
    assert args.host == "https://towers.retina.fm"
    assert (args.radius, args.subtypes, args.strong_dbw, args.page_size) == (80, ["FM", "DTV"], 40.0, 30)


def test_parse_args_options():
    args = mc.parse_args(
        ["--host", "https://test-towers.retina.fm/", "--radius", "50", "--subtypes", "fm", "--strong-dbw", "35", "ubc"]
    )
    assert args.host == "https://test-towers.retina.fm"
    assert (args.radius, args.subtypes, args.strong_dbw) == (50, ["FM"], 35.0)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["atlantis"],
        ["43.6"],
        ["91,0"],
        ["toronto", "--radius", "0"],
        ["toronto", "--radius", "301"],
        ["toronto", "--page-size", "101"],
        ["toronto", "--subtypes", ","],
    ],
)
def test_parse_args_rejects(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        mc.parse_args(argv)
    assert exc.value.code == 2


def test_parse_target_messages_name_presets():
    with pytest.raises(argparse.ArgumentTypeError, match="toronto"):
        mc.parse_target("nowhere")


def test_index_url_repeats_geoterm_and_filters_subtype():
    q = urllib.parse.parse_qs(urllib.parse.urlparse(mc.index_url(43.65, -79.38, 80, "FM", 60, 30)).query)
    assert q["geoterm"] == ["43.65,-79.38", "80"]
    assert q["fts"] == ["dv_licence_subtype:e:FM"]
    assert (q["offset"], q["limit"], q["source"]) == (["60"], ["30"], ["CA"])


# ── main, with the network faked ─────────────────────────────────────────────


class FakeNet:
    """Stands in for fetch_json: canned service answers and paged index docs."""

    def __init__(self, service, index):
        self.service = service  # host-agnostic: one answer (or FetchError) for every target
        self.index = index  # subtype -> full doc list
        self.index_calls = []

    def __call__(self, url, timeout):
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/towers":
            if isinstance(self.service, Exception):
                raise self.service
            return self.service
        subtype = q["fts"][0].rsplit(":", 1)[1]
        offset, limit = int(q["offset"][0]), int(q["limit"][0])
        self.index_calls.append((subtype, offset))
        docs = self.index.get(subtype, [])
        return {"response": {"numFound": len(docs), "docs": docs[offset : offset + limit]}}


def run(monkeypatch, net, argv):
    monkeypatch.setattr(mc, "fetch_json", net)
    monkeypatch.setattr(mc.time, "sleep", lambda s: None)
    out = io.StringIO()
    code = mc.main(argv, out=out)
    return code, out.getvalue()


def _service(*callsigns):
    towers = [{"callsign": c, "shared_callsigns": []} for c in callsigns]
    return {"towers": towers, "query": {"source": "ca"}, "count": len(towers)}


def test_main_pages_the_index_and_exits_1_on_strong_missing(monkeypatch):
    fm = [doc(f"C{i:03d}-FM", 20.0) for i in range(65)] + [doc("CKFM-FM", 45.6)]
    net = FakeNet(_service(*[f"C{i:03d}-FM" for i in range(65)]), {"FM": fm, "DTV": [doc("CFTO-DT", 50.0)]})
    code, out = run(monkeypatch, net, ["toronto", "--subtypes", "FM,DTV"])
    assert code == 1
    assert [c for c in net.index_calls if c[0] == "FM"] == [("FM", 0), ("FM", 30), ("FM", 60)]
    assert "! CKFM-FM" in out and "strong stations missing at toronto" in out
    assert "! CFTO-DT" in out  # the DTV table is checked too


def test_main_exits_0_when_only_weak_stations_missing(monkeypatch):
    net = FakeNet(_service("CKFM-FM"), {"FM": [doc("CKFM-FM", 45.6), doc("CHES-FM", 10.0)]})
    code, out = run(monkeypatch, net, ["toronto", "--subtypes", "FM"])
    assert code == 0
    assert "CHES-FM" in out and "no missing station at or above" in out


@pytest.mark.parametrize(
    "status, detail",
    [(422, "Input should be a valid number"), (500, "MAPRAD_API_KEY not configured"), (502, "Maprad rejected")],
)
def test_main_reports_service_errors_and_continues(monkeypatch, status, detail):
    net = FakeNet(mc.FetchError(f"HTTP {status}: {detail}"), {"FM": [doc("CKFM-FM", 45.6)]})
    code, out = run(monkeypatch, net, ["toronto", "montreal", "--subtypes", "FM"])
    assert code == 2
    assert out.count(f"FAILED: service https://towers.retina.fm: HTTP {status}: {detail}") == 2
    assert "could not check toronto, montreal" in out
    assert net.index_calls == []  # no index traffic for a target the service failed


def test_strong_missing_outranks_a_failed_target(monkeypatch):
    calls = {"n": 0}
    good = FakeNet(_service(), {"FM": [doc("CKFM-FM", 45.6)]})

    def net(url, timeout):
        if "/api/towers" in url:
            calls["n"] += 1
            if calls["n"] == 2:
                raise mc.FetchError("HTTP 502: External service unavailable. Please try again.")
        return good(url, timeout)

    code, out = run(monkeypatch, net, ["toronto", "montreal", "--subtypes", "FM"])
    assert code == 1
    assert "strong stations missing at toronto" in out and "could not check montreal" in out


def test_fetch_json_turns_http_errors_into_the_detail(monkeypatch):
    import urllib.error

    body = json.dumps({"detail": "MAPRAD_API_KEY not configured"}).encode()

    def boom(req, timeout):
        assert req.get_header("User-agent", "").startswith("tower-finder-service/")
        raise urllib.error.HTTPError(req.full_url, 500, "err", {}, io.BytesIO(body))

    monkeypatch.setattr(mc.urllib.request, "urlopen", boom)
    with pytest.raises(mc.FetchError, match="HTTP 500: MAPRAD_API_KEY not configured"):
        mc.fetch_json("https://example.invalid/api/towers", 5)
