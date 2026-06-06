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
    assert resp.json() == {"status": "ok"}
