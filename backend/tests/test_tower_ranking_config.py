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
        # The detection-area model's own fields, which the shipped sort_order
        # now leads with. Dropping one of these turns the shipped config itself
        # into a 400 on the next PUT.
        model = {"expected_area_km2", "best_azimuth_deg", "horizon_km"}
        assert tower_ranking._SORTABLE_FIELDS == frozenset(monolith | measurement | model)

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


class TestValidateScoringSection:
    """The scoring knobs feed logs, divisors and an array bound.

    Each of these passes every structural check and then raises (or allocates
    for ever) inside numpy on the next search, which is the same "valid shape,
    breaks at request time" fault the rest of this validator exists to catch.
    """

    def test_absent_section_is_valid(self):
        assert tower_ranking.validate_config({}) is None

    def test_empty_section_is_valid(self):
        assert tower_ranking.validate_config({"scoring": {}}) is None

    def test_section_must_be_an_object(self):
        assert "must be an object" in tower_ranking.validate_config({"scoring": [1, 2]})

    def test_shipped_scoring_section_is_valid(self):
        assert tower_ranking.validate_config({"scoring": _shipped_default()["scoring"]}) is None

    @pytest.mark.parametrize("key", tower_ranking._SCORING_NUMBER_KEYS)
    def test_number_keys_reject_a_string(self, key):
        assert tower_ranking.validate_config({"scoring": {key: "lots"}}) is not None

    @pytest.mark.parametrize("key", tower_ranking._SCORING_POSITIVE_KEYS)
    def test_positive_keys_reject_zero_and_negatives(self, key):
        assert tower_ranking.validate_config({"scoring": {key: 0}}) is not None
        assert tower_ranking.validate_config({"scoring": {key: -1}}) is not None

    def test_nan_is_rejected_here_too(self):
        cfg = json.loads('{"scoring": {"grid_km": NaN}}')
        assert tower_ranking.validate_config(cfg) is not None

    def test_bistatic_angle_must_be_a_real_angle(self):
        assert tower_ranking.validate_config({"scoring": {"max_bistatic_angle_deg": 0}}) is not None
        assert tower_ranking.validate_config({"scoring": {"max_bistatic_angle_deg": 181}}) is not None
        assert tower_ranking.validate_config({"scoring": {"max_bistatic_angle_deg": 150}}) is None

    def test_n_azimuths_must_be_a_positive_whole_number(self):
        assert tower_ranking.validate_config({"scoring": {"n_azimuths": 12.5}}) is not None
        assert tower_ranking.validate_config({"scoring": {"n_azimuths": 0}}) is not None
        assert tower_ranking.validate_config({"scoring": {"n_azimuths": 36}}) is None

    def test_a_grid_finer_than_the_disk_is_capped(self):
        # 0.01 km cells over an 80 km disk is 256 million of them: not a slow
        # search, an OOM-killed container that comes back and does it again.
        error = tower_ranking.validate_config({"scoring": {"grid_km": 0.01}})
        assert error is not None and "ceiling" in error

    def test_a_grid_coarser_than_the_disk_is_rejected(self):
        assert tower_ranking.validate_config({"scoring": {"grid_km": 100, "max_range_km": 80}}) is not None

    def test_band_params_must_carry_both_knobs(self):
        assert tower_ranking.validate_config({"scoring": {"band_params": {"FM": {"bw_hz": 100e3}}}}) is not None
        assert tower_ranking.validate_config({"scoring": {"band_params": {"FM": {"cpi_s": 1.0}}}}) is not None

    def test_band_params_must_be_positive(self):
        cfg = {"scoring": {"band_params": {"FM": {"bw_hz": 0, "cpi_s": 1.0}}}}
        # 10*log10(0) is -inf, which no amount of EIRP recovers from.
        assert tower_ranking.validate_config(cfg) is not None

    def test_band_params_must_be_objects(self):
        assert tower_ranking.validate_config({"scoring": {"band_params": {"FM": 6e6}}}) is not None


class TestValidateBandOffsets:
    def test_absent_is_valid(self):
        assert tower_ranking.validate_config({"ranking": {}}) is None

    def test_numbers_accepted(self):
        assert tower_ranking.validate_config({"ranking": {"band_offset_db": {"FM": -3, "UHF": 1.5}}}) is None

    def test_must_be_an_object(self):
        assert "must be an object" in tower_ranking.validate_config({"ranking": {"band_offset_db": [0, 0, 0]}})

    def test_values_must_be_numbers(self):
        # Added to EIRP inside the model, so a string raises deep in numpy.
        assert tower_ranking.validate_config({"ranking": {"band_offset_db": {"FM": "low"}}}) is not None

    def test_nan_rejected(self):
        cfg = json.loads('{"ranking": {"band_offset_db": {"FM": NaN}}}')
        assert tower_ranking.validate_config(cfg) is not None


class TestValidateDiversity:
    """The MMR knobs meet as a (1 - lambda * sim) multiplier and two Gaussian
    divisors. Every one of these passes a structural check and then either
    inverts the pass (a negative multiplier ranks the most redundant tower
    first) or divides by zero inside it, on every search."""

    def test_absent_section_is_valid(self):
        assert tower_ranking.validate_config({"ranking": {}}) is None

    def test_empty_section_is_valid(self):
        assert tower_ranking.validate_config({"ranking": {"diversity": {}}}) is None

    def test_shipped_section_is_valid(self):
        shipped = _shipped_default()["ranking"]["diversity"]
        assert tower_ranking.validate_config({"ranking": {"diversity": shipped}}) is None

    def test_section_must_be_an_object(self):
        error = tower_ranking.validate_config({"ranking": {"diversity": [0.7]}})
        assert error is not None and "must be an object" in error

    def test_enabled_must_be_a_bool(self):
        assert tower_ranking.validate_config({"ranking": {"diversity": {"enabled": "yes"}}}) is not None
        assert tower_ranking.validate_config({"ranking": {"diversity": {"enabled": 1}}}) is not None
        assert tower_ranking.validate_config({"ranking": {"diversity": {"enabled": False}}}) is None

    @pytest.mark.parametrize("key", tower_ranking._DIVERSITY_UNIT_KEYS)
    def test_unit_keys_reject_a_string(self, key):
        assert tower_ranking.validate_config({"ranking": {"diversity": {key: "lots"}}}) is not None

    @pytest.mark.parametrize("key", tower_ranking._DIVERSITY_UNIT_KEYS)
    def test_unit_keys_reject_values_outside_the_unit_interval(self, key):
        assert tower_ranking.validate_config({"ranking": {"diversity": {key: 1.5}}}) is not None
        assert tower_ranking.validate_config({"ranking": {"diversity": {key: -0.1}}}) is not None
        # Both ends are usable: 0 turns the term off, 1 is total overlap.
        assert tower_ranking.validate_config({"ranking": {"diversity": {key: 0}}}) is None
        assert tower_ranking.validate_config({"ranking": {"diversity": {key: 1}}}) is None

    @pytest.mark.parametrize("key", tower_ranking._DIVERSITY_POSITIVE_KEYS)
    def test_positive_keys_reject_a_string_zero_and_negatives(self, key):
        assert tower_ranking.validate_config({"ranking": {"diversity": {key: "wide"}}}) is not None
        assert tower_ranking.validate_config({"ranking": {"diversity": {key: 0}}}) is not None
        assert tower_ranking.validate_config({"ranking": {"diversity": {key: -5}}}) is not None
        assert tower_ranking.validate_config({"ranking": {"diversity": {key: 12.5}}}) is None

    def test_nan_rejected(self):
        cfg = json.loads('{"ranking": {"diversity": {"lambda": NaN}}}')
        assert tower_ranking.validate_config(cfg) is not None

    def test_bool_is_not_a_number_here_either(self):
        assert tower_ranking.validate_config({"ranking": {"diversity": {"lambda": True}}}) is not None


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

    def test_the_scoring_section_does_not_alias_the_caller_either(self, restore_config):
        """band_params nests a dict per band, so this one needs a copy a level
        deeper than the rest."""
        body = {
            "ranking": {"band_offset_db": {"FM": 0.0}},
            "scoring": {"band_params": {"FM": {"bw_hz": 100e3, "cpi_s": 1.0}}},
        }

        tower_ranking.apply_config(body)

        body["ranking"]["band_offset_db"]["FM"] = 99.0
        body["scoring"]["band_params"]["FM"]["cpi_s"] = 99.0

        assert tower_ranking.BAND_OFFSET_DB == {"FM": 0.0}
        assert tower_ranking.SCORING_PARAMS.band_params == {"FM": {"bw_hz": 100e3, "cpi_s": 1.0}}

    def test_scoring_params_take_the_receiver_gain_at_call_time(self, restore_config):
        """The model and the FSPL link budget must use the same receiver
        antenna: rx_gain_dbi is not a scoring knob of its own."""
        tower_ranking.apply_config({"receiver": {"rx_antenna_gain_dbi": 11.0}})

        assert tower_ranking._scoring_params().rx_gain_dbi == 11.0

        tower_ranking.RX_ANTENNA_GAIN_DBI = 3.0
        assert tower_ranking._scoring_params().rx_gain_dbi == 3.0

    def test_scoring_knobs_reach_the_model(self, restore_config):
        tower_ranking.apply_config({"scoring": {"n_azimuths": 8, "max_range_km": 40}})

        params = tower_ranking._scoring_params()
        assert params.n_azimuths == 8
        assert params.max_range_km == 40

    def test_default_sort_order_matches_the_shipped_file(self, restore_config):
        """A config naming no sort_order ranks the same way as a fresh overlay.

        The monolith leads its own fallback with coverage_area_added_km2;
        adopting that here would silently re-rank every deployment whose config
        omits the section.
        """
        tower_ranking.apply_config({})

        assert tower_ranking.SORT_ORDER == _shipped_default()["ranking"]["sort_order"]
        assert tower_ranking.BAND_PRIORITY == _shipped_default()["ranking"]["band_priority"]

    def test_shipped_default_ranks_by_expected_area_then_power(self):
        """Expected detection area first, modelled received power as the
        tie-break. The model returns a multiple of the cell area, so towers
        genuinely do tie, and power is the more informative of the two orders
        within a tie."""
        ranking = _shipped_default()["ranking"]
        assert ranking["sort_order"] == [
            {"field": "expected_area_km2", "ascending": False},
            {"field": "received_power_dbm", "ascending": False},
        ]
        assert "distance_classes" not in ranking
        assert "distance_priority" not in ranking

    def test_band_priority_survives_for_overlays_that_still_sort_on_it(self):
        """The hard band tier is no longer in the shipped sort_order — the
        model's band_offset_db is its successor — but the table is still
        shipped, applied and sortable, so an overlay naming it keeps working."""
        ranking = _shipped_default()["ranking"]
        assert ranking["band_priority"]["VHF"] == ranking["band_priority"]["UHF"]
        assert ranking["band_priority"]["FM"] > ranking["band_priority"]["UHF"]
        assert "band_priority" in tower_ranking._SORTABLE_FIELDS

    def test_shipped_band_offsets_are_neutral_placeholders(self):
        """Zero until they are fitted from fleet data: a placeholder that
        shifts nothing is honest, an invented number is not."""
        assert _shipped_default()["ranking"]["band_offset_db"] == {"VHF": 0, "UHF": 0, "FM": 0}

    def test_the_diversity_section_does_not_alias_the_caller(self, restore_config):
        body = {"ranking": {"diversity": {"enabled": True, "lambda": 0.4}}}

        tower_ranking.apply_config(body)
        body["ranking"]["diversity"]["lambda"] = 0.99
        body["ranking"]["diversity"]["enabled"] = False

        assert tower_ranking.DIVERSITY["lambda"] == 0.4
        assert tower_ranking.DIVERSITY["enabled"] is True

    def test_a_partial_diversity_section_keeps_the_other_defaults(self, restore_config):
        tower_ranking.apply_config({"ranking": {"diversity": {"lambda": 0.2}}})

        assert tower_ranking.DIVERSITY["lambda"] == 0.2
        assert tower_ranking.DIVERSITY["site_radius_km"] == tower_ranking._DEFAULT_DIVERSITY["site_radius_km"]
        assert set(tower_ranking.DIVERSITY) == set(tower_ranking._DEFAULT_DIVERSITY)

    def test_an_unknown_diversity_key_is_dropped_not_carried(self, restore_config):
        """The MMR pass reads these by name, so a typo that reached live state
        would sit there looking like it did something."""
        tower_ranking.apply_config({"ranking": {"diversity": {"lamda": 0.2}}})

        assert "lamda" not in tower_ranking.DIVERSITY
        assert tower_ranking.DIVERSITY == tower_ranking._DEFAULT_DIVERSITY

    def test_shipped_diversity_section_matches_the_in_code_defaults(self, restore_config):
        """As with scoring and sort_order: an overlay that omits the section
        must order the same way as a fresh one."""
        tower_ranking.apply_config(_shipped_default())
        from_file = dict(tower_ranking.DIVERSITY)

        tower_ranking.apply_config({})

        assert from_file == tower_ranking.DIVERSITY
        assert set(_shipped_default()["ranking"]["diversity"]) == set(tower_ranking._DEFAULT_DIVERSITY)

    def test_shipped_scoring_section_matches_the_in_code_defaults(self, restore_config):
        """The section spells out every knob, and the in-code fallback has to
        agree with it: an overlay that omits the section must score the same
        way as a fresh one, as with sort_order."""
        shipped = _shipped_default()
        tower_ranking.apply_config(shipped)
        from_file = tower_ranking.SCORING_PARAMS

        tower_ranking.apply_config({})
        from_code = tower_ranking.SCORING_PARAMS

        assert from_file == from_code
        assert set(shipped["scoring"]) == set(tower_ranking._SCORING_PARAM_KEYS) | {"band_params"}


class TestShippedRanking:
    """What the shipped config ranks on, run through process_and_rank.

    Every tower sits at the same spot ~20 km north of the receiver, so EIRP
    and band are the only things that vary and distance cannot leak into the
    order. The rank is the bistatic detection-area model's answer
    (services/tower_scoring.py); received power only breaks ties.
    """

    _LAT, _LON = 33.749, -84.388

    @pytest.fixture(autouse=True)
    def _shipped(self, restore_config):
        tower_ranking.apply_config(_shipped_default())

    def _rank(self, devices):
        return tower_ranking.process_and_rank([_system(devices)], self._LAT, self._LON)

    def test_every_tower_carries_the_model_fields(self):
        towers = self._rank([_device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="UHF", eirp=100_000.0)])
        t = towers[0]
        assert t["expected_area_km2"] > 0
        assert 0 <= t["best_azimuth_deg"] < 360
        assert t["horizon_km"] > 0

    def test_towers_are_ordered_by_expected_area(self):
        devices = [
            _device(freq_mhz=185.0, lat=33.93, lon=-84.388, callsign="VHF_WEAK", eirp=1.0),
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="UHF_STRONG", eirp=1_000_000.0),
            _device(freq_mhz=195.0, lat=33.93, lon=-84.388, callsign="VHF_STRONG", eirp=10_000.0),
            _device(freq_mhz=545.0, lat=33.93, lon=-84.388, callsign="UHF_WEAK", eirp=100.0),
        ]
        towers = self._rank(devices)
        areas = [t["expected_area_km2"] for t in towers]
        assert areas == sorted(areas, reverse=True)
        # UHF and VHF interleave: neither band outranks the other, as before.
        assert [t["callsign"] for t in towers] == ["UHF_STRONG", "VHF_STRONG", "UHF_WEAK", "VHF_WEAK"]

    def test_tv_outranks_fm_at_equal_eirp_and_distance(self):
        """What the hard band tier was standing in for. It survives as a
        margin the model produces (~18 dB of processing gain on a 6 MHz
        channel), not as a rule no amount of power can overcome."""
        devices = [
            _device(freq_mhz=95.5, lat=33.93, lon=-84.388, callsign="FM", eirp=100_000.0),
            _device(freq_mhz=185.0, lat=33.93, lon=-84.388, callsign="VHF", eirp=100_000.0),
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="UHF", eirp=100_000.0),
        ]
        towers = self._rank(devices)
        assert towers[-1]["callsign"] == "FM"

    def test_a_huge_fm_tower_now_outranks_a_tiny_tv_one(self):
        """The deliberate behaviour change. Under the old band tier a 1 MW FM
        station ranked below every 10 W TV tower in the list; the model says a
        usable illuminator beats an unusable one whatever band it is in."""
        devices = [
            _device(freq_mhz=95.5, lat=33.93, lon=-84.388, callsign="FM_HUGE", eirp=1_000_000.0),
            _device(freq_mhz=185.0, lat=33.93, lon=-84.388, callsign="VHF_TINY", eirp=10.0),
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="UHF_TINY", eirp=10.0),
        ]
        towers = self._rank(devices)
        assert towers[0]["callsign"] == "FM_HUGE"
        assert towers[0]["expected_area_km2"] > towers[1]["expected_area_km2"]

    def test_measured_score_no_longer_reorders_the_shipped_ranking(self):
        """POST /api/towers: the SDR heard the weak tower better than the
        strong one. That used to win outright. It says how well the receiver
        hears the illuminator, which is not what the rank is answering any
        more, so the model's area decides and the score comes back untouched
        for a client that wants it."""
        devices = [
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="UHF_STRONG", eirp=1_000_000.0),
            _device(freq_mhz=545.0, lat=33.93, lon=-84.388, callsign="UHF_WEAK", eirp=100.0),
        ]
        measurements = [
            {"freq_mhz": 515.0, "band": "UHF", "score": 0.4, "snr_db": None, "obw_fraction": None, "power_db": -60.0},
            {"freq_mhz": 545.0, "band": "UHF", "score": 0.9, "snr_db": None, "obw_fraction": None, "power_db": -40.0},
        ]
        towers = tower_ranking.process_and_rank([_system(devices)], self._LAT, self._LON, measurements=measurements)
        assert [t["callsign"] for t in towers] == ["UHF_STRONG", "UHF_WEAK"]
        assert towers[0]["expected_area_km2"] > towers[1]["expected_area_km2"]
        assert [t["score"] for t in towers] == [0.4, 0.9]

    def test_an_operator_can_still_rank_on_the_measured_score(self, restore_config):
        """Switching ranking strategy stays a config PUT, not a code change."""
        cfg = _shipped_default()
        cfg["ranking"]["sort_order"] = [{"field": "score", "ascending": False}]
        tower_ranking.apply_config(cfg)

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

    def test_a_megawatt_next_door_ranks_below_a_usable_tower_further_out(self):
        """The reason for the redesign, end to end: the loudest signal in the
        band is the worst illuminator on the list, because its direct path
        swamps the surveillance channel. The old power-ordered ranking put it
        first."""
        devices = [
            _device(freq_mhz=515.0, lat=33.755, lon=-84.388, callsign="NEXT_DOOR", eirp=1_000_000.0),
            _device(freq_mhz=545.0, lat=34.09, lon=-84.388, callsign="ACROSS_TOWN", eirp=100_000.0),
        ]
        towers = self._rank(devices)
        assert [t["callsign"] for t in towers] == ["ACROSS_TOWN", "NEXT_DOOR"]
        # It is still the loudest thing the receiver hears, by a wide margin.
        assert towers[1]["received_power_dbm"] > towers[0]["received_power_dbm"]

    def test_distance_no_longer_decides_the_order(self):
        # Under the old classes an 80 km tower was "Far" and a 20 km one
        # "Ideal", and the class outranked everything. No class survives, and
        # the model prefers the far tower here on its own terms.
        devices = [
            _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="NEAR_WEAK", eirp=10.0),
            _device(freq_mhz=545.0, lat=34.45, lon=-84.388, callsign="FAR_STRONG", eirp=1_000_000.0),
        ]
        towers = tower_ranking.process_and_rank([_system(devices)], self._LAT, self._LON)
        assert [t["callsign"] for t in towers] == ["FAR_STRONG", "NEAR_WEAK"]
        assert towers[0]["distance_km"] > towers[1]["distance_km"]
        assert "distance_class" not in towers[0]


class TestDiversityOrdering:
    """Site-aware MMR, through process_and_rank on the shipped config.

    A node tunes one centre frequency at a time and Auto-Calibrate tries at most
    three candidates from the top of the list, so ten channels of one mast in
    the top ten is three attempts at the same mast, the same direct path and the
    same failure.
    """

    _LAT, _LON = 33.749, -84.388

    # Two UHF channels on one mast ~20 km north, plus a weaker tower ~22 km
    # east. The east tower's area is well below the mast's and well above 30% of
    # it, so only the same-site penalty (lambda 0.7 x sim 1.0) can lift it above
    # the mast's second channel.
    _SITE_A1 = _device(freq_mhz=515.0, lat=33.93, lon=-84.388, callsign="SITE_A1", eirp=100_000.0)
    _SITE_A2 = _device(freq_mhz=521.0, lat=33.93, lon=-84.388, callsign="SITE_A2", eirp=100_000.0)
    _EAST = _device(freq_mhz=527.0, lat=33.749, lon=-84.15, callsign="EAST", eirp=3_000.0)
    # Same mast, other band: the smaller penalty (sim 0.5).
    _SITE_FM = _device(freq_mhz=95.5, lat=33.93, lon=-84.388, callsign="SITE_FM", eirp=100_000.0)

    @pytest.fixture(autouse=True)
    def _shipped(self, restore_config):
        tower_ranking.apply_config(_shipped_default())

    def _diversity(self, **knobs):
        cfg = _shipped_default()
        cfg["ranking"]["diversity"] = {**cfg["ranking"]["diversity"], **knobs}
        tower_ranking.apply_config(cfg)

    def _rank(self, devices, **kwargs):
        return tower_ranking.process_and_rank([_system(devices)], self._LAT, self._LON, **kwargs)

    def _by_callsign(self, towers):
        return {t["callsign"]: t for t in towers}

    def test_a_second_channel_on_one_mast_falls_below_a_weaker_tower_elsewhere(self):
        towers = self._rank([self._SITE_A1, self._SITE_A2, self._EAST])

        assert [t["callsign"] for t in towers] == ["SITE_A1", "EAST", "SITE_A2"]
        # The demoted channel is genuinely the stronger of the two: this is the
        # diversity penalty, not the model preferring the east tower.
        by = self._by_callsign(towers)
        assert by["SITE_A2"]["expected_area_km2"] > by["EAST"]["expected_area_km2"]

    def test_with_diversity_off_the_plain_sort_stands(self):
        self._diversity(enabled=False)

        towers = self._rank([self._SITE_A1, self._SITE_A2, self._EAST])

        assert [t["callsign"] for t in towers] == ["SITE_A1", "SITE_A2", "EAST"]
        areas = [t["expected_area_km2"] for t in towers]
        assert areas == sorted(areas, reverse=True)
        assert all(t["diversity_penalty"] == 0.0 for t in towers)

    def test_the_site_fields_are_stamped_whether_or_not_mmr_runs(self):
        self._diversity(enabled=False)

        towers = self._rank([self._SITE_A1, self._SITE_A2, self._EAST])

        by = self._by_callsign(towers)
        assert by["SITE_A1"]["site_id"] == by["SITE_A2"]["site_id"]
        assert by["SITE_A1"]["site_channels"] == 2
        assert by["EAST"]["site_channels"] == 1

    def test_same_site_other_band_pays_the_smaller_penalty(self):
        towers = self._rank([self._SITE_A1, self._SITE_A2, self._SITE_FM])

        by = self._by_callsign(towers)
        # lambda 0.7 x same_site_same_band 1.0, against 0.7 x 0.5 across bands.
        assert by["SITE_A2"]["diversity_penalty"] == pytest.approx(0.7)
        assert by["SITE_FM"]["diversity_penalty"] == pytest.approx(0.35)

    def test_the_first_pick_pays_nothing(self):
        towers = self._rank([self._SITE_A1, self._SITE_A2, self._EAST])

        assert towers[0]["diversity_penalty"] == 0.0

    def test_site_channels_counts_every_band_on_the_mast(self):
        towers = self._rank([self._SITE_A1, self._SITE_A2, self._SITE_FM, self._EAST])

        by = self._by_callsign(towers)
        assert by["SITE_FM"]["site_channels"] == 3
        assert by["SITE_A1"]["site_channels"] == 3
        assert by["EAST"]["site_channels"] == 1

    def test_the_site_id_is_its_lead_towers_coordinates(self):
        towers = self._rank([self._SITE_A1, self._SITE_A2])

        assert {t["site_id"] for t in towers} == {"33.9300,-84.3880"}

    def test_the_matched_first_split_survives_the_diversity_pass(self):
        """A hand-typed frequency says which towers the caller asked about,
        which is not a preference MMR may trade away."""
        devices = [self._SITE_A1, self._SITE_A2, self._EAST, self._SITE_FM]

        towers = self._rank(devices, user_frequencies=[95.5])

        assert towers[0]["callsign"] == "SITE_FM"
        assert towers[0]["frequency_matched"] is True
        assert all(t["frequency_matched"] is False for t in towers[1:])
        # And the unmatched group is still diversified among itself.
        assert [t["callsign"] for t in towers[1:]] == ["SITE_A1", "EAST", "SITE_A2"]

    def test_ranks_stay_contiguous_under_mmr(self):
        towers = self._rank([self._SITE_A1, self._SITE_A2, self._EAST, self._SITE_FM])

        assert [t["rank"] for t in towers] == [1, 2, 3, 4]

    def test_lambda_zero_reproduces_the_plain_sort(self):
        """The knob has to be able to turn the pass off by degrees as well as
        outright: at lambda 0 every penalty is 0 and MMR is the sort."""
        self._diversity(**{"lambda": 0})

        towers = self._rank([self._SITE_A1, self._SITE_A2, self._EAST])

        assert [t["callsign"] for t in towers] == ["SITE_A1", "SITE_A2", "EAST"]
        assert all(t["diversity_penalty"] == 0.0 for t in towers)

    def test_a_sort_that_leads_elsewhere_is_left_alone(self, restore_config):
        """MMR's value is expected_area_km2, so an operator ranking on anything
        else gets their sort, not a diversity pass over a number their config
        does not use."""
        cfg = _shipped_default()
        cfg["ranking"]["sort_order"] = [{"field": "received_power_dbm", "ascending": False}]
        tower_ranking.apply_config(cfg)

        towers = self._rank([self._SITE_A1, self._SITE_A2, self._EAST])

        powers = [t["received_power_dbm"] for t in towers]
        assert powers == sorted(powers, reverse=True)
        assert all(t["diversity_penalty"] == 0.0 for t in towers)

    def test_diagnostics_name_the_ordering_that_ran(self, restore_config):
        devices = [self._SITE_A1, self._SITE_A2]

        diagnostics: dict = {}
        self._rank(devices, diagnostics=diagnostics)
        assert diagnostics["ranking"] == "expected_area_mmr"

        self._diversity(enabled=False)
        diagnostics = {}
        self._rank(devices, diagnostics=diagnostics)
        assert diagnostics["ranking"] == "expected_area"

        cfg = _shipped_default()
        cfg["ranking"]["sort_order"] = [{"field": "distance_km", "ascending": True}]
        tower_ranking.apply_config(cfg)
        diagnostics = {}
        self._rank(devices, diagnostics=diagnostics)
        assert diagnostics["ranking"] == "sort_order"


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

        # Dropping the distance rule leaves the default that shipped before it
        # did, which the second migration then upgrades (see
        # TestLegacyDefaultSortUpgrade below).
        assert tower_ranking.SORT_ORDER == _shipped_default()["ranking"]["sort_order"]
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


class TestLegacyDefaultSortUpgrade:
    """An overlay still on a shipped default has to follow the shipped default.

    The overlay volume is seeded once and never re-seeded, so without this a
    change of default changes nothing anywhere that already has one: every
    deployment would keep ranking on band tier and received power for good,
    and the only symptom would be the new ranking never appearing.
    """

    _NEW_DEFAULT = [
        {"field": "expected_area_km2", "ascending": False},
        {"field": "received_power_dbm", "ascending": False},
    ]

    def _reload(self, tmp_path, monkeypatch, caplog, cfg):
        path = tmp_path / "tower_config.json"
        path.write_text(json.dumps(cfg))
        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", path)
        with caplog.at_level("WARNING", logger=tower_ranking.__name__):
            tower_ranking.reload_config()
        return path

    @pytest.mark.parametrize(
        "legacy_sort",
        [
            # The default that shipped until this change.
            [
                {"field": "band_priority", "ascending": True},
                {"field": "score", "ascending": False},
                {"field": "received_power_dbm", "ascending": False},
            ],
            # 2026-05-28's.
            [
                {"field": "band_priority", "ascending": True},
                {"field": "score", "ascending": False},
            ],
            # And the one before it, as _drop_legacy_distance_rules leaves it.
            [
                {"field": "band_priority", "ascending": True},
                {"field": "received_power_dbm", "ascending": False},
            ],
        ],
    )
    def test_a_shipped_default_is_upgraded_in_memory(self, legacy_sort, tmp_path, monkeypatch, restore_config, caplog):
        cfg = {"ranking": {"band_priority": {"VHF": 0, "UHF": 0, "FM": 1}, "sort_order": legacy_sort}}
        path = self._reload(tmp_path, monkeypatch, caplog, cfg)

        assert tower_ranking.SORT_ORDER == self._NEW_DEFAULT
        assert "expected detection area" in caplog.text
        assert str(path) in caplog.text
        assert json.loads(path.read_text()) == cfg, "the file on disk is not rewritten"

    def test_the_upgrade_does_not_alias_the_default(self, tmp_path, monkeypatch, restore_config, caplog):
        """Two overlays upgraded in one process must not share rule objects
        with each other or with the module default."""
        legacy = [
            {"field": "band_priority", "ascending": True},
            {"field": "received_power_dbm", "ascending": False},
        ]
        self._reload(tmp_path, monkeypatch, caplog, {"ranking": {"sort_order": legacy}})

        tower_ranking.SORT_ORDER[0]["ascending"] = True

        assert tower_ranking._DEFAULT_SORT_ORDER == self._NEW_DEFAULT

    def test_an_operators_own_sort_order_is_left_alone(self, tmp_path, monkeypatch, restore_config, caplog):
        """Anything that is not exactly a shipped default is a deliberate
        choice — including one that merely resembles one."""
        deliberate = [
            {"field": "band_priority", "ascending": True},
            {"field": "distance_km", "ascending": True},
        ]
        self._reload(tmp_path, monkeypatch, caplog, {"ranking": {"sort_order": deliberate}})

        assert tower_ranking.SORT_ORDER == deliberate
        assert "expected detection area" not in caplog.text

    def test_a_near_miss_of_a_default_is_left_alone(self, tmp_path, monkeypatch, restore_config, caplog):
        # Same fields as the old default, one direction flipped: an operator
        # who wanted the quietest towers first still gets them.
        near_miss = [
            {"field": "band_priority", "ascending": True},
            {"field": "received_power_dbm", "ascending": True},
        ]
        self._reload(tmp_path, monkeypatch, caplog, {"ranking": {"sort_order": near_miss}})

        assert tower_ranking.SORT_ORDER == near_miss
        assert "expected detection area" not in caplog.text

    def test_the_current_default_is_not_warned_about(self, tmp_path, monkeypatch, restore_config, caplog):
        self._reload(tmp_path, monkeypatch, caplog, _shipped_default())

        assert tower_ranking.SORT_ORDER == self._NEW_DEFAULT
        assert "expected detection area" not in caplog.text

    def test_a_config_naming_no_sort_order_is_untouched_and_silent(self, tmp_path, monkeypatch, restore_config, caplog):
        # apply_config's fallback already is the new default; there is nothing
        # to migrate and nothing to warn about.
        self._reload(tmp_path, monkeypatch, caplog, {"search": {"default_limit": 5}})

        assert tower_ranking.SORT_ORDER == self._NEW_DEFAULT
        assert "expected detection area" not in caplog.text
