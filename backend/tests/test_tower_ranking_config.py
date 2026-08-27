"""Additional tower_ranking tests — config validation, reload_config, parse_geom."""

import dis
import inspect
import json

import pytest
from services import tower_ranking


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
                "distance_classes": [
                    {"label": "near", "min_km": 0, "max_km": 10},
                    {"label": "far", "min_km": 10, "max_km": None},
                ],
                "distance_priority": {"near": 0, "far": 1},
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
            # "far" has max_km=None → converted to inf
            far = next(dc for dc in tower_ranking.DISTANCE_CLASSES if dc[0] == "far")
            assert far[2] == float("inf")
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

    def test_distance_class_missing_max_km_rejected(self):
        cfg = {"ranking": {"distance_classes": [{"label": "Ideal", "min_km": 8}]}}
        assert "max_km" in tower_ranking.validate_config(cfg)

    def test_open_ended_distance_class_accepted(self):
        # A null max_km is the final open-ended class; apply maps it to inf.
        cfg = {"ranking": {"distance_classes": [{"label": "Far", "min_km": 60, "max_km": None}]}}
        assert tower_ranking.validate_config(cfg) is None

    def test_non_numeric_min_km_rejected(self):
        cfg = {"ranking": {"distance_classes": [{"label": "Ideal", "min_km": "eight", "max_km": 30}]}}
        assert tower_ranking.validate_config(cfg) is not None

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

    def test_distance_priority_values_must_be_numbers(self):
        cfg = {"ranking": {"distance_priority": {"Ideal": "x"}}}
        assert tower_ranking.validate_config(cfg) is not None

    def test_priority_tables_accept_numbers(self):
        cfg = {"ranking": {"band_priority": {"FM": 0, "VHF": 1}, "distance_priority": {"Ideal": 0}}}
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
            "distance_priority",
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

    def test_nan_distance_bound_rejected(self):
        # max_km <= min_km is False when either side is NaN, so the ordering
        # check cannot catch this one on its own.
        cfg = json.loads('{"ranking": {"distance_classes": [{"label": "A", "min_km": NaN, "max_km": 10}]}}')
        assert tower_ranking.validate_config(cfg) is not None

    def test_bool_is_not_a_number(self):
        assert tower_ranking.validate_config({"receiver": {"rx_antenna_gain_dbi": True}}) is not None


class TestApplyConfig:
    def test_raises_on_a_shape_it_cannot_apply(self):
        """PUT /api/config depends on this raising, to reject the write."""
        with pytest.raises(KeyError):
            tower_ranking.apply_config({"ranking": {"distance_classes": [{"label": "A", "min_km": 8}]}})

    def test_failed_apply_leaves_the_previous_config_intact(self):
        before = (tower_ranking.RX_ANTENNA_GAIN_DBI, list(tower_ranking.DISTANCE_CLASSES))

        with pytest.raises(KeyError):
            tower_ranking.apply_config(
                {
                    "receiver": {"rx_antenna_gain_dbi": 99.0},
                    "ranking": {"distance_classes": [{"label": "A", "min_km": 8}]},
                }
            )

        # The receiver gain is read before the distance classes are built, so an
        # apply that assigned as it went would have taken 99.0 on its way out.
        assert before == (tower_ranking.RX_ANTENNA_GAIN_DBI, tower_ranking.DISTANCE_CLASSES)

    def test_applied_config_does_not_alias_the_caller(self, restore_config):
        """PUT /api/config applies the parsed request body itself.

        Assigning the objects nested inside it would leave live ranking state
        aliasing that body, for anything the handler does to it afterwards to
        rewrite the settings a search in flight is reading.
        """
        body = {
            "ranking": {
                "band_priority": {"FM": 0},
                "distance_priority": {"Ideal": 0},
                "sort_order": [{"field": "distance_km", "ascending": True}],
            }
        }

        tower_ranking.apply_config(body)

        ranking = body["ranking"]
        ranking["band_priority"]["FM"] = 99
        ranking["distance_priority"]["Ideal"] = 99
        ranking["sort_order"][0]["ascending"] = False
        ranking["sort_order"].append({"field": "eirp_dbm", "ascending": False})

        assert tower_ranking.BAND_PRIORITY == {"FM": 0}
        assert tower_ranking.DISTANCE_PRIORITY == {"Ideal": 0}
        assert tower_ranking.SORT_ORDER == [{"field": "distance_km", "ascending": True}]

    def test_default_sort_order_is_unchanged_by_the_coverage_port(self, restore_config):
        """A config naming no sort_order must rank exactly as it did before.

        The monolith leads its own fallback with coverage_area_added_km2;
        adopting that here would silently re-rank every deployment whose config
        omits the section.
        """
        tower_ranking.apply_config({})

        assert tower_ranking.SORT_ORDER == [
            {"field": "band_priority", "ascending": True},
            {"field": "distance_priority", "ascending": True},
            {"field": "received_power_dbm", "ascending": False},
        ]

    def test_shipped_default_sort_order_is_unchanged(self):
        """The file the image ships still ranks band first, analyser score second."""
        assert _shipped_default()["ranking"]["sort_order"] == [
            {"field": "band_priority", "ascending": True},
            {"field": "score", "ascending": False},
        ]


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
