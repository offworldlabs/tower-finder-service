"""Additional tower_ranking tests — reload_config + parse_geom edge cases."""

import json

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


