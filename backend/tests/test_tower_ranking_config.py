"""Additional tower_ranking tests — config validation, reload_config, parse_geom."""

import dis
import inspect
import json

import pytest
from services import tower_ranking
from tests._helpers import device as _device
from tests._helpers import system as _system


class TestReloadConfig:
    def test_reload_after_file_change(self, tmp_path, monkeypatch):
        """reload_config() picks up new values from tower_config.json."""
        cfg = {
            "receiver": {
                "rx_antenna_gain_dbi": 12.5,
                "sensitivity_dbm": -110.0,
            },
            "broadcast_bands": {
                "FM": [[88.0, 108.0]],
                "VHF": [[174.0, 216.0]],
            },
            "ranking": {
                "band_priority": {"VHF": 0, "FM": 1},
                "sort_order": [{"field": "band_priority", "ascending": True}],
            },
            "search": {
                "default_radius_km": 123,
                "default_limit": 7,
            },
        }

        original_path = tower_ranking._CONFIG_PATH
        original_gain = tower_ranking.RX_ANTENNA_GAIN_DBI

        fake_path = tmp_path / "tower_config.json"
        fake_path.write_text(json.dumps(cfg))
        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", fake_path)

        try:
            tower_ranking.reload_config()
            assert tower_ranking.RX_ANTENNA_GAIN_DBI == 12.5
            assert tower_ranking.SENSITIVITY_DBM == -110.0
            assert tower_ranking.DEFAULT_RADIUS_KM == 123
            assert tower_ranking.DEFAULT_LIMIT == 7
            assert tower_ranking.BAND_PRIORITY == {"VHF": 0, "FM": 1}
        finally:
            # Restore real config so downstream tests aren't broken
            monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", original_path)
            tower_ranking.reload_config()
            assert original_gain == tower_ranking.RX_ANTENNA_GAIN_DBI


class TestParseGeomEdgeCases:
    def test_point_well_formed(self):
        # WKT POINT is "lon lat", parse_geom returns (lat, lon)
        assert tower_ranking.parse_geom("POINT(151.2 -33.9)") == (-33.9, 151.2)

    def test_point_wrapped_dict(self):
        assert tower_ranking.parse_geom({"string": "POINT(10 20)"}) == (20.0, 10.0)

    def test_point_missing_paren(self):
        """Malformed WKT used to raise ValueError; now returns None."""
        assert tower_ranking.parse_geom("POINT 10 20") is None
        assert tower_ranking.parse_geom("POINT(10 20") is None

    def test_point_non_numeric(self):
        assert tower_ranking.parse_geom("POINT(x y)") is None

    def test_empty_inputs(self):
        assert tower_ranking.parse_geom(None) is None
        assert tower_ranking.parse_geom("") is None
        assert tower_ranking.parse_geom("   ") is None
        assert tower_ranking.parse_geom({}) is None
        assert tower_ranking.parse_geom(12345) is None

    def test_unknown_geometry(self):
        assert tower_ranking.parse_geom("LINESTRING(0 0, 1 1)") is None

    def test_polygon_centroid(self):
        wkt = "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))"
        result = tower_ranking.parse_geom(wkt)
        assert result is not None
        lat, lon = result
        # Centroid of unit square (with duplicated closing vertex) ≈ (4, 4)
        assert 3.0 <= lat <= 5.0
        assert 3.0 <= lon <= 5.0

    def test_multipolygon(self):
        wkt = "MULTIPOLYGON(((0 0, 2 0, 2 2, 0 2, 0 0)))"
        result = tower_ranking.parse_geom(wkt)
        assert result is not None


# ── Config validation ────────────────────────────────────────────────────────
#
# Ported from the monolith's tests of the same name, minus its fail-soft
# reload/health cases: reload_config() here raises on an unusable config rather
# than degrading to the shipped defaults (there is no health surface to report a
# degraded config on), so those tests describe behaviour this service does not
# have.


@pytest.fixture()
def restore_config():
    """Put every module-level setting back after a test that re-applies one."""
    saved = {name: getattr(tower_ranking, name) for name in tower_ranking.CONFIG_SETTINGS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(tower_ranking, name, value)


def _shipped_default() -> dict:
    """The config the image ships, as opposed to the runtime overlay."""
    with (tower_ranking._SOURCE_DEFAULT_DIR / "tower_config.json").open() as f:
        return json.load(f)


class TestConfigSettings:
    def test_lists_every_module_global_assigned_at_runtime(self):
        """A name missing here is module state that leaks between tests.

        Deliberately wider than the config: any global a function assigns
        outlives the test that triggered it, so anything new must be listed
        whether or not it is a setting.
        """
        assigned = {
            instruction.argval
            for obj in vars(tower_ranking).values()
            if inspect.isfunction(obj) and obj.__module__ == tower_ranking.__name__
            for instruction in dis.get_instructions(obj)
            if instruction.opname == "STORE_GLOBAL"
        }

        assert assigned, "no module globals found; the detection below has broken, not the list"
        assert assigned == set(tower_ranking.CONFIG_SETTINGS)


class TestValidateConfig:
    def test_shipped_default_is_valid(self):
        assert tower_ranking.validate_config(_shipped_default()) is None

    def test_empty_config_is_valid(self):
        # Every section is optional — apply_config() has a default for each.
        assert tower_ranking.validate_config({}) is None

    def test_non_object_section_rejected(self):
        assert tower_ranking.validate_config({"receiver": "6 dBi"}) is not None

    def test_non_object_config_rejected(self):
        assert tower_ranking.validate_config([]) is not None

    def test_broadcast_band_range_must_be_a_pair(self):
        assert tower_ranking.validate_config({"broadcast_bands": {"FM": [[88.0]]}}) is not None

    def test_broadcast_band_range_must_ascend(self):
        assert tower_ranking.validate_config({"broadcast_bands": {"FM": [[108.0, 87.8]]}}) is not None

    def test_sort_order_field_must_be_sortable(self):
        # _sort_key() negates a descending value, so a string field raises
        # TypeError on every search: the same "valid shape, breaks at request
        # time" fault as an unguarded NaN.
        cfg = {"ranking": {"sort_order": [{"field": "callsign", "ascending": False}]}}
        assert tower_ranking.validate_config(cfg) is not None

    def test_band_priority_values_must_be_numbers(self):
        # _sort_key() sorts on these against a literal 99 fallback, so a string
        # raises TypeError comparing int with str on any mixed-band search.
        cfg = {"ranking": {"band_priority": {"FM": "high"}}}
        assert tower_ranking.validate_config(cfg) is not None

    def test_band_priority_must_be_an_object(self):
        cfg = {"ranking": {"band_priority": ["VHF", "UHF", "FM"]}}
        assert "must be an object" in tower_ranking.validate_config(cfg)

    def test_band_priority_accepts_numbers(self):
        cfg = {"ranking": {"band_priority": {"FM": 0, "VHF": 1}}}
        assert tower_ranking.validate_config(cfg) is None

    def test_distance_priority_is_no_longer_sortable(self):
        # Towers no longer carry a distance class, so a rule naming it has
        # nothing to sort on. A PUT is rejected; an overlay already on disk is
        # migrated instead (TestReloadConfigValidates below).
        cfg = {"ranking": {"sort_order": [{"field": "distance_priority", "ascending": True}]}}
        assert "must be one of" in tower_ranking.validate_config(cfg)

    def test_legacy_distance_tables_are_ignored_not_rejected(self):
        # An overlay seeded before the classes were removed still carries these
        # sections. They feed nothing now, and rejecting them would fail the
        # load of every such overlay.
        cfg = {
            "ranking": {
                "distance_classes": [{"label": "Ideal", "min_km": 8, "max_km": 30}],
                "distance_priority": {"Ideal": 0},
            }
        }
        assert tower_ranking.validate_config(cfg) is None

    def test_default_limit_must_be_a_whole_number(self):
        # A slice bound: towers[:20.5] raises however positive 20.5 is.
        assert tower_ranking.validate_config({"search": {"default_limit": 20.5}}) is not None

    def test_default_radius_may_be_fractional(self):
        # Only ever compared against, so unlike the limit it needs no
        # integrality.
        assert tower_ranking.validate_config({"search": {"default_radius_km": 80.5}}) is None

    def test_sort_order_field_must_be_a_string(self):
        # An unhashable value makes the allowlist membership test raise, which
        # would turn the 400 this function exists to produce into a 500.
        cfg = {"ranking": {"sort_order": [{"field": []}]}}
        assert "must be a string" in tower_ranking.validate_config(cfg)

    def test_sort_order_rejects_an_unknown_field(self):
        cfg = {"ranking": {"sort_order": [{"field": "recieved_power_dbm"}]}}
        assert "must be one of" in tower_ranking.validate_config(cfg)

    def test_sort_order_ascending_must_be_a_bool(self):
        cfg = {"ranking": {"sort_order": [{"field": "distance_km", "ascending": "false"}]}}
        assert tower_ranking.validate_config(cfg) is not None

    def test_sort_order_accepts_the_shipped_fields(self):
        cfg = {"ranking": {"sort_order": _shipped_default()["ranking"]["sort_order"]}}
        assert tower_ranking.validate_config(cfg) is None

    def test_every_sortable_field_is_accepted(self):
        for field in tower_ranking._SORTABLE_FIELDS:
            cfg = {"ranking": {"sort_order": [{"field": field, "ascending": False}]}}
            assert tower_ranking.validate_config(cfg) is None, field

    def test_the_allowlist_is_the_union_of_both_engines(self):
        """The point of the reconcile: neither engine's fields may be dropped.

        Left column is the monolith's ranking vocabulary, right column this
        service's analyser-measurement one. A future edit that trims either
        side turns a live config into a 400 on the next PUT.
        """
        monolith = {
            "band_priority",
            "coverage_area_added_km2",
            "received_power_dbm",
            "distance_km",
            "bearing_deg",
            "frequency_mhz",
            "eirp_dbm",
            "frequency_matched",
            "latitude",
            "longitude",
        }
        measurement = {"score", "snr_db", "power_db", "obw_fraction", "measured"}
        assert tower_ranking._SORTABLE_FIELDS == frozenset(monolith | measurement)

    def test_nan_rejected(self):
        # json.loads accepts the bare NaN literal, and every comparison against
        # NaN is False, so an unguarded NaN slips past each range check and is
        # persisted. DEFAULT_LIMIT = nan then makes every search raise TypeError.
        cfg = json.loads('{"search": {"default_limit": NaN}}')
        assert tower_ranking.validate_config(cfg) is not None

    def test_infinity_rejected(self):
        cfg = json.loads('{"receiver": {"sensitivity_dbm": -Infinity}}')
        assert tower_ranking.validate_config(cfg) is not None

    def test_nan_band_priority_rejected(self):
        cfg = json.loads('{"ranking": {"band_priority": {"FM": NaN}}}')
        assert tower_ranking.validate_config(cfg) is not None

    def test_bool_is_not_a_number(self):
        assert tower_ranking.validate_config({"receiver": {"rx_antenna_gain_dbi": True}}) is not None


class TestApplyConfig:
    def test_raises_on_a_shape_it_cannot_apply(self):
        """PUT /api/config depends on this raising, to reject the write."""
        with pytest.raises(TypeError):
            tower_ranking.apply_config({"broadcast_bands": {"FM": 5}})

    def test_failed_apply_leaves_the_previous_config_intact(self):
        before = (tower_ranking.RX_ANTENNA_GAIN_DBI, dict(tower_ranking.BROADCAST_BANDS))

        with pytest.raises(TypeError):
            tower_ranking.apply_config(
                {
                    "receiver": {"rx_antenna_gain_dbi": 99.0},
                    "broadcast_bands": {"FM": 5},
                }
            )

        # The receiver gain is read before the bands are built, so an apply
        # that assigned as it went would have taken 99.0 on its way out.
        assert before == (tower_ranking.RX_ANTENNA_GAIN_DBI, tower_ranking.BROADCAST_BANDS)

    def test_applied_config_does_not_alias_the_caller(self, restore_config):
        """PUT /api/config applies the parsed request body itself.

        Assigning the objects nested inside it would leave live ranking state
        aliasing that body, for anything the handler does to it afterwards to
        rewrite the settings a search in flight is reading.
        """
        body = {
            "ranking": {
                "band_priority": {"FM": 0},
                "sort_order": [{"field": "distance_km", "ascending": True}],
            }
        }

        tower_ranking.apply_config(body)

        ranking = body["ranking"]
        ranking["band_priority"]["FM"] = 99
        ranking["sort_order"][0]["ascending"] = False
        ranking["sort_order"].append({"field": "eirp_dbm", "ascending": False})

        assert tower_ranking.BAND_PRIORITY == {"FM": 0}
        assert tower_ranking.SORT_ORDER == [{"field": "distance_km", "ascending": True}]

    def test_default_sort_order_matches_the_shipped_file(self, restore_config):
        """A config naming no sort_order ranks the same way as a fresh overlay.

        The monolith leads its own fallback with coverage_area_added_km2;
        adopting that here would silently re-rank every deployment whose config
        omits the section.
        """
        tower_ranking.apply_config({})

        assert tower_ranking.SORT_ORDER == _shipped_default()["ranking"]["sort_order"]
        assert tower_ranking.BAND_PRIORITY == _shipped_default()["ranking"]["band_priority"]

    def test_shipped_default_ranks_by_band_then_score_then_power(self):
        """Band tier first, VHF and UHF tied; then measured score, then
        modelled received power for towers without a measurement."""
        ranking = _shipped_default()["ranking"]
        assert ranking["sort_order"] == [
            {"field": "band_priority", "ascending": True},
            {"field": "score", "ascending": False},
            {"field": "received_power_dbm", "ascending": False},
        ]
        assert ranking["band_priority"]["VHF"] == ranking["band_priority"]["UHF"]
        assert ranking["band_priority"]["FM"] > ranking["band_priority"]["UHF"]
        assert "distance_classes" not in ranking
        assert "distance_priority" not in ranking


class TestShippedRanking:
    """What the shipped config ranks on, run through process_and_rank.

    Every tower sits at the same spot ~20 km north of the receiver, so
    received power is decided by EIRP alone and distance cannot leak into the
    order.
    """

    _LAT, _LON = 33.749, -84.388

    @pytest.fixture(autouse=True)
    def _shipped(self, restore_config):
        tower_ranking.apply_config(_shipped_default())

    def _rank(self, devices):
        return tower_ranking.process_and_rank([_system(devices)], self._LAT, self._LON)

    def test_tv_towers_rank_by_power_across_vhf_and_uhf(self):
        # EIRP steps of 20 dB, well clear of the ~9 dB extra path loss UHF
        # pays over VHF at the same distance, so the intended order is also
        # the received-power order.
        devices = [
            _device(freq_mhz=185.0, lat=33.93, lon=-84.388, callsign="VHF_WEAK", eirp=1.0),
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="UHF_STRONG", eirp=1_000_000.0),
            _device(freq_mhz=195.0, lat=33.93, lon=-84.388, callsign="VHF_STRONG", eirp=10_000.0),
            _device(freq_mhz=545.0, lat=33.93, lon=-84.388, callsign="UHF_WEAK", eirp=100.0),
        ]
        towers = self._rank(devices)
        # UHF and VHF interleave on power: neither band outranks the other.
        assert [t["callsign"] for t in towers] == ["UHF_STRONG", "VHF_STRONG", "UHF_WEAK", "VHF_WEAK"]
        powers = [t["received_power_dbm"] for t in towers]
        assert powers == sorted(powers, reverse=True)

    def test_fm_ranks_after_every_tv_tower_whatever_its_power(self):
        devices = [
            _device(freq_mhz=95.5, lat=33.93, lon=-84.388, callsign="FM_HUGE", eirp=1_000_000.0),
            _device(freq_mhz=185.0, lat=33.93, lon=-84.388, callsign="VHF_TINY", eirp=10.0),
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="UHF_TINY", eirp=10.0),
        ]
        towers = self._rank(devices)
        assert towers[-1]["callsign"] == "FM_HUGE"
        assert {t["callsign"] for t in towers[:2]} == {"VHF_TINY", "UHF_TINY"}
        # Not a power tie-break: the FM tower is received far louder and still loses.
        assert towers[-1]["received_power_dbm"] > max(t["received_power_dbm"] for t in towers[:2])

    def test_measured_score_outranks_modelled_power(self):
        # POST /api/towers: the SDR heard the weak tower better than the
        # strong one (a hill, say). Its score wins over the FSPL prediction.
        devices = [
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="UHF_STRONG", eirp=1_000_000.0),
            _device(freq_mhz=545.0, lat=33.93, lon=-84.388, callsign="UHF_WEAK", eirp=100.0),
        ]
        measurements = [
            {"freq_mhz": 515.0, "band": "UHF", "score": 0.4, "snr_db": None, "obw_fraction": None, "power_db": -60.0},
            {"freq_mhz": 545.0, "band": "UHF", "score": 0.9, "snr_db": None, "obw_fraction": None, "power_db": -40.0},
        ]
        towers = tower_ranking.process_and_rank([_system(devices)], self._LAT, self._LON, measurements=measurements)
        assert [t["callsign"] for t in towers] == ["UHF_WEAK", "UHF_STRONG"]
        assert towers[0]["received_power_dbm"] < towers[1]["received_power_dbm"]

    def test_score_never_lifts_fm_above_tv(self):
        devices = [
            _device(freq_mhz=95.5, lat=33.93, lon=-84.388, callsign="FM", eirp=100_000.0),
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="UHF", eirp=100.0),
        ]
        measurements = [
            {"freq_mhz": 95.5, "band": "FM", "score": 1.0, "snr_db": 50.0, "obw_fraction": 0.5, "power_db": None},
            {"freq_mhz": 515.0, "band": "UHF", "score": 0.1, "snr_db": None, "obw_fraction": None, "power_db": -70.0},
        ]
        towers = tower_ranking.process_and_rank([_system(devices)], self._LAT, self._LON, measurements=measurements)
        assert [t["callsign"] for t in towers] == ["UHF", "FM"]

    def test_distance_no_longer_decides_the_order(self):
        # Under the old classes an 80 km tower was "Far" and a 20 km one
        # "Ideal", and the class outranked power. Now only power counts, so
        # the far tower wins when it is strong enough to be received louder.
        devices = [
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="NEAR_WEAK", eirp=10.0),
            _device(freq_mhz=545.0, lat=34.45, lon=-84.388, callsign="FAR_STRONG", eirp=1_000_000.0),
        ]
        towers = tower_ranking.process_and_rank([_system(devices)], self._LAT, self._LON)
        assert [t["callsign"] for t in towers] == ["FAR_STRONG", "NEAR_WEAK"]
        assert towers[0]["distance_km"] > towers[1]["distance_km"]
        assert "distance_class" not in towers[0]


class TestReloadConfigValidates:
    """Validation cannot be a write-time gate only.

    The overlay is a mounted volume, so the configs that matter most are the
    ones edited by hand inside it, which never pass through the endpoint. One
    that applies without raising and then breaks every search must be caught on
    load, loudly — this service has no health surface on which a silent
    fallback could be noticed.
    """

    def test_invalid_config_on_disk_is_rejected_on_load(self, tmp_path, monkeypatch, restore_config):
        path = tmp_path / "tower_config.json"
        path.write_text(json.dumps({"ranking": {"sort_order": [{"field": "callsign", "ascending": False}]}}))
        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", path)

        with pytest.raises(ValueError, match="callsign"):
            tower_ranking.reload_config()

    def test_valid_config_on_disk_applies(self, tmp_path, monkeypatch, restore_config):
        path = tmp_path / "tower_config.json"
        path.write_text(json.dumps({"search": {"default_limit": 5}}))
        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", path)

        tower_ranking.reload_config()

        assert tower_ranking.DEFAULT_LIMIT == 5

    def test_legacy_overlay_sorting_on_distance_priority_loads(self, tmp_path, monkeypatch, restore_config, caplog):
        """The default that shipped until 2026-05-28, as a seeded overlay would hold it.

        The overlay volume is never re-seeded, so this is what an environment
        nobody has PUT a config to still has on disk. It must boot: the rule is
        dropped with a warning and the rest of the config applies. The file is
        left alone — a PUT of this body is still rejected.
        """
        legacy = {
            "ranking": {
                "band_priority": {"VHF": 0, "UHF": 1, "FM": 2},
                "distance_classes": [
                    {"label": "Too Close", "min_km": 0, "max_km": 8},
                    {"label": "Ideal", "min_km": 8, "max_km": 30},
                    {"label": "Good", "min_km": 30, "max_km": 60},
                    {"label": "Far", "min_km": 60, "max_km": None},
                ],
                "distance_priority": {"Ideal": 0, "Good": 1, "Far": 2, "Too Close": 3},
                "sort_order": [
                    {"field": "band_priority", "ascending": True},
                    {"field": "distance_priority", "ascending": True},
                    {"field": "received_power_dbm", "ascending": False},
                ],
            },
            "search": {"default_limit": 9},
        }
        path = tmp_path / "tower_config.json"
        path.write_text(json.dumps(legacy))
        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", path)

        with caplog.at_level("WARNING", logger=tower_ranking.__name__):
            tower_ranking.reload_config()

        assert tower_ranking.SORT_ORDER == [
            {"field": "band_priority", "ascending": True},
            {"field": "received_power_dbm", "ascending": False},
        ]
        assert tower_ranking.BAND_PRIORITY == {"VHF": 0, "UHF": 1, "FM": 2}
        assert tower_ranking.DEFAULT_LIMIT == 9
        assert "distance_priority" in caplog.text
        assert json.loads(path.read_text()) == legacy, "the file on disk is not rewritten"
        assert tower_ranking.validate_config(legacy) is not None, "the same body is still refused by PUT"

    def test_legacy_migration_is_silent_when_nothing_to_drop(self, tmp_path, monkeypatch, restore_config, caplog):
        path = tmp_path / "tower_config.json"
        path.write_text(json.dumps(_shipped_default()))
        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", path)

        with caplog.at_level("WARNING", logger=tower_ranking.__name__):
            tower_ranking.reload_config()

        assert "distance_priority" not in caplog.text
