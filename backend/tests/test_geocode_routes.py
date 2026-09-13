"""POST /api/geocode: what the search box is coded against.

The three outcomes are deliberately distinct statuses — 200, 404 and 503 — so a
client can tell "check the spelling" from "try again in a minute". The service's
own behaviour is covered in test_geocode_service.py; this is the contract.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import app
from services import geocode as geo
from tests._helpers import json_response, make_httpx_mock

CENSUS_MATCH = {
    "result": {
        "addressMatches": [
            {
                "matchedAddress": "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500",
                "coordinates": {"x": -77.0365, "y": 38.8977},
            }
        ]
    }
}
CENSUS_EMPTY = {"result": {"addressMatches": []}}
NOMINATIM_ZIP = [
    {
        "display_name": "94110, San Francisco, California, United States",
        "lat": "37.7485484",
        "lon": "-122.4184108",
        "type": "postcode",
        "addresstype": "postcode",
        "address": {"postcode": "94110"},
    }
]
NOMINATIM_CITY = [
    {
        "display_name": "San Francisco, California, United States",
        "lat": "37.7792588",
        "lon": "-122.4193286",
        "type": "administrative",
        "addresstype": "city",
        "address": {"city": "San Francisco"},
    }
]


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(autouse=True)
def _cold_cache():
    """The cache outlives a request by design, so it must not outlive a test."""
    geo._cache.clear()
    geo._nominatim_last_call = None
    yield
    geo._cache.clear()
    geo._nominatim_last_call = None


def _post(client, query):
    return client.post("/api/geocode", json={"query": query})


# ── The three outcomes ───────────────────────────────────────────────────────


class TestGeocodeRoute:
    def test_a_street_address_comes_back_from_census(self, client):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_MATCH)]):
            r = _post(client, "1600 Pennsylvania Ave NW, Washington, DC 20500")

        assert r.status_code == 200
        assert r.json() == {
            "query": "1600 Pennsylvania Ave NW, Washington, DC 20500",
            "latitude": 38.8977,
            "longitude": -77.0365,
            "matched_address": "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500",
            "provider": "census",
            "precision": "street",
        }

    def test_a_zip_falls_through_to_nominatim(self, client):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response(NOMINATIM_ZIP)]):
            r = _post(client, "94110")

        assert r.status_code == 200
        body = r.json()
        assert (body["provider"], body["precision"]) == ("nominatim", "postcode")

    def test_a_city_is_a_locality(self, client):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response(NOMINATIM_CITY)]):
            r = _post(client, "San Francisco, CA")

        assert r.status_code == 200
        assert r.json()["precision"] == "locality"

    def test_the_echoed_query_is_the_stripped_one(self, client):
        """The client pairs the answer with what it sent, so the echo has to be
        the string the lookup actually used."""
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_MATCH)]):
            r = _post(client, "  1600 Pennsylvania Ave NW  ")

        assert r.json()["query"] == "1600 Pennsylvania Ave NW"

    def test_nobody_knows_the_address_is_a_404(self, client):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response([])]):
            r = _post(client, "zzzzqqq nowhere")

        assert r.status_code == 404
        assert r.json() == {"detail": "No match for that address"}

    def test_an_unreachable_upstream_is_a_503(self, client):
        with make_httpx_mock(get_side_effect=httpx.TimeoutException("timed out")):
            r = _post(client, "1600 Pennsylvania Ave NW")

        assert r.status_code == 503
        assert r.json() == {"detail": "Address lookup is unavailable right now"}

    def test_the_fallback_alone_is_still_a_200(self, client):
        with make_httpx_mock(get_side_effect=[httpx.ConnectError("refused"), json_response(NOMINATIM_CITY)]):
            r = _post(client, "San Francisco, CA")

        assert r.status_code == 200
        assert r.json()["provider"] == "nominatim"

    def test_the_endpoint_is_open(self, client):
        """No bearer token, unlike PUT /api/config: it exposes nothing but two
        public geocoders, and the guards are rate limits, not auth."""
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_MATCH)]):
            assert _post(client, "1600 Pennsylvania Ave NW").status_code == 200


# ── Input validation ─────────────────────────────────────────────────────────


class TestGeocodeValidation:
    @pytest.mark.parametrize("query", ["", "   ", "\t\n"])
    def test_an_empty_query_is_rejected(self, client, query):
        """Stripped before it is measured, so whitespace is empty."""
        assert _post(client, query).status_code == 422

    def test_an_overlong_query_is_rejected(self, client):
        assert _post(client, "x" * 201).status_code == 422
        assert _post(client, "x" * 200).status_code != 422

    def test_padding_does_not_count_towards_the_limit(self, client):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_MATCH)]):
            r = _post(client, "  " + "x" * 199 + "  ")
        assert r.status_code != 422

    def test_an_unknown_key_is_rejected(self, client):
        """extra="forbid", so a client sending `adress` finds out at once
        rather than getting a 422 about a missing `query`."""
        r = client.post("/api/geocode", json={"query": "somewhere", "adress": "typo"})
        assert r.status_code == 422

    def test_a_missing_body_is_rejected(self, client):
        assert client.post("/api/geocode", json={}).status_code == 422

    def test_no_upstream_is_touched_by_a_rejected_query(self, client):
        """Validation runs before the fan-out, so a flood of junk costs the
        public geocoders nothing."""
        patcher = make_httpx_mock(get_side_effect=[json_response(CENSUS_MATCH)])
        with patcher:
            assert _post(client, " ").status_code == 422
        patcher.mock_client.get.assert_not_awaited()


class TestGeocodeSchema:
    def test_the_route_is_published(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        assert "post" in paths["/api/geocode"]
