"""Tests for the admin guard on PUT /api/config.

The service is publicly reachable, so the interesting cases are the ones that
must not succeed. Where a test needs to prove the guard *admitted* a request,
it sends an oversized body: the 1 MB check runs after the dependency and before
the write, so a 413 says "authenticated, and nothing reached the disk".
"""

import pytest
from core.auth import ENV_VAR
from fastapi.testclient import TestClient

from app import app

TOKEN = "s3cret-admin-token"
# Oversized on purpose: proves the guard passed without writing config to disk.
OVERSIZED_BODY = {"data": "x" * 1_100_000}


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv(ENV_VAR, TOKEN)


class TestTokenNotConfigured:
    def test_put_is_closed_when_token_unset(self, client, monkeypatch):
        """Unset must disable the write, not disable the guard."""
        monkeypatch.delenv(ENV_VAR, raising=False)
        r = client.put("/api/config", json={"anything": True})
        assert r.status_code == 503
        assert ENV_VAR in r.json()["detail"]

    def test_blank_token_is_treated_as_unset(self, client, monkeypatch):
        """A variable set to empty string is a misconfiguration, not a secret."""
        monkeypatch.setenv(ENV_VAR, "   ")
        r = client.put("/api/config", json={"anything": True})
        assert r.status_code == 503


class TestRejectsBadCredentials:
    def test_no_authorization_header(self, client, configured):
        r = client.put("/api/config", json={"anything": True})
        assert r.status_code == 401

    def test_wrong_token(self, client, configured):
        r = client.put(
            "/api/config",
            json={"anything": True},
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert r.status_code == 401

    def test_wrong_scheme(self, client, configured):
        r = client.put(
            "/api/config",
            json={"anything": True},
            headers={"Authorization": f"Basic {TOKEN}"},
        )
        assert r.status_code == 401

    def test_bearer_with_empty_value(self, client, configured):
        r = client.put(
            "/api/config",
            json={"anything": True},
            headers={"Authorization": "Bearer   "},
        )
        assert r.status_code == 401

    def test_non_ascii_token_is_rejected_not_a_500(self, client, configured):
        """Headers are latin-1 text on the wire, so a high byte reaches the
        guard as a non-ASCII str. `compare_digest` on str raises TypeError for
        those, which would surface as a 500."""
        r = client.put(
            "/api/config",
            json={"anything": True},
            headers={"Authorization": "Bearer caf\xe9".encode("latin-1")},
        )
        assert r.status_code == 401

    def test_bare_token_without_scheme(self, client, configured):
        r = client.put(
            "/api/config",
            json={"anything": True},
            headers={"Authorization": TOKEN},
        )
        assert r.status_code == 401


class TestAcceptsValidCredentials:
    def test_valid_token_passes_the_guard(self, client, configured):
        r = client.put(
            "/api/config",
            json=OVERSIZED_BODY,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert r.status_code == 413

    def test_scheme_is_case_insensitive(self, client, configured):
        """RFC 7235 makes auth-scheme case-insensitive; the monolith's node
        bearer guard had to be fixed for exactly this."""
        r = client.put(
            "/api/config",
            json=OVERSIZED_BODY,
            headers={"Authorization": f"bearer {TOKEN}"},
        )
        assert r.status_code == 413


class TestReadPathUnchanged:
    def test_get_config_still_open(self, client, configured):
        """Only the write is gated here. Restricting the read is 86c8upz59."""
        r = client.get("/api/config")
        assert r.status_code == 200
