"""Tests for tower-finding and helper functions."""

import unittest.mock

import pytest
from fastapi.testclient import TestClient

from app import app
from core.auth import ENV_VAR
from tests._helpers import (
    CONSUMER_FIELDS,
    CONSUMER_NULLABLE_FIELDS,
    device,
    get_towers,
    make_httpx_mock,
    post_towers,
    status_error_response,
    system,
)


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


# ── find_towers service-error paths ─────────────────────────────────────────


class TestFindTowersServiceErrors:
    def test_fcc_fetch_fails_returns_502(self):
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(side_effect=Exception("Network error")),
            ),
            unittest.mock.patch(
                "services.elevation.lookup_many",
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
                "services.elevation.lookup_many",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=au")

        assert r.status_code == 502


class _GraphQLUpstream:
    """maprad.io at the httpx boundary: every query gets the same JSON body.

    Stands in for ``httpx.AsyncClient`` (constructed, then entered as a
    context manager) so the whole path from the route through the client's
    fan-out is exercised, not just the route's exception mapping.
    """

    def __init__(self, body_for):
        self._body_for = body_for
        self.queries = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None, headers=None):
        self.queries.append(json["query"])
        body = self._body_for(json["query"])
        response = unittest.mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = body
        return response


_CA_NOT_AUTHORIZED = "READ access to 'source' [ca] is not authorized."
_TORONTO = (43.6532, -79.3832)


def _refusal(query):
    return {
        "errors": [{"message": _CA_NOT_AUTHORIZED, "extensions": {"classification": "DataFetchingException"}}],
        "data": {"systems": None},
    }


class TestMapradRefusalReachesTheCaller:
    """A query maprad.io refuses must surface as a 502 naming the cause,
    never as a 200 with zero towers."""

    def test_refusal_from_the_client_maps_to_502_with_the_upstream_message(self):
        from clients.maprad import MapradQueryError

        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_broadcast_systems",
                new=unittest.mock.AsyncMock(side_effect=MapradQueryError("ca", _CA_NOT_AUTHORIZED)),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get(f"/api/towers?lat={_TORONTO[0]}&lon={_TORONTO[1]}&source=ca")

        assert r.status_code == 502
        assert r.json()["detail"] == f"Maprad rejected the ca query: {_CA_NOT_AUTHORIZED}"

    def test_graphql_error_on_the_first_page_is_a_502_end_to_end(self):
        upstream = _GraphQLUpstream(_refusal)
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch("clients.maprad.httpx.AsyncClient", upstream),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get(f"/api/towers?lat={_TORONTO[0]}&lon={_TORONTO[1]}&source=ca")

        assert r.status_code == 502
        assert r.json()["detail"] == f"Maprad rejected the ca query: {_CA_NOT_AUTHORIZED}"
        assert upstream.queries, "the client was never reached"

    def test_post_maps_a_refusal_the_same_way(self):
        upstream = _GraphQLUpstream(_refusal)
        payload = {"lat": _TORONTO[0], "lon": _TORONTO[1], "source": "ca", "measurements": [_VALID_MEASUREMENT]}
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch("clients.maprad.httpx.AsyncClient", upstream),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.post("/api/towers", json=payload)

        assert r.status_code == 502
        assert _CA_NOT_AUTHORIZED in r.json()["detail"]

    def test_other_failures_keep_the_generic_502(self):
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_broadcast_systems",
                new=unittest.mock.AsyncMock(side_effect=RuntimeError("socket closed")),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get(f"/api/towers?lat={_TORONTO[0]}&lon={_TORONTO[1]}&source=ca")

        assert r.status_code == 502
        assert r.json()["detail"] == "External service unavailable. Please try again."


class TestCanadianSearchEndToEnd:
    """A Toronto search through the real client, with maprad.io faked at httpx."""

    @staticmethod
    def _ckfm(query):
        # CKFM-FM as maprad.io's CA index holds it: 99.9 MHz from the CN Tower,
        # ERP in dBW under a W label. Only the FM leg finds anything.
        if 'values: "FM"' not in query:
            return {"data": {"systems": {"edges": [], "pageInfo": {"hasNextPage": False}}}}
        node = {
            "id": "ckfm",
            "licence": {"type": "Broadcast", "subtype": "FM"},
            "devices": [
                {
                    "callsign": "CKFM-FM",
                    "frequency": 99.9,
                    "eirp": 45.58469,
                    "transmitPower": 17000.0,
                    "antennaHeight": 469.7,
                    "location": {"name": "Toronto", "state": "ON", "geom": "POINT(-79.3871 43.6426)"},
                }
            ],
        }
        return {"data": {"systems": {"edges": [{"cursor": "c1", "node": node}], "pageInfo": {"hasNextPage": False}}}}

    def test_toronto_returns_the_station_at_its_real_power(self):
        upstream = _GraphQLUpstream(self._ckfm)
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch("clients.maprad.httpx.AsyncClient", upstream),
            unittest.mock.patch(
                "services.elevation.lookup_many",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get(f"/api/towers?lat={_TORONTO[0]}&lon={_TORONTO[1]}&source=ca")

        assert r.status_code == 200
        towers = r.json()["towers"]
        assert [t["callsign"] for t in towers] == ["CKFM-FM"]
        # 45.58 dBW is 75.6 dBm; read as watts it would have been 46.6.
        assert towers[0]["eirp_dbm"] == pytest.approx(75.6, abs=0.1)
        # The subtype legs found data, so no fallback query was spent.
        assert not any("licence_type" in q for q in upstream.queries)


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
                "services.elevation.lookup_many",
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
                "services.elevation.lookup_many",
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
                "services.elevation.lookup_many",
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


class TestUsDoesNotConsultMaprad:
    """US searches are served from the FCC database alone.

    Maprad's only US dataset is the FCC ULS licence system: land mobile,
    microwave, maritime and broadcast auxiliary. It carries no broadcast
    stations, and the land-mobile records it does return rank as television
    because T-Band public safety sits on former UHF channels.
    """

    _FCC_SYSTEMS = [_raw_system([_raw_device(545.0, 33.9, -84.6, "WTEST")])]

    def test_get_leaves_maprad_alone(self):
        maprad = unittest.mock.AsyncMock(return_value=[])
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=self._FCC_SYSTEMS),
            ),
            unittest.mock.patch("routes.towers.fetch_broadcast_systems", new=maprad),
            unittest.mock.patch(
                "services.elevation.lookup_many",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.get("/api/towers?lat=33.9&lon=-84.6&source=us")

        assert r.status_code == 200
        maprad.assert_not_awaited()
        assert [t["callsign"] for t in r.json()["towers"]] == ["WTEST"]

    def test_post_leaves_maprad_alone(self):
        maprad = unittest.mock.AsyncMock(return_value=[])
        with (
            unittest.mock.patch("routes.towers.API_KEY", "fake-key"),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=self._FCC_SYSTEMS),
            ),
            unittest.mock.patch("routes.towers.fetch_broadcast_systems", new=maprad),
            unittest.mock.patch(
                "services.elevation.lookup_many",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                r = c.post("/api/towers", json=_VALID_PAYLOAD)

        assert r.status_code == 200
        maprad.assert_not_awaited()


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
                "services.elevation.lookup_many",
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
                "services.elevation.lookup_many",
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
                "services.elevation.lookup_many",
                new=unittest.mock.AsyncMock(return_value={}),
            ),
        ):
            r = client.post("/api/towers", json=payload)
        assert r.status_code == 200
        assert r.json()["query"]["source"] == "au"


# ── What the response says about how it was ranked ───────────────────────────


class TestRankingDiagnostics:
    """Both routes say which ordering the caller got, and POST says what the
    sweep calibrated. A client should not have to infer either from the rows."""

    _LAT, _LON = 33.9, -84.6

    # One mast with two UHF channels plus a tower elsewhere, so the diversity
    # pass has something to do.
    _SYSTEMS = [
        system(
            [
                device(515.0, 34.05, -84.6, callsign="MAST1", eirp=100_000),
                device(521.0, 34.05, -84.6, callsign="MAST2", eirp=100_000),
                device(527.0, 33.9, -84.35, callsign="EAST", eirp=3_000),
            ],
            licence_type="Broadcast",
        )
    ]

    @pytest.fixture(autouse=True)
    def _shipped(self):
        """Run these against the config the image ships, not whatever overlay
        the developer's runtime volume holds."""
        import json

        from services import tower_ranking

        saved = {name: getattr(tower_ranking, name) for name in tower_ranking.CONFIG_SETTINGS}
        with (tower_ranking._SOURCE_DEFAULT_DIR / "tower_config.json").open() as f:
            tower_ranking.apply_config(json.load(f))
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(tower_ranking, name, value)

    def _measurements(self):
        return [
            {"freq_mhz": f, "band": "UHF", "snr_db": None, "obw_fraction": None, "score": 0.9, "power_db": p}
            for f, p in ((515.0, -30.0), (521.0, -32.0), (527.0, -45.0))
        ]

    def test_get_names_the_ordering(self, client):
        r = get_towers(client, f"lat={self._LAT}&lon={self._LON}&source=us", self._SYSTEMS)

        assert r.status_code == 200
        assert r.json()["query"]["ranking"] == "expected_area_mmr"

    def test_get_names_the_plain_sort_when_diversity_is_off(self, client):
        from services import tower_ranking

        tower_ranking.DIVERSITY = {**tower_ranking.DIVERSITY, "enabled": False}

        r = get_towers(client, f"lat={self._LAT}&lon={self._LON}&source=us", self._SYSTEMS)

        assert r.json()["query"]["ranking"] == "expected_area"

    def test_post_names_the_ordering_and_the_calibration(self, client):
        payload = {"lat": self._LAT, "lon": self._LON, "source": "us", "measurements": self._measurements()}

        r = post_towers(client, payload, self._SYSTEMS)

        assert r.status_code == 200
        query = r.json()["query"]
        assert query["ranking"] == "expected_area_mmr"
        assert isinstance(query["calibration_offset_db"], float)
        assert query["calibrated_towers"] == 3
        # The pre-existing keys are untouched.
        assert query["measurement_count"] == 3
        assert query["source"] == "us"

    def test_post_reports_no_calibration_when_the_sweep_is_too_thin(self, client):
        one = [self._measurements()[0]]
        payload = {"lat": self._LAT, "lon": self._LON, "source": "us", "measurements": one}

        r = post_towers(client, payload, self._SYSTEMS)

        query = r.json()["query"]
        assert query["calibration_offset_db"] is None
        assert query["calibrated_towers"] == 0

    def test_get_rows_carry_the_diversity_annotations(self, client):
        r = get_towers(client, f"lat={self._LAT}&lon={self._LON}&source=us", self._SYSTEMS)

        towers = {t["callsign"]: t for t in r.json()["towers"]}
        assert towers["MAST1"]["site_id"] == towers["MAST2"]["site_id"]
        assert towers["MAST1"]["site_channels"] == 2
        assert towers["EAST"]["site_channels"] == 1
        # The mast's second channel is the one that pays for the repeat.
        assert [t["callsign"] for t in r.json()["towers"]] == ["MAST1", "EAST", "MAST2"]
        assert towers["MAST2"]["diversity_penalty"] == pytest.approx(0.7)

    def test_post_rows_say_where_the_direct_path_came_from(self, client):
        payload = {"lat": self._LAT, "lon": self._LON, "source": "us", "measurements": self._measurements()}

        r = post_towers(client, payload, self._SYSTEMS)

        for t in r.json()["towers"]:
            assert t["direct_power_source"] == "measured"
            assert t["measurement_quality"] == pytest.approx(0.95)


class TestConsumerFieldContract:
    """retina-gui and retina-spectrum read both routes.

    Phase 2 only adds fields and changes order, so every field those consumers
    read must still be there, with the same type, on GET and on POST alike.
    """

    _LAT, _LON = 33.9, -84.6
    _SYSTEMS = [system([device(515.0, 34.05, -84.6, callsign="WTEST", eirp=100_000)], licence_type="Broadcast")]
    _MEASUREMENT = {
        "freq_mhz": 515.0,
        "band": "UHF",
        "snr_db": None,
        "obw_fraction": None,
        "score": 0.9,
        "power_db": -30.0,
    }

    def _assert_contract(self, tower):
        for field, expected_type in CONSUMER_FIELDS.items():
            assert field in tower, f"{field} is missing"
            assert isinstance(tower[field], expected_type), f"{field} is {type(tower[field]).__name__}"
        for field in CONSUMER_NULLABLE_FIELDS:
            assert field in tower, f"{field} is missing"

    def test_get_row_keeps_every_consumed_field(self, client):
        r = get_towers(client, f"lat={self._LAT}&lon={self._LON}&source=us", self._SYSTEMS)

        assert r.status_code == 200
        self._assert_contract(r.json()["towers"][0])

    def test_post_row_keeps_every_consumed_field(self, client):
        payload = {"lat": self._LAT, "lon": self._LON, "source": "us", "measurements": [self._MEASUREMENT]}

        r = post_towers(client, payload, self._SYSTEMS)

        assert r.status_code == 200
        self._assert_contract(r.json()["towers"][0])

    def test_ranks_stay_contiguous_from_one(self, client):
        systems = [
            system(
                [
                    device(515.0, 34.05, -84.6, callsign="A", eirp=100_000),
                    device(521.0, 34.05, -84.6, callsign="B", eirp=100_000),
                    device(527.0, 33.9, -84.35, callsign="C", eirp=3_000),
                ],
                licence_type="Broadcast",
            )
        ]

        r = get_towers(client, f"lat={self._LAT}&lon={self._LON}&source=us", systems)

        ranks = [t["rank"] for t in r.json()["towers"]]
        assert ranks == list(range(1, len(ranks) + 1))

    def test_the_query_and_count_keys_are_still_there(self, client):
        r = get_towers(client, f"lat={self._LAT}&lon={self._LON}&source=us", self._SYSTEMS)

        body = r.json()
        assert body["count"] == len(body["towers"])
        assert set(body["query"]) >= {
            "latitude",
            "longitude",
            "altitude_m",
            "radius_km",
            "source",
            "user_frequencies_mhz",
        }


# ── /api/elevation ───────────────────────────────────────────────────────────


class TestElevationEndpoint:
    """The search form pre-fills altitude from this as coordinates are typed."""

    def test_returns_elevation_for_a_point(self, client):
        with unittest.mock.patch(
            "services.elevation.lookup",
            new=unittest.mock.AsyncMock(return_value=43.5),
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
        from services.elevation import ElevationUnavailable

        with unittest.mock.patch(
            "services.elevation.lookup",
            new=unittest.mock.AsyncMock(side_effect=ElevationUnavailable("open-meteo unreachable")),
        ):
            r = client.get("/api/elevation", params={"lat": 33.9, "lon": -84.6})
        assert r.status_code == 503
        assert "Elevation service unavailable" in r.json()["detail"]

    def test_a_point_with_no_data_returns_404(self, client):
        """open-meteo answered; it just has no DEM coverage here. That is a
        valid answer about the point, not a failure of the service."""
        with unittest.mock.patch(
            "services.elevation.lookup",
            new=unittest.mock.AsyncMock(return_value=None),
        ):
            r = client.get("/api/elevation", params={"lat": 33.9, "lon": -84.6})
        assert r.status_code == 404
        assert "No elevation data" in r.json()["detail"]

    def test_a_fault_of_our_own_is_a_500_not_a_503(self, client):
        """The smoke check passes a 503 carrying this detail, so a bug of ours
        must never produce one: it would gate a broken route green."""
        with unittest.mock.patch(
            "services.elevation.lookup",
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
                "services.elevation.lookup_many",
                new=unittest.mock.AsyncMock(side_effect=KeyError(0)),
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


# ── query.altitude_m ─────────────────────────────────────────────────────────


class TestResolvedAltitude:
    """The node's own ground level, echoed back so the SPA can show what the
    search actually ran with."""

    # Far enough from the query point below to tell the two coordinates apart
    # in the elevations the lookup is asked for.
    RAW = [system([device(95.5, 33.9, -84.6, callsign="WFAR", eirp=10000)], "Broadcast", "FM")]
    ELEVATIONS = {(34.0, -84.5): 250.0, (33.9, -84.6): 300.0}

    def _get(self, client, elevations, **params):
        lookup_many = unittest.mock.AsyncMock(return_value=elevations)
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=self.RAW),
            ),
            unittest.mock.patch("services.elevation.lookup_many", new=lookup_many),
        ):
            r = client.get("/api/towers", params={"lat": 34.0, "lon": -84.5, "source": "us", **params})
        return r, lookup_many

    def test_an_altitude_the_caller_gave_is_left_alone(self, client):
        r, lookup_many = self._get(client, self.ELEVATIONS, altitude=150)

        assert r.json()["query"]["altitude_m"] == 150
        assert (34.0, -84.5) not in lookup_many.call_args.args[0], "the query point must not be looked up"

    def test_a_missing_altitude_is_resolved_from_the_query_point(self, client):
        r, _ = self._get(client, self.ELEVATIONS)

        assert r.json()["query"]["altitude_m"] == 250.0

    def test_the_query_point_is_asked_for_before_the_towers(self, client):
        """It rides in the same request as the towers now, and a chunk that
        fails abandons the ones after it, so trailing the towers would lose the
        altitude on exactly the large searches that need chunking."""
        _, lookup_many = self._get(client, self.ELEVATIONS)

        assert lookup_many.call_args.args[0][0] == (34.0, -84.5)

    def test_an_elevation_we_cannot_get_leaves_the_altitude_at_the_default(self, client):
        r, _ = self._get(client, {})

        assert r.json()["query"]["altitude_m"] == 0

    def test_towers_still_answer_when_open_meteo_refuses(self, client):
        """Through the real lookup rather than a stubbed one: enrichment is
        best-effort, so a rate-limited upstream costs the elevations and
        nothing else."""
        with (
            unittest.mock.patch("routes.towers.API_KEY", ""),
            unittest.mock.patch(
                "routes.towers.fetch_fcc_broadcast_systems",
                new=unittest.mock.AsyncMock(return_value=self.RAW),
            ),
            make_httpx_mock(get_return=status_error_response(429)),
        ):
            r = client.get("/api/towers", params={"lat": 34.0, "lon": -84.5, "source": "us"})

        assert r.status_code == 200
        assert r.json()["towers"], "towers must still be returned"
        assert all(t["elevation_m"] is None for t in r.json()["towers"])
        assert r.json()["query"]["altitude_m"] == 0
