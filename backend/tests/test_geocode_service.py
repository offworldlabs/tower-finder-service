"""Tests for the two-provider address lookup behind POST /api/geocode.

Everything here patches ``httpx.AsyncClient``, so the providers' own request
building and payload parsing are exercised without leaving the process.
"""

import asyncio
import logging
import time
import unittest.mock

import httpx
import pytest

from services import geocode as geo
from tests._helpers import json_response, make_httpx_mock, status_error_response

CENSUS_MATCH = {
    "result": {
        "addressMatches": [
            {
                "matchedAddress": "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500",
                "coordinates": {"x": -77.0365298765, "y": 38.8976763210},
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
        "address": {"postcode": "94110", "city": "San Francisco"},
    }
]
NOMINATIM_CITY = [
    {
        "display_name": "San Francisco, California, United States",
        "lat": "37.7792588",
        "lon": "-122.4193286",
        "type": "administrative",
        "addresstype": "city",
        "address": {"city": "San Francisco", "state": "California"},
    }
]
NOMINATIM_HOUSE = [
    {
        "display_name": "1 Dr Carlton B Goodlett Pl, San Francisco, California",
        "lat": "37.779",
        "lon": "-122.419",
        "type": "house",
        "addresstype": "building",
        "address": {"house_number": "1", "road": "Dr Carlton B Goodlett Place"},
    }
]


@pytest.fixture(autouse=True)
def _clean_module_state():
    """Cache and throttle are process-wide by design, so each test starts from
    a cold one rather than inheriting the previous test's."""
    geo._cache.clear()
    geo._nominatim_last_call = None
    yield
    geo._cache.clear()
    geo._nominatim_last_call = None


# ── Provider selection ───────────────────────────────────────────────────────


class TestProviderOrder:
    async def test_census_match_wins_without_asking_nominatim(self):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_MATCH)]) as _:
            result = await geo.geocode("1600 Pennsylvania Ave NW, Washington, DC")

        assert result.provider == "census"
        assert result.precision == "street"
        assert result.matched_address.startswith("1600 PENNSYLVANIA")
        # Rounded on the way out, so the response is stable and comparable.
        assert (result.latitude, result.longitude) == (38.897676, -77.03653)

    async def test_census_empty_falls_through_to_nominatim(self):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response(NOMINATIM_ZIP)]):
            result = await geo.geocode("94110")

        assert result.provider == "nominatim"
        assert result.precision == "postcode"
        assert (result.latitude, result.longitude) == (37.748548, -122.418411)

    async def test_a_city_is_a_locality(self):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response(NOMINATIM_CITY)]):
            result = await geo.geocode("San Francisco, CA")

        assert (result.provider, result.precision) == ("nominatim", "locality")

    async def test_a_house_number_is_a_street(self):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response(NOMINATIM_HOUSE)]):
            result = await geo.geocode("1 Dr Carlton B Goodlett Pl")

        assert (result.provider, result.precision) == ("nominatim", "street")

    async def test_both_empty_is_no_match(self):
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response([])]):
            assert await geo.geocode("zzzzqqq nowhere") is None

    async def test_census_down_but_nominatim_answers(self):
        """The fallback exists for exactly this: one upstream being unreachable
        is not an outage while the other still knows the address."""
        with make_httpx_mock(
            get_side_effect=[httpx.ConnectError("refused"), json_response(NOMINATIM_CITY)],
        ):
            result = await geo.geocode("San Francisco, CA")

        assert result.provider == "nominatim"

    async def test_both_down_is_unavailable(self):
        with make_httpx_mock(get_side_effect=httpx.TimeoutException("timed out")):
            with pytest.raises(geo.GeocodeUnavailable):
                await geo.geocode("anywhere")

    async def test_a_bad_status_is_unavailable(self):
        with make_httpx_mock(get_side_effect=[status_error_response(500), status_error_response(429)]):
            with pytest.raises(geo.GeocodeUnavailable):
                await geo.geocode("anywhere")

    async def test_a_malformed_body_is_unavailable_not_a_crash(self):
        """A payload that is not the documented shape is the upstream failing,
        so it must read as an outage rather than escape as a 500."""
        with make_httpx_mock(get_side_effect=[json_response({"result": "nonsense"}), json_response("nonsense")]):
            with pytest.raises(geo.GeocodeUnavailable):
                await geo.geocode("anywhere")

    async def test_one_provider_down_and_the_other_blank_is_unavailable(self):
        """Not a 404: with Census unreachable the address may well exist, and
        claiming otherwise would cache a wrong answer for a day."""
        with make_httpx_mock(get_side_effect=[httpx.ConnectError("refused"), json_response([])]):
            with pytest.raises(geo.GeocodeUnavailable):
                await geo.geocode("1600 Pennsylvania Ave NW")

    async def test_providers_are_a_sequence_the_route_never_sees(self):
        """The interface a third, keyed provider would be slotted into."""
        assert list(geo.PROVIDERS) == [geo._census, geo._nominatim]


# ── The Nominatim request itself ─────────────────────────────────────────────


class TestNominatimRequest:
    async def test_it_identifies_itself_and_filters_to_the_country(self):
        """Both are conditions of Nominatim's usage policy, not decoration."""
        patcher = make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response(NOMINATIM_CITY)])
        with patcher:
            await geo.geocode("San Francisco, CA")

        _, kwargs = patcher.mock_client.get.call_args
        assert kwargs["headers"]["User-Agent"].startswith("tower-finder-service (")
        assert kwargs["params"]["countrycodes"] == geo.NOMINATIM_COUNTRY_CODES == "us"
        assert kwargs["params"]["limit"] == 1

    async def test_the_contact_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(geo.CONTACT_ENV_VAR, "ops@example.com")
        assert geo._user_agent() == "tower-finder-service (ops@example.com)"

    async def test_an_unset_contact_still_identifies_the_service(self, monkeypatch):
        monkeypatch.delenv(geo.CONTACT_ENV_VAR, raising=False)
        assert "github.com/offworldlabs/tower-finder-service" in geo._user_agent()

    async def test_calls_are_held_a_second_apart(self):
        """Patched sleep, not a real one: what is under test is the delay the
        throttle asks for, and a test suite must not wait out a rate limit."""
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with unittest.mock.patch("services.geocode.asyncio.sleep", new=fake_sleep):
            await geo._throttle_nominatim()
            first = geo._nominatim_last_call
            await geo._throttle_nominatim()

        assert slept and slept[0] == pytest.approx(1.0, abs=0.05)
        assert geo._nominatim_last_call >= first

    async def test_the_first_call_is_not_delayed(self):
        with unittest.mock.patch("services.geocode.asyncio.sleep", new=unittest.mock.AsyncMock()) as sleep:
            await geo._throttle_nominatim()
        sleep.assert_not_called()

    async def test_concurrent_callers_do_not_leave_together(self):
        """The sleep is taken under the lock; released first, every waiter
        would read the same stale timestamp and burst."""
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with unittest.mock.patch("services.geocode.asyncio.sleep", new=fake_sleep):
            await asyncio.gather(*(geo._throttle_nominatim() for _ in range(3)))

        assert len(slept) == 2  # the first goes straight through, the rest wait


# ── Cache ────────────────────────────────────────────────────────────────────


class TestCache:
    async def test_a_repeat_query_does_not_reach_the_upstream(self):
        patcher = make_httpx_mock(get_side_effect=[json_response(CENSUS_MATCH)])
        with patcher:
            first = await geo.geocode("1600 Pennsylvania Ave NW")
            # Same address, differently typed: the key is case-folded and has
            # its whitespace collapsed, so this is the same entry.
            second = await geo.geocode("  1600   PENNSYLVANIA ave NW ")

        assert first == second
        assert patcher.mock_client.get.await_count == 1

    async def test_a_definitive_no_match_is_cached(self):
        patcher = make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response([])])
        with patcher:
            assert await geo.geocode("zzzzqqq nowhere") is None
            assert await geo.geocode("zzzzqqq nowhere") is None

        assert patcher.mock_client.get.await_count == 2  # both providers, once

    async def test_a_failure_is_never_cached(self):
        with make_httpx_mock(get_side_effect=httpx.ConnectError("refused")):
            with pytest.raises(geo.GeocodeUnavailable):
                await geo.geocode("1600 Pennsylvania Ave NW")

        patcher = make_httpx_mock(get_side_effect=[json_response(CENSUS_MATCH)])
        with patcher:
            result = await geo.geocode("1600 Pennsylvania Ave NW")

        assert result.provider == "census"
        assert patcher.mock_client.get.await_count == 1

    async def test_an_expired_entry_is_re_fetched(self):
        key = geo._cache_key("somewhere")
        geo._cache[key] = (time.monotonic() - 1, None)
        assert geo._cache_get(key) is geo._MISS
        assert key not in geo._cache

    def test_the_cache_is_bounded_and_evicts_the_oldest(self):
        for i in range(geo._CACHE_MAX_ENTRIES + 5):
            geo._cache_put(f"key {i}", None)

        assert len(geo._cache) == geo._CACHE_MAX_ENTRIES
        assert "key 0" not in geo._cache
        assert f"key {geo._CACHE_MAX_ENTRIES + 4}" in geo._cache

    def test_refreshing_an_entry_moves_it_off_the_eviction_line(self):
        geo._cache_put("first", None)
        geo._cache_put("second", None)
        geo._cache_put("first", None)
        assert list(geo._cache) == ["second", "first"]


# ── Privacy ──────────────────────────────────────────────────────────────────


class TestLogging:
    async def test_the_query_is_never_logged(self, caplog):
        """An address typed into a public box is the nearest thing this service
        handles to personal data; the outcome and the provider are all an
        operator needs."""
        secret = "221B Baker Street Apt 4 Somebodys Home"
        caplog.set_level(logging.DEBUG)

        with make_httpx_mock(get_side_effect=[json_response(CENSUS_MATCH)]):
            await geo.geocode(secret)
        with make_httpx_mock(get_side_effect=[json_response(CENSUS_EMPTY), json_response([])]):
            await geo.geocode(secret + " nowhere")
        with make_httpx_mock(get_side_effect=httpx.ConnectError("refused")):
            with pytest.raises(geo.GeocodeUnavailable):
                await geo.geocode(secret + " unreachable")

        captured = caplog.text.lower()
        assert captured  # the assertions below are worthless against no output
        assert "baker street" not in captured
        assert "221b" not in captured
        assert "census" in captured and "nominatim" in captured
