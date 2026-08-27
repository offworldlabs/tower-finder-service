"""Tests for tower-finding and helper functions."""

import unittest.mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from core.auth import ENV_VAR
from tests._helpers import device, system


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

    def test_unknown_region_raises(self):
        """Paris (48.85, 2.35) is not in a supported region — must raise 422
        rather than silently falling through to 'us'."""
        from fastapi import HTTPException

        from routes.towers import _detect_source

        with pytest.raises(HTTPException):
            _detect_source(48.85, 2.35)


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

    def test_unmapped_location_returns_422(self, client):
        # source defaults to "auto"; Paris is outside US/CA/AU, so _detect_source
        # raises before any external fetch. The specific detail distinguishes this
        # from a request-validation 422.
        r = client.get("/api/towers?lat=48.85&lon=2.35")
        assert r.status_code == 422
        assert "not in a supported region" in r.json()["detail"]


# ── Config endpoints ─────────────────────────────────────────────────────────


class TestTowerConfig:
    def test_get_config(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200

    def test_update_config_too_large_returns_413(self, client, monkeypatch):
        """PUT /api/config with a body > 1 MB → 413 before writing to disk.

        Authenticated, since the admin guard now runs ahead of the size check.
        """
        monkeypatch.setenv(ENV_VAR, "token-for-size-check")
        huge_body = {"data": "x" * 1_100_000}
        r = client.put(
            "/api/config",
            json=huge_body,
            headers={"Authorization": "Bearer token-for-size-check"},
        )
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


# ── TV-band gating by region (ATSC allowlist) ────────────────────────────────


def _raw_device(freq_mhz, lat, lon, callsign):
    """Raw device dict shaped like Maprad/FCC output for process_and_rank.

    Thin wrapper over the shared factory with a strong EIRP so these towers
    clear the sensitivity filter regardless of distance.
    """
    return device(freq_mhz, lat, lon, callsign=callsign, eirp=10000)  # watts


def _raw_system(devices):
    return system(devices, licence_type="Broadcast")


class TestTvBandGatingByRegion:
    # Sydney query point; devices placed within ~a few km so they pass radius.
    _SYD_LAT = -33.87
    _SYD_LON = 151.21

    def _au_mixed_systems(self):
        return [
            _raw_system(
                [
                    _raw_device(95.5, -33.87, 151.21, "AUFM"),  # FM
                    _raw_device(195.0, -33.87, 151.21, "AUVHF"),  # TV VHF
                    _raw_device(545.0, -33.87, 151.21, "AUUHF"),  # TV UHF
                ]
            )
        ]

    def test_au_source_returns_fm_only(self):
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=self._au_mixed_systems()),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get(f"/api/towers?lat={self._SYD_LAT}&lon={self._SYD_LON}&source=au")

        assert r.status_code == 200
        towers = r.json()["towers"]
        assert len(towers) > 0
        bands = {t["band"] for t in towers}
        assert bands == {"FM"}, f"AU (non-ATSC) must yield FM only, got {bands}"

    def test_us_source_includes_tv(self):
        us_systems = [
            _raw_system(
                [
                    _raw_device(95.5, 33.9, -84.6, "USFM"),  # FM
                    _raw_device(195.0, 33.9, -84.6, "USVHF"),  # TV VHF
                    _raw_device(545.0, 33.9, -84.6, "USUHF"),  # TV UHF
                ]
            )
        ]
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=us_systems),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=us")

        assert r.status_code == 200
        bands = {t["band"] for t in r.json()["towers"]}
        assert bands & {"VHF", "UHF"}, f"US (ATSC) must include TV, got {bands}"

    def test_post_au_source_excludes_tv(self):
        payload = {
            "lat": self._SYD_LAT,
            "lon": self._SYD_LON,
            "source": "au",
            "measurements": [
                {
                    "freq_mhz": 195.0,
                    "snr_db": 30.0,
                    "obw_fraction": 0.03,
                    "score": 0.75,
                    "power_db": -62.0,
                    "band": "VHF",
                }
            ],
        }
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=self._au_mixed_systems()),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.post("/api/towers", json=payload)

        assert r.status_code == 200
        bands = {t["band"] for t in r.json()["towers"]}
        assert "VHF" not in bands and "UHF" not in bands, (
            f"AU POST path must withhold TV even for a TV measurement, got {bands}"
        )


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

    def test_unmapped_location_returns_422(self, client):
        # No source → defaults to "auto"; Paris is unmapped, so the request is
        # rejected before any external fetch.
        r = client.post("/api/towers", json={"lat": 48.85, "lon": 2.35, "measurements": []})
        assert r.status_code == 422
        assert "not in a supported region" in r.json()["detail"]

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


# ── /api/elevation ───────────────────────────────────────────────────────────


class TestElevationEndpoint:
    """The search form pre-fills altitude from this as coordinates are typed."""

    def test_returns_elevation_for_a_point(self, client):
        with unittest.mock.patch(
            "routes.towers._batch_lookup_elevations",
            new=unittest.mock.AsyncMock(return_value={(42.387080, -71.249054): 43.5}),
        ):
            r = client.get("/api/elevation", params={"lat": 42.38708028093612, "lon": -71.24905416622781})
        assert r.status_code == 200
        body = r.json()
        assert body["elevation_m"] == 43.5
        assert body["latitude"] == 42.38708028093612
        assert body["longitude"] == -71.24905416622781

    def test_upstream_failure_returns_502(self, client):
        with unittest.mock.patch(
            "routes.towers._batch_lookup_elevations",
            new=unittest.mock.AsyncMock(return_value={}),
        ):
            r = client.get("/api/elevation", params={"lat": 33.9, "lon": -84.6})
        assert r.status_code == 502
        assert "Elevation lookup failed" in r.json()["detail"]

    def test_rejects_out_of_range_latitude(self, client):
        r = client.get("/api/elevation", params={"lat": 91, "lon": 0})
        assert r.status_code == 422


# ── GET /api/towers?frequencies= (user frequencies) ──────────────────────────


class TestUserFrequencies:
    """The `frequencies` query param, which retina-server's nginx contract
    (deploy/tower-contract.sh there) asserts by its echo in the response."""

    _SYSTEMS = [
        _raw_system(
            [
                _raw_device(95.5, 33.93, -84.6, "WNEAR"),
                _raw_device(107.9, 33.93, -84.6, "WFAR"),
            ]
        )
    ]

    def _get(self, client, query):
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=self._SYSTEMS),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            return client.get(f"/api/towers?{query}")

    def test_frequencies_echoed_in_query(self, client):
        r = self._get(client, "lat=33.9&lon=-84.6&source=us&frequencies=1234.5")
        assert r.status_code == 200
        assert r.json()["query"]["user_frequencies_mhz"] == [1234.5]

    def test_contract_echo_shape(self, client):
        """Byte-for-byte what assert_tower_contract greps for: key name and
        JSON rendering both count, so this pins the serialized form."""
        r = self._get(client, "lat=33.9&lon=-84.6&source=us&frequencies=1234.5")
        assert '"user_frequencies_mhz":[1234.5]' in r.content.decode()

    def test_no_frequencies_echoes_empty_list(self, client):
        r = self._get(client, "lat=33.9&lon=-84.6&source=us")
        assert r.json()["query"]["user_frequencies_mhz"] == []

    def test_matched_tower_ranks_first_nothing_dropped(self, client):
        r = self._get(client, "lat=33.9&lon=-84.6&source=us&frequencies=107.9")
        towers = r.json()["towers"]
        assert [t["callsign"] for t in towers] == ["WFAR", "WNEAR"]
        assert towers[0]["frequency_matched"] is True
        assert towers[1]["frequency_matched"] is False

    def test_junk_frequencies_ignored(self, client):
        r = self._get(client, "lat=33.9&lon=-84.6&source=us&frequencies=abc,,-5")
        assert r.status_code == 200
        assert r.json()["query"]["user_frequencies_mhz"] == []

    def test_repeated_frequencies_key_all_count(self, client):
        """Starlette keeps only the last occurrence of a repeated key for a
        scalar-typed query param. requests (used by retina-gui's proxy) sends
        a list-valued param exactly this way, so a repeated `frequencies` key
        must not silently drop everything but the last one."""
        r = self._get(client, "lat=33.9&lon=-84.6&source=us&frequencies=95.5&frequencies=101.1")
        assert r.status_code == 200
        assert r.json()["query"]["user_frequencies_mhz"] == [95.5, 101.1]

    def test_many_repeated_frequencies_keys_do_not_error(self, client):
        """The route bounds what it does with a large repeated-key list rather
        than joining all of it; the result still respects parse_user_frequencies'
        own max_count regardless of how many occurrences were sent."""
        query = "lat=33.9&lon=-84.6&source=us&" + "&".join(f"frequencies={88 + i * 0.001}" for i in range(1000))
        r = self._get(client, query)
        assert r.status_code == 200
        assert len(r.json()["query"]["user_frequencies_mhz"]) <= 10
