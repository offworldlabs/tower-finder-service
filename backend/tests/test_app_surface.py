"""What the application serves, as opposed to what the edge sends about it."""

import pytest
from app import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Built against a real dist, so the SPA catch-all exists. CI's backend job
    runs no frontend build, and without one the routes under test are absent
    and every assertion here holds vacuously."""
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><div id=root></div>")
    monkeypatch.setenv("TOWER_FINDER_FRONTEND_DIST", str(tmp_path))
    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def test_the_schema_is_published(client):
    """On the body: the catch-all answers every path 200, so a status check
    would pass with the schema unpublished."""
    assert "paths" in client.get("/openapi.json").json()


def test_a_nul_byte_in_the_path_does_not_500(client):
    """resolve() raises ValueError where is_file() would have swallowed it, on
    a catch-all about to sit on a public ingress."""
    assert client.get("/x%00y").status_code < 500


def test_an_unknown_api_path_is_a_404_not_the_shell(client):
    assert client.get("/api/nope").status_code == 404
    assert client.get("/nope").status_code == 200  # client routes still reach the SPA
