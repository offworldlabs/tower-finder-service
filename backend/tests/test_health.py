"""Tests for the liveness health endpoint."""

import pytest
from fastapi.testclient import TestClient

from app import app


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_health_returns_ok(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "environment" in resp.json()


def test_health_reports_configured_environment(monkeypatch, client):
    monkeypatch.setenv("TOWER_FINDER_ENV", "staging")
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "staging"}


def test_health_environment_defaults_to_unknown(monkeypatch, client):
    monkeypatch.delenv("TOWER_FINDER_ENV", raising=False)
    response = client.get("/api/health")
    assert response.json()["environment"] == "unknown"
