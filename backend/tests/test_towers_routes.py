"""Tests for tower-finding and helper functions."""

import unittest.mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── _detect_source ───────────────────────────────────────────────────────────


class TestDetectSource:
    def test_us_mainland(self):
        from routes.towers import _detect_source

        assert _detect_source(34.05, -118.25) == "us"

    def test_australia(self):
        from routes.towers import _detect_source

        assert _detect_source(-33.87, 151.21) == "au"

    def test_canada(self):
        from routes.towers import _detect_source

        assert _detect_source(45.42, -75.69) == "ca"

    def test_us_northern_tier_not_misclassified_as_canada(self):
        """Amherst, MA (42.2687, -72.6713) — same longitude band as Canada
        but south of the real border; previously misclassified as 'ca'."""
        from routes.towers import _detect_source

        assert _detect_source(42.2687, -72.6713) == "us"

    def test_toronto_is_canada(self):
        """Toronto (43.6532, -79.3832) sits south of a flat 45°N cutoff but
        is still Canada — the real border dips around the Great Lakes."""
        from routes.towers import _detect_source

        assert _detect_source(43.6532, -79.3832) == "ca"

    def test_windsor_is_canada(self):
        """Windsor, ON (42.3149, -83.0364) is south of Detroit, MI — a flat
        latitude threshold can't separate them; polygon lookup can."""
        from routes.towers import _detect_source

        assert _detect_source(42.3149, -83.0364) == "ca"

    def test_northern_maine_is_us(self):
        """Fort Kent, ME (47.2380, -68.5905) sits north of 45°N but is US —
        the border bulges north around the Maine/Quebec line."""
        from routes.towers import _detect_source

        assert _detect_source(47.2380, -68.5905) == "us"

    def test_hawaii(self):
        from routes.towers import _detect_source

        assert _detect_source(21.31, -157.86) == "us"

    def test_alaska(self):
        from routes.towers import _detect_source

        assert _detect_source(64.2, -152.5) == "us"

    def test_unknown_defaults_to_us(self):
        from routes.towers import _detect_source

        assert _detect_source(48.85, 2.35) == "us"  # Paris → falls through to us


# ── Tower search validation ──────────────────────────────────────────────────


class TestTowerSearch:
    def test_missing_lat_lon(self, client):
        r = client.get("/api/towers")
        assert r.status_code == 422  # Missing required query params

    def test_invalid_source(self, client):
        r = client.get("/api/towers?lat=33.45&lon=-112.07&source=invalid")
        assert r.status_code == 400
        assert "Invalid source" in r.json()["detail"]

    def test_lat_out_of_range(self, client):
        r = client.get("/api/towers?lat=100&lon=0")
        assert r.status_code == 422


# ── Config endpoints ─────────────────────────────────────────────────────────


class TestTowerConfig:
    def test_get_config(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200

    def test_update_config_too_large_returns_413(self, client):
        """PUT /api/config with a body > 1 MB → 413 before writing to disk."""
        huge_body = {"data": "x" * 1_100_000}
        r = client.put("/api/config", json=huge_body)
        assert r.status_code == 413
        assert "too large" in r.json()["detail"].lower()


# ── _batch_lookup_elevations ─────────────────────────────────────────────────


def _make_httpx_mock(get_return=None, get_side_effect=None):
    """Return a patch context manager that intercepts httpx.AsyncClient."""
    mock_client = unittest.mock.AsyncMock()
    mock_client.get = unittest.mock.AsyncMock(return_value=get_return, side_effect=get_side_effect)
    mock_ctx = unittest.mock.MagicMock()
    mock_ctx.__aenter__ = unittest.mock.AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = unittest.mock.AsyncMock(return_value=False)
    return unittest.mock.patch("httpx.AsyncClient", return_value=mock_ctx)


class TestBatchLookupElevations:
    async def test_empty_list_returns_empty_dict(self):
        from routes.towers import _batch_lookup_elevations

        result = await _batch_lookup_elevations([])
        assert result == {}

    async def test_http_success_returns_elevation(self):
        from routes.towers import _batch_lookup_elevations

        mock_resp = unittest.mock.MagicMock()
        mock_resp.raise_for_status = unittest.mock.MagicMock()
        mock_resp.json.return_value = {"elevation": [123.4]}

        with _make_httpx_mock(get_return=mock_resp):
            result = await _batch_lookup_elevations([(33.9, -84.6)])

        assert result == {(33.9, -84.6): 123.4}

    async def test_http_timeout_returns_empty_dict(self):
        from routes.towers import _batch_lookup_elevations

        with _make_httpx_mock(get_side_effect=httpx.TimeoutException("timed out")):
            result = await _batch_lookup_elevations([(33.9, -84.6)])

        assert result == {}

    async def test_http_500_error_returns_empty_dict(self):
        from routes.towers import _batch_lookup_elevations

        mock_resp = unittest.mock.MagicMock()
        mock_resp.raise_for_status = unittest.mock.MagicMock(
            side_effect=httpx.HTTPStatusError(
                "500 Server Error",
                request=unittest.mock.MagicMock(),
                response=unittest.mock.MagicMock(),
            )
        )

        with _make_httpx_mock(get_return=mock_resp):
            result = await _batch_lookup_elevations([(33.9, -84.6)])

        assert result == {}

    async def test_generic_connection_error_returns_empty_dict(self):
        from routes.towers import _batch_lookup_elevations

        with _make_httpx_mock(get_side_effect=httpx.ConnectError("connection refused")):
            result = await _batch_lookup_elevations([(33.9, -84.6)])

        assert result == {}


# ── find_towers service-error paths ─────────────────────────────────────────


class TestFindTowersServiceErrors:
    def test_fcc_succeeds_maprad_fails_returns_200(self):
        fcc_data = [
            {
                "call_sign": "TEST",
                "latitude": 33.9,
                "longitude": -84.6,
                "distance_km": 10,
                "frequency_mhz": 100.1,
            }
        ]

        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=fcc_data),
            ),
            unittest.mock.patch(
                "routes.towers.fetch_broadcast_systems",
                new=unittest.mock.AsyncMock(side_effect=Exception("Maprad down")),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=us")

        assert r.status_code == 200
        assert "towers" in r.json()

    def test_fcc_fetch_fails_returns_502(self):
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(side_effect=Exception("Network error")),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=us")

        assert r.status_code == 502

    def test_non_us_no_api_key_returns_500(self):
        with unittest.mock.patch("routes.towers.API_KEY", ""):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=au")

        assert r.status_code == 500
        assert "MAPRAD_API_KEY not configured" in r.json()["detail"]

    def test_non_us_with_api_key_fetch_fails_returns_502(self):
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_broadcast_systems",
                new=unittest.mock.AsyncMock(side_effect=Exception("AU service down")),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=au")

        assert r.status_code == 502


# ── POST /api/towers (measurement payload) ───────────────────────────────────

_VALID_MEASUREMENT = {
    "freq_mhz": 95.5,
    "snr_db": 30.0,
    "obw_fraction": 0.03,
    "score": 0.75,
    "power_db": -62.0,
    "band": "FM",
}

_VALID_PAYLOAD = {
    "lat": 33.9,
    "lon": -84.6,
    "source": "us",
    "measurements": [_VALID_MEASUREMENT],
}


class TestFindTowersWithMeasurements:
    def test_missing_lat_lon_returns_422(self, client):
        r = client.post("/api/towers", json={"measurements": []})
        assert r.status_code == 422

    def test_invalid_source_returns_400(self, client):
        payload = {**_VALID_PAYLOAD, "source": "invalid"}
        r = client.post("/api/towers", json=payload)
        assert r.status_code == 400
        assert "Invalid source" in r.json()["detail"]

    def test_empty_measurements_accepted(self, client):
        payload = {**_VALID_PAYLOAD, "measurements": []}
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=[]),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            r = client.post("/api/towers", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["query"]["measurement_count"] == 0

    def test_valid_payload_returns_200_with_measurement_count(self, client):
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=[]),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            r = client.post("/api/towers", json=_VALID_PAYLOAD)
        assert r.status_code == 200
        body = r.json()
        assert "towers" in body
        assert body["query"]["measurement_count"] == 1

    def test_measurement_obw_fraction_out_of_range_returns_422(self, client):
        bad_measurement = {**_VALID_MEASUREMENT, "obw_fraction": 1.5}
        payload = {**_VALID_PAYLOAD, "measurements": [bad_measurement]}
        r = client.post("/api/towers", json=payload)
        assert r.status_code == 422

    def test_measurement_negative_freq_returns_422(self, client):
        bad_measurement = {**_VALID_MEASUREMENT, "freq_mhz": -1.0}
        payload = {**_VALID_PAYLOAD, "measurements": [bad_measurement]}
        r = client.post("/api/towers", json=payload)
        assert r.status_code == 422

    def test_non_us_no_api_key_returns_500(self, client):
        payload = {**_VALID_PAYLOAD, "lat": -33.87, "lon": 151.21, "source": "au"}
        with unittest.mock.patch("routes.towers.API_KEY", ""):
            r = client.post("/api/towers", json=payload)
        assert r.status_code == 500
        assert "MAPRAD_API_KEY not configured" in r.json()["detail"]

    def test_source_auto_detected_from_coordinates(self, client):
        """Auto source detection should pick 'au' for Sydney coordinates."""
        payload = {
            **_VALID_PAYLOAD,
            "lat": -33.87,
            "lon": 151.21,
            "source": "auto",
        }
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=[]),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            r = client.post("/api/towers", json=payload)
        assert r.status_code == 200
        assert r.json()["query"]["source"] == "au"
