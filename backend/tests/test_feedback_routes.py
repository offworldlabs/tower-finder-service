"""Tests for the fleet feedback endpoints.

The write endpoint is reachable from every node in the fleet and the service is
publicly exposed, so the cases that matter most are the ones that must not
store a row. Each test points the store at a tmp file, so nothing here writes
to the real runtime overlay.
"""

import pytest
from core.auth import ENV_VAR, FEEDBACK_ENV_VAR
from fastapi.testclient import TestClient
from services import tower_feedback

from app import app

TOKEN = "s3cret-feedback-token"
ADMIN_TOKEN = "s3cret-admin-token"

ROW = {
    "node_id": "node-1",
    "rx_lat": 34.05,
    "rx_lon": -118.25,
    "tx_lat": 34.23,
    "tx_lon": -118.06,
    "fc_hz": 98_700_000.0,
    "callsign": "KABC",
    "source": "calibration",
    "outcome": "confirmed_track",
    "max_evidence": 2,
    "max_detections": 41,
    "duration_s": 90.0,
    "gain_a": 12,
    "gain_b": 8,
    "lna_state": 3,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tower_feedback, "_DB_PATH", tmp_path / "feedback.db")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv(FEEDBACK_ENV_VAR, TOKEN)


@pytest.fixture()
def admin(monkeypatch):
    monkeypatch.setenv(ENV_VAR, ADMIN_TOKEN)


def post(client, body, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/api/feedback/tower-outcome", json=body, headers=headers)


class TestTokenNotConfigured:
    def test_ingest_is_closed_when_the_token_is_unset(self, client, monkeypatch):
        """Unset must disable ingest, not disable the guard: this endpoint
        writes to a volume and is reachable from the internet."""
        monkeypatch.delenv(FEEDBACK_ENV_VAR, raising=False)
        r = post(client, ROW)
        assert r.status_code == 503
        assert FEEDBACK_ENV_VAR in r.json()["detail"]

    def test_blank_token_is_treated_as_unset(self, client, monkeypatch):
        monkeypatch.setenv(FEEDBACK_ENV_VAR, "   ")
        assert post(client, ROW).status_code == 503

    def test_the_admin_token_does_not_open_ingest(self, client, admin, monkeypatch):
        """Every node holds the feedback token, so the two secrets must not be
        interchangeable in either direction."""
        monkeypatch.delenv(FEEDBACK_ENV_VAR, raising=False)
        assert post(client, ROW, token=ADMIN_TOKEN).status_code == 503


class TestRejectsBadCredentials:
    def test_no_authorization_header(self, client, configured):
        assert post(client, ROW, token=None).status_code == 401

    def test_wrong_token(self, client, configured):
        assert post(client, ROW, token="not-the-token").status_code == 401

    def test_the_admin_token_is_not_the_feedback_token(self, client, configured, admin):
        assert post(client, ROW, token=ADMIN_TOKEN).status_code == 401

    def test_the_guard_runs_before_the_body_is_judged(self, client, monkeypatch, tmp_path):
        """An unauthenticated caller gets 503/401 whatever it posts, so junk
        from the open internet never reaches the parser or the disk."""
        monkeypatch.delenv(FEEDBACK_ENV_VAR, raising=False)
        assert post(client, {"junk": True}, token=None).status_code == 503
        assert not (tmp_path / "feedback.db").exists()


class TestRejectsBadBodies:
    def test_junk_is_a_422(self, client, configured):
        assert post(client, {"hello": "world"}).status_code == 422

    def test_an_unknown_field_is_a_422(self, client, configured):
        """A misspelled field that was quietly dropped would weigh a row with
        no evidence in it, and nothing would ever say so."""
        assert post(client, {**ROW, "max_detection": 41}).status_code == 422

    def test_out_of_range_frequency(self, client, configured):
        assert post(client, {**ROW, "fc_hz": 98.7}).status_code == 422

    def test_an_unknown_outcome(self, client, configured):
        assert post(client, {**ROW, "outcome": "vibes"}).status_code == 422

    def test_observed_is_archive_only(self, client, configured):
        assert post(client, {**ROW, "outcome": "observed"}).status_code == 422

    def test_batch_is_capped(self, client, configured):
        """The whole body is parsed and inserted inside one request on a single
        worker, so an unbounded list stalls every other caller."""
        assert post(client, [ROW] * 101).status_code == 422

    def test_nothing_in_a_rejected_batch_is_stored(self, client, configured):
        assert post(client, [ROW, {**ROW, "outcome": "vibes"}]).status_code == 422
        assert tower_feedback.summary(10) == []


class TestStores:
    def test_a_single_row(self, client, configured):
        r = post(client, ROW)
        assert r.status_code == 200
        assert r.json() == {"stored": 1}
        assert tower_feedback.summary(10)[0]["rows"] == 1

    def test_a_batch(self, client, configured):
        rows = [ROW, {**ROW, "node_id": "node-2"}, {**ROW, "node_id": "node-3"}]
        assert post(client, rows).json() == {"stored": 3}

        summary = tower_feedback.summary(10)
        assert summary[0]["rows"] == 3
        assert summary[0]["nodes"] == 3

    def test_an_archive_row(self, client, configured):
        archive = {
            "node_id": "node-1",
            "rx_lat": 34.05,
            "rx_lon": -118.25,
            "tx_lat": 34.23,
            "tx_lon": -118.06,
            "fc_hz": 98_700_000.0,
            "source": "archive",
            "outcome": "observed",
            "verified_range_p85_km": 130.0,
            "adsb_match_rate": 0.8,
            "snr_median_db": 11.5,
            "hours_observed": 12.0,
            "observed_at": "2026-09-01T12:00:00Z",
        }
        assert post(client, archive).json() == {"stored": 1}
        assert tower_feedback.summary(10)[0]["weight"] == 12.0

    def test_an_empty_batch_stores_nothing(self, client, configured):
        assert post(client, []).json() == {"stored": 0}

    def test_scheme_is_case_insensitive(self, client, configured):
        r = client.post(
            "/api/feedback/tower-outcome",
            json=ROW,
            headers={"Authorization": f"bearer {TOKEN}"},
        )
        assert r.status_code == 200


class TestSummary:
    def test_requires_the_admin_token_not_the_feedback_one(self, client, configured, admin):
        r = client.get("/api/feedback/summary", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 401

    def test_closed_when_the_admin_token_is_unset(self, client, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert client.get("/api/feedback/summary").status_code == 503

    def test_reports_what_was_stored(self, client, configured, admin):
        post(client, [ROW, {**ROW, "node_id": "node-2", "callsign": "KXYZ"}])
        r = client.get("/api/feedback/summary", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["towers"][0]["rows"] == 2
        assert body["towers"][0]["callsigns"] == ["KABC", "KXYZ"]
        assert body["towers"][0]["mean_multiplier"] == pytest.approx(1.5)

    def test_limit_is_bounded(self, client, admin):
        assert (
            client.get(
                "/api/feedback/summary?limit=0",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            ).status_code
            == 422
        )
