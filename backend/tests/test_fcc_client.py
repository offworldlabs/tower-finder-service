"""The cache and the fan-out around the FCC's two CGI endpoints.

The line parsing itself is covered through the route tests; what is pinned
here is how often the endpoints are asked, how many states go at once, and
what a state that fails costs the ones beside it.
"""

import asyncio
import time
import unittest.mock

import httpx
import pytest

from clients import fcc

# Real licensed records, captured from the two CGI endpoints on 2026-09-16.
# Synthetic lines are no use here: both parsers read fixed pipe-delimited
# columns, and a hand-written line silently parses to None.
FM_LINE = "|WLHV        |88.1  MHz |FM |201 |DA  |                    |A  |-  |LIC    |ANNANDALE-ON-HUDSON      |NY |US |BMLED-20121106AAZ   |-      kW |0.91   kW |0.0     |116.2   |173238     |N |42 |5  |55.3  |W |73  |43 |48.4  |TRI-STATE PUBLIC COMMUNICATIONS, INC.                                       |   0.00 km |   0.00 mi |  0.00 deg |0.0    m|260.7  m|-         |0.      |-       |       m|201211061 |47d205d1c4404e1daddec1b6063ebeb7   |bfe17a7f3443473b96fcc1b6063ebeb7   |"
TV_LINE = "|W02CY-D     |-         |LPD|2   |DA  |-                   |-  |-  |LIC    |NEW YORK                 |NY |US |             0000178220|.5     kW |-         |0.0     |-       |130477     |N |40 |45 |8.1   |W |73  |58 |2.1   |HC2 STATION GROUP, INC.                                                     |   0.00 km |   0.00 mi |     0.00 deg |274.9  m|          |-         |0.        |1268297   |265.5   |178220    |45 |25076ff37dda7617017de814c4431cd0   |25076ff37dda7617017de814c5531cd8   |"


@pytest.fixture(autouse=True)
def _clean_cache():
    fcc._cache.clear()
    yield
    fcc._cache.clear()


def body(line):
    resp = unittest.mock.MagicMock()
    resp.raise_for_status = unittest.mock.MagicMock()
    resp.text = f"header line\n{line}\n"
    return resp


def error_body(status_code):
    resp = unittest.mock.MagicMock()
    resp.raise_for_status = unittest.mock.MagicMock(
        side_effect=httpx.HTTPStatusError(
            f"{status_code} error",
            request=unittest.mock.MagicMock(),
            response=unittest.mock.MagicMock(status_code=status_code),
        )
    )
    return resp


def junk_body():
    """A 200 that is not a record listing, which is what a legacy CGI serves
    while it is being worked on."""
    resp = unittest.mock.MagicMock()
    resp.raise_for_status = unittest.mock.MagicMock()
    resp.text = "<html><body>The system is temporarily unavailable.</body></html>"
    return resp


class _Upstream:
    """Stands in for transition.fcc.gov, counting and pacing the requests."""

    def __init__(self, delay=0.0, fail_states=(), error_states=(), junk_states=()):
        self.calls = []
        self.requests = []
        self.in_flight = 0
        self.peak = 0
        self._delay = delay
        self._fail_states = set(fail_states)
        self._error_states = set(error_states)
        self._junk_states = set(junk_states)

    async def get(self, url, **kwargs):
        state = kwargs["params"]["state"]
        self.calls.append((url, state))
        self.requests.append((url, kwargs["params"], kwargs.get("headers")))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self._delay)
            if state in self._fail_states:
                raise httpx.ConnectError("refused")
            if state in self._error_states:
                return error_body(500)
            if state in self._junk_states:
                return junk_body()
            return body(TV_LINE if url.endswith("tvq") else FM_LINE)
        finally:
            self.in_flight -= 1

    def patcher(self):
        ctx = unittest.mock.MagicMock()
        ctx.__aenter__ = unittest.mock.AsyncMock(return_value=self)
        ctx.__aexit__ = unittest.mock.AsyncMock(return_value=False)
        return unittest.mock.patch("httpx.AsyncClient", return_value=ctx)

    def states_asked(self):
        return [state for _, state in self.calls]


# ── Cache ────────────────────────────────────────────────────────────────────


class TestCache:
    async def test_a_second_search_over_the_same_states_asks_nothing(self):
        up = _Upstream()
        with up.patcher():
            first = await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ"])
            second = await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ"])

        assert up.states_asked() == ["NY", "NJ"]
        assert len(first) == len(second) == 2

    async def test_only_the_states_not_already_held_are_asked_for(self):
        up = _Upstream()
        with up.patcher():
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ", "CT"])

        assert up.states_asked() == ["NY", "NJ", "CT"]

    async def test_the_two_endpoints_are_held_apart_for_one_state(self):
        """Same state code, different database. Sharing a key would serve TV
        records to an FM search."""
        up = _Upstream()
        with up.patcher():
            tv = await fcc.fetch_fcc_tv_stations(0, 0, states=["NY"])
            fm = await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])

        assert len(up.calls) == 2
        assert tv[0]["devices"][0]["callsign"] == "W02CY-D"
        assert fm[0]["devices"][0]["callsign"] == "WLHV"

    async def test_a_state_that_failed_is_asked_again_next_time(self):
        up = _Upstream(fail_states=["NJ"])
        with up.patcher():
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ"])
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ"])

        assert up.states_asked().count("NJ") == 2, "a failure must not be cached"
        assert up.states_asked().count("NY") == 1

    async def test_an_entry_past_its_life_is_fetched_again(self):
        up = _Upstream()
        with up.patcher():
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])
            expired = time.monotonic() - 1
            fcc._cache[("NY", "fm")] = (expired, fcc._cache[("NY", "fm")][1])
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])

        assert up.states_asked() == ["NY", "NY"]

    def test_the_cache_is_bounded_and_evicts_the_oldest(self):
        for i in range(fcc._CACHE_MAX_ENTRIES + 3):
            fcc._cache_put((f"S{i}", "fm"), "")

        assert len(fcc._cache) == fcc._CACHE_MAX_ENTRIES
        assert ("S0", "fm") not in fcc._cache
        assert (f"S{fcc._CACHE_MAX_ENTRIES + 2}", "fm") in fcc._cache

    async def test_each_search_gets_device_records_of_its_own(self):
        """The cache holds the response text, not parsed records, so nothing
        downstream can reach back into a 24-hour entry and change it."""
        up = _Upstream()
        with up.patcher():
            first = await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])
            second = await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])

        assert first[0]["devices"][0] == second[0]["devices"][0]
        assert first[0]["devices"][0] is not second[0]["devices"][0]


# ── Fan-out ──────────────────────────────────────────────────────────────────


class TestFanOut:
    async def test_states_go_together_rather_than_one_after_another(self):
        up = _Upstream(delay=0.05)
        states = ["NY", "NJ", "CT", "PA"]

        with up.patcher():
            await fcc.fetch_fcc_fm_stations(0, 0, states=states)

        assert len(up.calls) == 4
        assert up.peak > 1, "the states were queried one after another"

    async def test_the_fan_out_is_capped(self):
        """transition.fcc.gov is a slow legacy CGI on a .gov host. Asking it
        for a dozen states at once is how a search earns a rate limit."""
        up = _Upstream(delay=0.02)
        states = [f"S{i}" for i in range(12)]

        with up.patcher():
            await fcc.fetch_fcc_fm_stations(0, 0, states=states)

        assert up.peak <= fcc._MAX_CONCURRENT_QUERIES

    async def test_one_state_failing_does_not_cost_the_others(self):
        up = _Upstream(fail_states=["NJ"])
        with up.patcher():
            systems = await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ", "CT"])

        assert len(systems) == 2, "the states that answered must still be returned"

    async def test_every_state_failing_returns_nothing_rather_than_raising(self):
        """The route turns an exception here into a 502 for the whole search;
        the existing behaviour is to log each state and carry on."""
        up = _Upstream(fail_states=["NY", "NJ"])
        with up.patcher():
            assert await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ"]) == []


# ── The request itself ───────────────────────────────────────────────────────


class TestRequestShape:
    """The whole of how these two requests are built moved in one change, and
    the endpoints answer a different dataset for a different `status` or a
    missing `chan`. Reading the same records back is not evidence of that."""

    async def test_the_fm_query_is_built_as_the_endpoint_expects(self):
        up = _Upstream()
        with up.patcher():
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])

        url, params, headers = up.requests[0]
        assert url == fcc._FM_URL
        assert params == {"list": "4", "state": "NY", "city": "", "type": "4", "status": "3"}
        assert headers == {"User-Agent": "TowerFinder/1.0"}

    async def test_the_tv_query_is_built_as_the_endpoint_expects(self):
        up = _Upstream()
        with up.patcher():
            await fcc.fetch_fcc_tv_stations(0, 0, states=["NY"])

        url, params, headers = up.requests[0]
        assert url == fcc._TV_URL
        assert params == {"list": "4", "state": "NY", "city": "", "chan": "0", "type": "4", "status": "3"}
        assert headers == {"User-Agent": "TowerFinder/1.0"}

    async def test_the_client_carries_the_timeout(self):
        up = _Upstream()
        with up.patcher() as client_cls:
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])

        assert client_cls.call_args.kwargs["timeout"] == fcc._TIMEOUT_S


# ── Answers that are not listings ────────────────────────────────────────────


class TestUnusableBodies:
    async def test_an_http_error_is_skipped_and_not_cached(self):
        up = _Upstream(error_states=["NJ"])
        with up.patcher():
            systems = await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ"])
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ"])

        assert len(systems) == 1, "the state that answered must still be returned"
        assert up.states_asked().count("NJ") == 2

    async def test_a_200_that_is_not_a_listing_is_never_held_for_a_day(self):
        """A legacy CGI answers a maintenance page with a 200. Every US state
        has licensed stations, so a body with no records is always wrong, and
        keeping it would blank that state until tomorrow."""
        up = _Upstream(junk_states=["NJ"])
        with up.patcher():
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ"])
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY", "NJ"])

        assert ("NJ", "fm") not in fcc._cache
        assert up.states_asked().count("NJ") == 2
        assert up.states_asked().count("NY") == 1


# ── The gate and the loop it belongs to ──────────────────────────────────────


class TestFanOutAcrossLoops:
    def test_a_second_event_loop_still_reaches_every_state(self):
        """A module-level Semaphore binds to the loop that first contends on
        it and raises on every other one. Caught as an upstream failure, that
        reads as the FCC being down and silently shortens the tower list."""
        states = [f"S{i}" for i in range(8)]

        def run_once():
            up = _Upstream(delay=0.01)
            with up.patcher():
                systems = asyncio.run(fcc.fetch_fcc_fm_stations(0, 0, states=states))
            fcc._cache.clear()
            return len(systems), up

        first, _ = run_once()
        second, up = run_once()

        assert first == len(states)
        assert second == len(states), "the second loop lost states to the gate"
        assert up.peak <= fcc._MAX_CONCURRENT_QUERIES, "the cap must survive the new loop"


class TestCacheReporting:
    async def test_an_expired_entry_is_not_reported_as_served_from_cache(self, caplog):
        """The line exists so an operator can tell "the FCC answered" from
        "the FCC has not been reached since yesterday". Counting entries that
        are merely present would report a state as cached in the same breath
        as fetching it."""
        up = _Upstream()
        with up.patcher():
            await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])
            fcc._cache[("NY", "fm")] = (time.monotonic() - 1, fcc._cache[("NY", "fm")][1])
            with caplog.at_level("INFO", logger="clients.fcc"):
                await fcc.fetch_fcc_fm_stations(0, 0, states=["NY"])

        assert "0 state(s) served from cache, 1 fetched" in caplog.text


class TestParseIsolation:
    async def test_an_unreadable_record_costs_its_own_line_and_no_more(self, caplog):
        """The parse used to sit inside the per-state handler. Outside it, an
        exception reaches _fetch_raw_towers, which answers 502 for the whole
        of /api/towers, so one bad line in one state would fail the search
        and, through the deploy probe, the release behind it."""
        good = fcc._parse_fm_line(FM_LINE)
        assert good is not None

        up = _Upstream()
        with up.patcher():
            with unittest.mock.patch(
                "clients.fcc._parse_fm_line",
                side_effect=[RuntimeError("a column moved"), good],
            ):
                with caplog.at_level("WARNING", logger="clients.fcc"):
                    systems = await fcc.fetch_fcc_fm_stations(0, 0, states=["NJ", "NY"])

        assert len(systems) == 1, "the state that parsed must still be returned"
        assert "unreadable" in caplog.text
        assert "NJ" in caplog.text
