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
        assert r.json() == {"stored": 1, "ignored": 0}
        assert tower_feedback.summary(10)[0]["rows"] == 1

    def test_a_batch(self, client, configured):
        rows = [ROW, {**ROW, "node_id": "node-2"}, {**ROW, "node_id": "node-3"}]
        assert post(client, rows).json() == {"stored": 3, "ignored": 0}

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
        assert post(client, archive).json() == {"stored": 1, "ignored": 0}
        assert tower_feedback.summary(10)[0]["weight"] == 12.0

    def test_an_empty_batch_stores_nothing(self, client, configured):
        assert post(client, []).json() == {"stored": 0, "ignored": 0}

    def test_scheme_is_case_insensitive(self, client, configured):
        r = client.post(
            "/api/feedback/tower-outcome",
            json=ROW,
            headers={"Authorization": f"bearer {TOKEN}"},
        )
        assert r.status_code == 200


# One entry of a retina-gui calibrator run's `history`, as the node records it
# (src/calibrator.py), and the row a node builds from it. Pinned here so the
# ingest cannot drift from the shape the node actually has to hand.
CALIBRATOR_HISTORY_ENTRY = {
    "tower_name": "KABC",
    "fc": 98_700_000,
    "descent": [],
    "outcome": "unstable_overload",
    "max_evidence": 1,
    "max_detections": 7,
    "gains_tried": [],
    "dwell_seconds": 212.4,
    "final_gain_a": 41,
    "final_gain_b": 35,
    "final_lna_state": 6,
    "device_error": True,
}


def node_row(entry=CALIBRATOR_HISTORY_ENTRY, run_id="run-2026-09-11T10:00:00Z"):
    return {
        "node_id": "e7f1c2a0-mender-id",
        "run_id": run_id,
        "rx_lat": 34.05,
        "rx_lon": -118.25,
        "tx_lat": 34.23,
        "tx_lon": -118.06,
        "fc_hz": entry["fc"],
        "callsign": entry["tower_name"],
        "source": "calibration",
        "outcome": entry["outcome"],
        "max_evidence": entry["max_evidence"],
        "max_detections": entry["max_detections"],
        "duration_s": entry["dwell_seconds"],
        "gain_a": entry["final_gain_a"],
        "gain_b": entry["final_gain_b"],
        "lna_state": entry["final_lna_state"],
        "device_error": entry["device_error"],
    }


class TestNodeShapedRow:
    def test_a_row_built_from_a_calibrator_history_entry_is_accepted(self, client, configured):
        assert post(client, node_row()).json() == {"stored": 1, "ignored": 0}

    def test_every_calibrator_outcome_is_accepted(self, client, configured):
        """The outcome vocabulary is the calibrator's, verbatim. A node must
        never have to translate a verdict before it can report it."""
        for outcome in (
            "confirmed_track",
            "no_confirmed_track",
            "unstable_overload",
            "tuned",
            "tuning_not_applied",
            "skipped_no_time",
            "not_reached",
        ):
            entry = {**CALIBRATOR_HISTORY_ENTRY, "outcome": outcome}
            assert post(client, node_row(entry, run_id=f"run-{outcome}")).status_code == 200, outcome

    def test_a_tower_the_node_never_reached_has_no_gains_yet(self, client, configured):
        """not_reached entries carry no final_* keys at all; the row must not
        need them."""
        row = node_row({**CALIBRATOR_HISTORY_ENTRY, "outcome": "not_reached"})
        for key in ("gain_a", "gain_b", "lna_state", "duration_s", "max_evidence", "max_detections", "device_error"):
            del row[key]
        assert post(client, row).status_code == 200

    def test_a_long_run_id_is_a_422(self, client, configured):
        assert post(client, node_row(run_id="x" * 65)).status_code == 422


class TestIdempotentRetries:
    def test_reposting_a_run_stores_nothing_new(self, client, configured):
        """A node that times out on the post and retries must not double its
        run's weight, and the retry must succeed so the node stops."""
        run = [node_row(), node_row({**CALIBRATOR_HISTORY_ENTRY, "fc": 101_100_000, "tower_name": "KXYZ"})]
        assert post(client, run).json() == {"stored": 2, "ignored": 0}
        r = post(client, run)
        assert r.status_code == 200
        assert r.json() == {"stored": 0, "ignored": 2}
        assert sum(t["rows"] for t in tower_feedback.summary(10)) == 2

    def test_a_new_tower_in_a_known_run_is_stored(self, client, configured):
        post(client, node_row())
        later = node_row({**CALIBRATOR_HISTORY_ENTRY, "fc": 101_100_000, "tower_name": "KXYZ"})
        assert post(client, later).json() == {"stored": 1, "ignored": 0}

    def test_the_same_run_id_from_another_node_is_stored(self, client, configured):
        post(client, node_row())
        assert post(client, {**node_row(), "node_id": "another-node"}).json() == {"stored": 1, "ignored": 0}

    def test_rows_without_a_run_id_are_never_deduplicated(self, client, configured):
        """The archive job has no run; two windows on the same tower are two
        rows. The same is true of an old node that sends no run_id."""
        assert post(client, [ROW, ROW]).json() == {"stored": 2, "ignored": 0}


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
