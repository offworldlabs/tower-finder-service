"""Tests for the hardened PUT /api/config.

The ordering under test — validate, prove it applies, and only then write — is
ported from the monolith's routes/config.py. It exists because _CONFIG_PATH
lives in a persistent volume and reload_config() runs at import: a config that
reaches disk without applying cleanly outlives a restart and a redeploy, so the
file must only ever hold something the running process has already accepted.

The admin guard itself is covered by test_config_auth.py; every request here
carries a valid token.
"""

import json
import unittest.mock

import pytest
from core.auth import ENV_VAR
from fastapi.testclient import TestClient
from routes import towers as towers_route
from services import tower_ranking
from tests._helpers import device, system

from app import app

TOKEN = "s3cret-admin-token"

# Written to the scratch overlay before each test, so "the file did not change"
# is checkable against something a valid PUT would visibly replace.
SENTINEL = {"search": {"default_limit": 11}}

VALID = {
    "receiver": {"rx_antenna_gain_dbi": 6.0, "sensitivity_dbm": -120.0},
    "broadcast_bands": {"FM": [[87.8, 108.0]]},
    "ranking": {
        "band_priority": {"FM": 0},
        "distance_classes": [{"label": "Ideal", "min_km": 0, "max_km": None}],
        "distance_priority": {"Ideal": 0},
        "sort_order": [{"field": "band_priority", "ascending": True}, {"field": "score", "ascending": False}],
    },
    "search": {"default_radius_km": 80, "default_limit": 25},
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv(ENV_VAR, TOKEN)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    """A scratch overlay for the route, seeded with the sentinel config.

    routes/towers.py binds _CONFIG_PATH at import, so the route's own name is
    the one to patch — patching services.tower_ranking._CONFIG_PATH would leave
    the endpoint writing the real overlay.
    """
    path = tmp_path / "tower_config.json"
    path.write_text(json.dumps(SENTINEL))
    monkeypatch.setattr(towers_route, "_CONFIG_PATH", path)
    monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", path)
    return path


@pytest.fixture(autouse=True)
def restore_config():
    """Put the live ranking settings back: a successful PUT really applies."""
    saved = {name: getattr(tower_ranking, name) for name in tower_ranking.CONFIG_SETTINGS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(tower_ranking, name, value)


def _put(client, body):
    return client.put("/api/config", json=body, headers={"Authorization": f"Bearer {TOKEN}"})


class TestRejectsWithoutWriting:
    def test_invalid_config_is_rejected_and_never_reaches_disk(self, client, config_path):
        """The whole point of the ordering. A config naming an unsortable field
        applies without raising and then breaks every search — on a persistent
        volume that survives the restart meant to fix it."""
        before = config_path.read_text()
        live_sort_order = list(tower_ranking.SORT_ORDER)

        r = _put(client, {"ranking": {"sort_order": [{"field": "callsign", "ascending": False}]}})

        assert r.status_code == 400
        assert "callsign" in r.json()["detail"]
        assert config_path.read_text() == before, "a rejected config must not be written"
        assert tower_ranking.SORT_ORDER == live_sort_order, "a rejected config must not be applied"

    def test_structurally_broken_config_is_rejected(self, client, config_path):
        before = config_path.read_text()

        r = _put(client, {"ranking": {"distance_classes": [{"label": "Ideal", "min_km": 8}]}})

        assert r.status_code == 400
        assert "max_km" in r.json()["detail"]
        assert config_path.read_text() == before

    def test_a_validation_gap_is_caught_by_the_apply(self, client, config_path, monkeypatch):
        """validate_config is not assumed perfect. A body that slips past it
        still has to apply before anything is written, and apply_config is
        all-or-nothing, so the running config survives."""
        before = config_path.read_text()
        monkeypatch.setattr(towers_route, "validate_config", lambda body: None)

        r = _put(client, {"ranking": {"distance_classes": [{"label": "Ideal", "min_km": 8}]}})

        assert r.status_code == 400
        assert "could not be applied" in r.json()["detail"]
        assert config_path.read_text() == before

    def test_unwritable_path_reports_the_write_failure(self, client, config_path, monkeypatch):
        """The config has applied but the file has not: a 500, not a silent
        divergence dressed up as success."""
        monkeypatch.setattr(towers_route, "_CONFIG_PATH", config_path.parent / "missing-dir" / "tower_config.json")

        r = _put(client, VALID)

        assert r.status_code == 500
        assert "could not be written" in r.json()["detail"]


class TestAcceptsAndApplies:
    def test_valid_config_is_applied_then_written(self, client, config_path):
        r = _put(client, VALID)

        assert r.status_code == 200
        assert r.json() == {"status": "updated"}
        assert json.loads(config_path.read_text()) == VALID
        assert tower_ranking.DEFAULT_LIMIT == 25
        assert tower_ranking.SORT_ORDER == VALID["ranking"]["sort_order"]

    def test_get_returns_what_was_written(self, client, config_path):
        _put(client, VALID)
        r = client.get("/api/config")
        assert r.status_code == 200
        assert r.json() == VALID

    @pytest.mark.parametrize("field", sorted(tower_ranking._SORTABLE_FIELDS))
    def test_every_sortable_field_can_be_put(self, client, config_path, field):
        """Switching ranking strategy is a config PUT, not a code change — for
        the monolith's coverage/distance/power fields and this service's
        analyser-measurement ones alike."""
        body = {"ranking": {"sort_order": [{"field": field, "ascending": False}]}}

        r = _put(client, body)

        assert r.status_code == 200, r.json()
        assert tower_ranking.SORT_ORDER == [{"field": field, "ascending": False}]
        assert json.loads(config_path.read_text()) == body

    def test_coverage_first_ranking_is_a_config_change(self, client, config_path):
        """The monolith's default ordering, applied to this service by PUT."""
        body = {
            "ranking": {
                "sort_order": [
                    {"field": "coverage_area_added_km2", "ascending": False},
                    {"field": "band_priority", "ascending": True},
                    {"field": "distance_priority", "ascending": True},
                    {"field": "received_power_dbm", "ascending": False},
                ]
            }
        }

        r = _put(client, body)

        assert r.status_code == 200
        assert tower_ranking.SORT_ORDER[0]["field"] == "coverage_area_added_km2"

    def test_applied_config_does_not_alias_the_request_body(self, client, config_path):
        """The handler applies the parsed body itself, then serialises it for
        the write; live settings must not be aliases into that object."""
        _put(client, VALID)

        assert tower_ranking.BAND_PRIORITY == {"FM": 0}
        assert tower_ranking.SORT_ORDER is not VALID["ranking"]["sort_order"]


# ── The route observing a config change, not just tower_ranking's own state ──


class TestConfigChangeReachesRoute:
    """routes/towers.py used to import DEFAULT_LIMIT / DEFAULT_RADIUS_KM by
    name, binding a copy at module load time. apply_config() rebinds
    tower_ranking's own globals, which a name imported that way never sees
    again: checking tower_ranking.DEFAULT_LIMIT after a PUT proves the
    setting changed, but not that the route can see it, which is the actual
    bug: these round-trip through GET /api/towers to prove the route itself
    observes the new value on its very next request."""

    def _get_towers(self, client, query, raw_systems):
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=raw_systems),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            return client.get(f"/api/towers?{query}")

    def test_put_default_limit_is_seen_by_get_towers(self, client, config_path):
        towers = [device(95.5 + i * 0.4, 33.9, -84.6, callsign=f"T{i}", eirp=10000) for i in range(3)]
        raw = [system(towers, licence_type="Broadcast", licence_subtype="FM")]
        query = "lat=33.9&lon=-84.6&source=us"

        before = self._get_towers(client, query, raw)
        assert len(before.json()["towers"]) == 3  # baseline default_limit comfortably covers 3

        body = dict(VALID, search={"default_radius_km": 80, "default_limit": 1})
        r = _put(client, body)
        assert r.status_code == 200

        after = self._get_towers(client, query, raw)
        assert len(after.json()["towers"]) == 1
        assert after.json()["count"] == 1

    def test_put_default_radius_km_is_seen_by_get_towers(self, client, config_path):
        near = device(95.5, 33.9, -84.6, callsign="NEAR", eirp=10000)
        far = device(96.5, 34.4, -84.6, callsign="FAR", eirp=10000)  # ~56 km north of the query point
        raw = [system([near, far], licence_type="Broadcast", licence_subtype="FM")]
        query = "lat=33.9&lon=-84.6&source=us"

        before = self._get_towers(client, query, raw)
        # Baseline default_radius_km (shipped 80 km) comfortably covers ~56 km.
        assert {t["callsign"] for t in before.json()["towers"]} == {"NEAR", "FAR"}

        body = dict(VALID, search={"default_radius_km": 10, "default_limit": 25})
        r = _put(client, body)
        assert r.status_code == 200

        after = self._get_towers(client, query, raw)
        assert {t["callsign"] for t in after.json()["towers"]} == {"NEAR"}
