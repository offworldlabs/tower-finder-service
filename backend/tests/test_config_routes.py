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
import os
import stat

import pytest
from core.auth import ENV_VAR
from fastapi.testclient import TestClient
from routes import towers as towers_route
from services import tower_ranking
from tests._helpers import device, get_towers, system

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
    """A scratch overlay for the route, seeded with the sentinel config."""
    path = tmp_path / "tower_config.json"
    path.write_text(json.dumps(SENTINEL))
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

        r = _put(client, {"broadcast_bands": {"FM": 5}})

        assert r.status_code == 400
        assert "must be a list" in r.json()["detail"]
        assert config_path.read_text() == before

    def test_a_validation_gap_is_caught_by_the_apply(self, client, config_path, monkeypatch):
        """validate_config is not assumed perfect. A body that slips past it
        still has to apply before anything is written, and apply_config is
        all-or-nothing, so the running config survives."""
        before = config_path.read_text()
        monkeypatch.setattr(towers_route, "validate_config", lambda body: None)

        r = _put(client, {"broadcast_bands": {"FM": 5}})

        assert r.status_code == 400
        assert "could not be applied" in r.json()["detail"]
        assert config_path.read_text() == before

    def test_unwritable_path_reports_the_write_failure(self, client, config_path, monkeypatch):
        """The config has applied but the file has not: a 500, not a silent
        divergence dressed up as success."""
        monkeypatch.setattr(tower_ranking, "_CONFIG_PATH", config_path.parent / "missing-dir" / "tower_config.json")

        r = _put(client, VALID)

        assert r.status_code == 500
        assert "could not be written" in r.json()["detail"]

    def test_the_live_config_is_never_opened_for_writing(self, client, config_path, monkeypatch):
        """The new config goes to a sibling and is renamed over the live file,
        which is never itself truncated. A direct write that failed part-way
        would leave invalid JSON, and reload_config() runs at import, so the
        next container start would crash-loop on a file only reachable inside
        the volume."""
        written = []
        real_open = open

        def recording_open(file, mode="r", *args, **kwargs):
            if "w" in mode or "a" in mode:
                written.append(str(file))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", recording_open)

        r = _put(client, VALID)
        monkeypatch.undo()

        assert r.status_code == 200
        assert str(config_path) not in written
        assert len(written) == 1
        # Name unique per request, so match its shape rather than the whole path.
        assert written[0].startswith(f"{config_path}.") and written[0].endswith(".tmp")
        assert json.loads(config_path.read_text()) == VALID
        assert not list(config_path.parent.glob("*.tmp"))

    def test_an_undurable_rename_is_not_reported_as_a_failed_write(self, client, config_path, monkeypatch):
        """The replace succeeded, so the new config is the file. Telling the
        caller it could not be written would have them retry a change that has
        already taken effect."""
        real_fsync = os.fsync

        def failing_dir_fsync(fd):
            # The directory handle only; the file's own fsync must still run.
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "Input/output error")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", failing_dir_fsync)

        r = _put(client, VALID)
        monkeypatch.undo()

        assert r.status_code == 200
        assert json.loads(config_path.read_text()) == VALID

    def test_a_failed_write_leaves_the_existing_config_intact(self, client, config_path, monkeypatch):
        """A write that cannot complete must leave the file that is there."""
        before = config_path.read_text()
        real_open = open

        def failing_open(file, mode="r", *args, **kwargs):
            if "w" in mode:
                raise OSError(28, "No space left on device")
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", failing_open)

        r = _put(client, VALID)
        monkeypatch.undo()

        assert r.status_code == 500
        assert json.loads(config_path.read_text()) == SENTINEL
        assert config_path.read_text() == before
        assert not list(config_path.parent.glob("*.tmp"))


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
                    {"field": "received_power_dbm", "ascending": False},
                ]
            }
        }

        r = _put(client, body)

        assert r.status_code == 200
        assert tower_ranking.SORT_ORDER[0]["field"] == "coverage_area_added_km2"

    def test_distance_priority_sort_rule_is_rejected(self, client, config_path):
        """Towers no longer carry a distance class, so the pre-2026-05-28
        default ordering can no longer be written."""
        before = config_path.read_text()

        r = _put(client, {"ranking": {"sort_order": [{"field": "distance_priority", "ascending": True}]}})

        assert r.status_code == 400
        assert "distance_priority" in r.json()["detail"]
        assert config_path.read_text() == before

    def test_applied_config_does_not_alias_the_request_body(self, client, config_path):
        """The handler applies the parsed body itself, then serialises it for
        the write; live settings must not be aliases into that object."""
        _put(client, VALID)

        assert tower_ranking.BAND_PRIORITY == {"FM": 0}
        assert tower_ranking.SORT_ORDER is not VALID["ranking"]["sort_order"]


# ── The route observing a config change, not just tower_ranking's own state ──


class TestConfigChangeReachesRoute:
    """Checking tower_ranking.DEFAULT_LIMIT after a PUT proves the setting
    changed, not that the route sees it. These round-trip through GET
    /api/towers to prove the route itself observes the new value on its
    very next request."""

    def test_put_default_limit_is_seen_by_get_towers(self, client, config_path):
        towers = [device(95.5 + i * 0.4, 33.9, -84.6, callsign=f"T{i}", eirp=10000) for i in range(3)]
        raw = [system(towers, licence_type="Broadcast", licence_subtype="FM")]
        query = "lat=33.9&lon=-84.6&source=us"

        # Establish the baseline rather than inherit it: the defaults loaded at
        # import come from data/runtime/, which is gitignored, hand-editable and
        # written by PUT, so a developer's overlay would otherwise decide it.
        _put(client, dict(VALID, search={"default_radius_km": 80, "default_limit": 25}))

        before = get_towers(client, query, raw)
        assert len(before.json()["towers"]) == 3

        body = dict(VALID, search={"default_radius_km": 80, "default_limit": 1})
        r = _put(client, body)
        assert r.status_code == 200

        after = get_towers(client, query, raw)
        assert len(after.json()["towers"]) == 1
        assert after.json()["count"] == 1

    def test_put_default_radius_km_is_seen_by_get_towers(self, client, config_path):
        near = device(95.5, 33.9, -84.6, callsign="NEAR", eirp=10000)
        far = device(96.5, 34.4, -84.6, callsign="FAR", eirp=10000)  # ~56 km north of the query point
        raw = [system([near, far], licence_type="Broadcast", licence_subtype="FM")]
        query = "lat=33.9&lon=-84.6&source=us"

        # Baseline established here, not inherited: see the test above.
        _put(client, dict(VALID, search={"default_radius_km": 80, "default_limit": 25}))

        before = get_towers(client, query, raw)
        assert {t["callsign"] for t in before.json()["towers"]} == {"NEAR", "FAR"}

        body = dict(VALID, search={"default_radius_km": 10, "default_limit": 25})
        r = _put(client, body)
        assert r.status_code == 200

        after = get_towers(client, query, raw)
        assert {t["callsign"] for t in after.json()["towers"]} == {"NEAR"}
