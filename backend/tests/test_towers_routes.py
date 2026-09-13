"""Tests for tower-finding and helper functions."""

import unittest.mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from core.auth import ENV_VAR
from tests._helpers import device, get_towers, make_httpx_mock, status_error_response, system


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

        with make_httpx_mock(get_return=mock_resp):
            result = await _batch_lookup_elevations([(33.9, -84.6)])

        assert result == {(33.9, -84.6): 123.4}

    async def test_http_timeout_raises_unavailable(self):
        from routes.towers import ElevationUnavailable, _batch_lookup_elevations

        with make_httpx_mock(get_side_effect=httpx.TimeoutException("timed out")):
            with pytest.raises(ElevationUnavailable):
                await _batch_lookup_elevations([(33.9, -84.6)])

    async def test_http_500_error_raises_unavailable(self):
        from routes.towers import ElevationUnavailable, _batch_lookup_elevations

        with make_httpx_mock(get_return=status_error_response(500)):
            with pytest.raises(ElevationUnavailable):
                await _batch_lookup_elevations([(33.9, -84.6)])

    async def test_rate_limiting_raises_unavailable(self):
        """429 is open-meteo's own limit, not a fault in the request: waiting
        fixes it, so it must not read as a broken route."""
        from routes.towers import ElevationUnavailable, _batch_lookup_elevations

        with make_httpx_mock(get_return=status_error_response(429)):
            with pytest.raises(ElevationUnavailable):
                await _batch_lookup_elevations([(33.9, -84.6)])

    @pytest.mark.parametrize("status", [400, 404, 422])
    async def test_a_rejected_request_is_not_reported_as_the_dependency(self, status):
        """A 4xx is open-meteo rejecting the request we built, so it is ours to
        answer for. Reporting it as the dependency would answer 503, which the
        post-deploy smoke passes on, gating a permanently broken route green."""
        from routes.towers import ElevationUnavailable, _batch_lookup_elevations

        with make_httpx_mock(get_return=status_error_response(status)):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await _batch_lookup_elevations([(33.9, -84.6)])
        assert not isinstance(exc_info.value, ElevationUnavailable)

    async def test_generic_connection_error_raises_unavailable(self):
        from routes.towers import ElevationUnavailable, _batch_lookup_elevations

        with make_httpx_mock(get_side_effect=httpx.ConnectError("connection refused")):
            with pytest.raises(ElevationUnavailable):
                await _batch_lookup_elevations([(33.9, -84.6)])

    async def test_unreadable_body_raises_unavailable(self):
        """open-meteo answered, with something that will not read as numbers.
        Still the dependency's failure, not ours."""
        from routes.towers import ElevationUnavailable, _batch_lookup_elevations

        mock_resp = unittest.mock.MagicMock()
        mock_resp.raise_for_status = unittest.mock.MagicMock()
        mock_resp.json.return_value = {"elevation": ["not a number"]}

        with make_httpx_mock(get_return=mock_resp):
            with pytest.raises(ElevationUnavailable):
                await _batch_lookup_elevations([(33.9, -84.6)])

    async def test_a_fault_of_our_own_is_not_reported_as_the_dependency(self):
        """The classification is what /api/elevation's 503 and the post-deploy
        smoke both rest on: a bug in the handling above must not reach either
        of them wearing open-meteo's name."""
        from routes.towers import ElevationUnavailable, _batch_lookup_elevations

        mock_resp = unittest.mock.MagicMock()
        mock_resp.raise_for_status = unittest.mock.MagicMock()
        # A dict where the code indexes a list: the shape a refactor gets wrong.
        mock_resp.json.return_value = {"elevation": {"0": 123.4}}

        with make_httpx_mock(get_return=mock_resp):
            with pytest.raises(Exception) as exc_info:  # noqa: PT011
                await _batch_lookup_elevations([(33.9, -84.6)])
        assert not isinstance(exc_info.value, ElevationUnavailable)


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

    def test_dependency_failure_returns_503(self, client):
        """Separate from the 404 below so a caller, and the post-deploy smoke,
        can tell an upstream outage from a route that has stopped working."""
        from routes.towers import ElevationUnavailable

        with unittest.mock.patch(
            "routes.towers._batch_lookup_elevations",
            new=unittest.mock.AsyncMock(side_effect=ElevationUnavailable("open-meteo unreachable")),
        ):
            r = client.get("/api/elevation", params={"lat": 33.9, "lon": -84.6})
        assert r.status_code == 503
        assert "Elevation service unavailable" in r.json()["detail"]

    def test_a_point_with_no_data_returns_404(self, client):
        """open-meteo answered; it just has no DEM coverage here. That is a
        valid answer about the point, not a failure of the service."""
        with unittest.mock.patch(
            "routes.towers._batch_lookup_elevations",
            new=unittest.mock.AsyncMock(return_value={}),
        ):
            r = client.get("/api/elevation", params={"lat": 33.9, "lon": -84.6})
        assert r.status_code == 404
        assert "No elevation data" in r.json()["detail"]

    def test_a_fault_of_our_own_is_a_500_not_a_503(self, client):
        """The smoke check passes a 503 carrying this detail, so a bug of ours
        must never produce one: it would gate a broken route green."""
        with unittest.mock.patch(
            "routes.towers._batch_lookup_elevations",
            new=unittest.mock.AsyncMock(side_effect=KeyError(0)),
        ):
            r = client.get("/api/elevation", params={"lat": 33.9, "lon": -84.6})
        assert r.status_code == 500

    def test_a_request_open_meteo_rejects_is_a_500_not_a_503(self, client):
        """The whole route, not just the classification: open-meteo answering
        400 means the request we build is wrong, and a 400 is what a renamed
        parameter or a malformed coordinate list gets. Answering 503 would have
        the smoke check report the route healthy and pass the deploy."""
        with make_httpx_mock(get_return=status_error_response(400)):
            r = client.get("/api/elevation", params={"lat": 33.45, "lon": -112.07})
        assert r.status_code == 500
        assert "Elevation service unavailable" not in r.text

    def test_towers_still_answers_when_the_lookup_itself_faults(self, client):
        """Best-effort covers a fault of ours too: /api/towers answers with the
        towers and a null elevation, not a 500."""
        raw = [system([device(95.5, 33.9, -84.6, callsign="WFAR", eirp=10000)], "Broadcast", "FM")]

        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=raw),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(side_effect=KeyError(0)),
            ),
        ):
            r = client.get("/api/towers", params={"lat": 33.9, "lon": -84.6, "source": "us"})
        assert r.status_code == 200
        assert r.json()["towers"], "towers must still be returned"
        assert all(t["elevation_m"] is None for t in r.json()["towers"])

    def test_towers_still_answers_when_elevation_is_unavailable(self, client):
        """Enrichment is best-effort: a dependency outage must not take the
        tower list down with it."""
        from routes.towers import ElevationUnavailable

        raw = [system([device(95.5, 33.9, -84.6, callsign="WFAR", eirp=10000)], "Broadcast", "FM")]

        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=raw),
            ),
            unittest.mock.patch(
                "routes.towers._batch_lookup_elevations",
                new=unittest.mock.AsyncMock(side_effect=ElevationUnavailable("down")),
            ),
        ):
            r = client.get("/api/towers", params={"lat": 33.9, "lon": -84.6, "source": "us"})
        assert r.status_code == 200
        assert r.json()["towers"], "towers must still be returned"
        assert all(t["elevation_m"] is None for t in r.json()["towers"])

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
        return get_towers(client, query, self._SYSTEMS)

    def test_frequencies_echoed_in_query(self, client):
        r = self._get(client, "lat=33.9&lon=-84.6&source=us&frequencies=1234.5")
        assert r.status_code == 200
        assert r.json()["query"]["user_frequencies_mhz"] == [1234.5]

    def test_contract_echo_shape(self, client):
        """Byte-for-byte what assert_tower_contract greps for: key name and
        JSON rendering both count, so this pins the serialized form.

        Pinned the same way by SMOKE_FREQ_ECHO in deploy/smoke-common.sh and by
        TOWER_CONTRACT_ECHO in retina-server's deploy/tower-contract.sh. Change
        the shape, change all three.
        """
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

    def test_many_valid_repeated_frequencies_keys_capped_at_ten(self, client):
        """Fifteen valid occurrences, well inside every bound, so the
        response's cap of ten is the only thing that can be limiting it."""
        many = "&".join(f"frequencies={90 + i}.5" for i in range(15))
        r = self._get(client, f"lat=33.9&lon=-84.6&source=us&{many}")
        assert r.status_code == 200
        assert len(r.json()["query"]["user_frequencies_mhz"]) == 10

    def test_many_repeated_junk_keys_do_not_hide_a_valid_one(self, client):
        """2000 unparseable occurrences must be absorbed without an error, and
        without costing the valid value behind them: junk never trips the
        response cap, so nothing but a ceiling on what is examined could drop
        it, and there is none."""
        junk = "&".join(f"frequencies=notafreq{i}" for i in range(2000))
        query = f"lat=33.9&lon=-84.6&source=us&{junk}&frequencies=95.5"
        r = self._get(client, query)
        assert r.status_code == 200
        assert r.json()["query"]["user_frequencies_mhz"] == [95.5]

    def test_oversized_occurrence_does_not_discard_a_valid_sibling(self, client):
        """An oversized occurrence must not swallow a valid one beside it, in
        either order: the answer cannot depend on the order the caller sent
        them in."""
        huge = "9" * 5000
        for query in (
            f"lat=33.9&lon=-84.6&source=us&frequencies={huge}&frequencies=95.5",
            f"lat=33.9&lon=-84.6&source=us&frequencies=95.5&frequencies={huge}",
        ):
            r = self._get(client, query)
            assert r.status_code == 200
            assert r.json()["query"]["user_frequencies_mhz"] == [95.5]

    def test_comma_separated_value_in_a_repeated_key_still_splits(self, client):
        """The two spellings compose: an occurrence may itself carry the
        comma-separated form the frontend sends."""
        r = self._get(client, "lat=33.9&lon=-84.6&source=us&frequencies=95.5,101.1&frequencies=88.1")
        assert r.status_code == 200
        assert r.json()["query"]["user_frequencies_mhz"] == [95.5, 101.1, 88.1]
