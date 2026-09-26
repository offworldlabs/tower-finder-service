"""The subtype fan-out and cursor walk behind maprad.io.

Maprad answers for AU and CA only, and it is the sole source for both, so
what is pinned here is that every broadcast subtype is asked, that they go
together rather than in turn, what a subtype that fails costs the ones
beside it, and that a refused query is never passed off as an empty area.
"""

import asyncio
import re
import unittest.mock

import httpx
import pytest

from clients import maprad

_SUBTYPE_RE = re.compile(r'values: "([^"]+)"')
_CURSOR_RE = re.compile(r'after: "([^"]*)"')
_PAGE_SIZE_RE = re.compile(r"first: (\d+)")
_SOURCE_RE = re.compile(r'source: "([^"]*)"')

_AU_SUBTYPES = maprad._BROADCAST_SUBTYPES["au"]
_CA_SUBTYPES = maprad._BROADCAST_SUBTYPES["ca"]

# What maprad.io says, inside an HTTP 200, to a key without access to a source.
_NOT_AUTHORIZED = "READ access to 'source' [ca] is not authorized."


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Upstream:
    """Stands in for maprad.io, counting and pacing the queries."""

    def __init__(
        self,
        pages=1,
        delay=0.01,
        fail_subtypes=(),
        error_subtypes=(),
        stuck_cursor=False,
        records=None,
        page_size_limit=None,
        empty_subtypes=(),
        error_on_page=None,
        error_message="cannot query field",
        fallback_records=0,
        devices=(),
    ):
        self.queries = []
        self.in_flight = 0
        self.peak = 0
        self._pages = pages
        self._delay = delay
        self._fail = set(fail_subtypes)
        self._errors = set(error_subtypes)
        self._stuck_cursor = stuck_cursor
        # How many systems a subtype holds, when the point of the test is
        # density rather than a page count. Pages are then served at whatever
        # size is asked for, as the real API does.
        self._records = records
        # maprad.io refuses a page larger than 30 with a GraphQL error rather
        # than clamping, so a test can ask for that refusal to be modelled.
        self._page_size_limit = page_size_limit
        # Subtypes the source holds nothing under, as a vocabulary it does not
        # use: an empty answer, not an error.
        self._empty = set(empty_subtypes)
        # Every walk hits a GraphQL error at this page (0-based), so a test can
        # model a refusal that arrives after earlier pages were served.
        self._error_on_page = error_on_page
        self._error_message = error_message
        # What the Canadian broad-licence fallback query finds.
        self._fallback_records = fallback_records
        self._devices = list(devices)

    # Constructed as httpx.AsyncClient(timeout=...) and entered as a context
    # manager, so it stands in for both.
    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    @staticmethod
    def _is_fallback(query):
        return "licence_type" in query

    @property
    def subtypes_queried(self):
        return [_SUBTYPE_RE.search(q).group(1) for q in self.queries if not self._is_fallback(q)]

    @property
    def fallback_queries(self):
        return [q for q in self.queries if self._is_fallback(q)]

    @property
    def sources_queried(self):
        return {_SOURCE_RE.search(q).group(1) for q in self.queries}

    async def post(self, url, json=None, headers=None):
        query = json["query"]
        fallback = self._is_fallback(query)
        subtype = "fallback" if fallback else _SUBTYPE_RE.search(query).group(1)
        cursor = _CURSOR_RE.search(query).group(1)
        page_size = int(_PAGE_SIZE_RE.search(query).group(1))
        self.queries.append(query)

        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            # A real await, so the three subtypes can actually overlap: a fake
            # that returns without yielding cannot show a fan-out at all.
            await asyncio.sleep(self._delay)

            if subtype in self._fail:
                raise httpx.ConnectError("maprad unreachable")
            if subtype in self._errors:
                return _Response({"errors": [{"message": self._error_message}], "data": {"systems": None}})

            if self._page_size_limit is not None and page_size > self._page_size_limit:
                return _Response(
                    {
                        "errors": [
                            {
                                "message": (
                                    f"The value of the 'first' argument ({page_size}) exceeds "
                                    f"the page size limit of {self._page_size_limit}"
                                )
                            }
                        ]
                    }
                )

            page = 0 if cursor == "" else int(cursor.rsplit("|", 1)[1]) + 1
            if self._error_on_page is not None and page >= self._error_on_page:
                return _Response({"errors": [{"message": self._error_message}], "data": {"systems": None}})
            if subtype in self._empty:
                served, has_next = 0, False
            elif fallback:
                served = max(0, min(page_size, self._fallback_records - page * page_size))
                has_next = (page + 1) * page_size < self._fallback_records
            elif self._records is None:
                served, has_next = page_size, page + 1 < self._pages
            else:
                served = max(0, min(page_size, self._records - page * page_size))
                has_next = (page + 1) * page_size < self._records
            edges = [
                {
                    "cursor": f"{subtype}|{0 if self._stuck_cursor else page}",
                    "node": {
                        "id": f"{subtype}-{page}-{i}",
                        "devices": [dict(d) for d in self._devices],
                        "licence": {"subtype": subtype},
                    },
                }
                for i in range(served)
            ]
            return _Response({"data": {"systems": {"edges": edges, "pageInfo": {"hasNextPage": has_next}}}})
        finally:
            self.in_flight -= 1


@pytest.fixture(autouse=True)
def _empty_result_cache():
    # The result cache is module state that outlives a test, and most tests
    # here ask the same point with a fresh fake upstream each time.
    maprad.clear_cache()
    yield
    maprad.clear_cache()


async def _fetch(upstream, **kwargs):
    with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
        return await maprad.fetch_broadcast_systems("fake-key", -33.87, 151.21, source="au", **kwargs)


async def _fetch_ca(upstream, **kwargs):
    # Toronto.
    with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
        return await maprad.fetch_broadcast_systems("fake-key", 43.6532, -79.3832, source="ca", **kwargs)


class TestSourceGuard:
    """Maprad serves AU and CA only; the US comes from the FCC directly."""

    async def test_us_is_refused(self):
        with pytest.raises(ValueError, match="'us'"):
            await maprad.fetch_broadcast_systems("fake-key", 33.9, -84.6, source="us")

    async def test_an_unknown_source_is_refused(self):
        with pytest.raises(ValueError, match="'nz'"):
            await maprad.fetch_broadcast_systems("fake-key", -36.85, 174.76, source="nz")

    async def test_the_guard_runs_before_any_request(self):
        upstream = _Upstream()
        with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
            with pytest.raises(ValueError):
                await maprad.fetch_broadcast_systems("fake-key", 33.9, -84.6, source="us")
        assert upstream.queries == []

    async def test_a_mixed_case_source_reaches_the_upstream_lowercased(self):
        # Maprad's source keys are lowercase; a query carrying "AU" matches
        # nothing there and comes back empty rather than raising.
        upstream = _Upstream()
        with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
            await maprad.fetch_broadcast_systems("fake-key", -33.87, 151.21, source="AU")
        assert upstream.sources_queried == {"au"}


class TestSubtypeFanOut:
    async def test_every_broadcast_subtype_is_asked(self):
        upstream = _Upstream()
        await _fetch(upstream)
        assert sorted(upstream.subtypes_queried) == sorted(_AU_SUBTYPES)

    async def test_the_subtypes_go_together_not_in_turn(self):
        upstream = _Upstream()
        await _fetch(upstream)
        assert upstream.peak == len(_AU_SUBTYPES), f"expected all subtypes in flight at once, peak was {upstream.peak}"

    async def test_results_from_every_subtype_are_returned(self):
        systems = await _fetch(_Upstream(pages=1))
        subtypes = {s["licence"]["subtype"] for s in systems}
        assert subtypes == set(_AU_SUBTYPES)


class TestPagination:
    async def test_the_cursor_walk_follows_pages(self):
        systems = await _fetch(_Upstream(pages=2))
        # Two full pages, for each of the three subtypes.
        assert len(systems) == 2 * maprad._PAGE_SIZE * len(_AU_SUBTYPES)

    async def test_the_walk_stops_when_the_upstream_says_so(self):
        upstream = _Upstream(pages=1)
        await _fetch(upstream, max_pages=10)
        assert len(upstream.queries) == len(_AU_SUBTYPES)

    async def test_max_pages_caps_the_walk(self):
        upstream = _Upstream(pages=99)
        systems = await _fetch(upstream, max_pages=2)
        assert len(upstream.queries) == 2 * len(_AU_SUBTYPES)
        assert len(systems) == 2 * maprad._PAGE_SIZE * len(_AU_SUBTYPES)

    async def test_a_cursor_that_does_not_advance_stops_the_walk(self):
        # hasNextPage stays true while the cursor repeats, which would
        # otherwise re-fetch the same page up to max_pages.
        upstream = _Upstream(pages=99, stuck_cursor=True)
        await _fetch(upstream, max_pages=10)
        assert len(upstream.queries) == 2 * len(_AU_SUBTYPES)


class TestFailureIsolation:
    async def test_one_subtype_failing_leaves_the_others(self):
        failed = _AU_SUBTYPES[0]
        systems = await _fetch(_Upstream(fail_subtypes=[failed]))
        subtypes = {s["licence"]["subtype"] for s in systems}
        assert subtypes == set(_AU_SUBTYPES) - {failed}

    async def test_graphql_errors_stop_only_their_own_subtype(self):
        broken = _AU_SUBTYPES[1]
        systems = await _fetch(_Upstream(error_subtypes=[broken]))
        subtypes = {s["licence"]["subtype"] for s in systems}
        assert subtypes == set(_AU_SUBTYPES) - {broken}

    async def test_a_partial_failure_is_logged_with_its_cause(self, caplog):
        broken = _AU_SUBTYPES[1]
        with caplog.at_level("WARNING", logger="clients.maprad"):
            await _fetch(_Upstream(error_subtypes=[broken], error_message="boom from upstream"))
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any(broken in w and "boom from upstream" in w for w in warnings), warnings

    async def test_every_subtype_failing_raises_rather_than_answering_empty(self):
        # An empty list here is indistinguishable from "no towers near this
        # point", which is how the Canadian outage went unnoticed.
        with pytest.raises(httpx.ConnectError):
            await _fetch(_Upstream(fail_subtypes=_AU_SUBTYPES))

    async def test_every_subtype_refused_raises_the_upstream_message(self):
        upstream = _Upstream(error_subtypes=_AU_SUBTYPES, error_message=_NOT_AUTHORIZED)
        with pytest.raises(maprad.MapradQueryError) as info:
            await _fetch(upstream)
        assert info.value.source == "au"
        assert info.value.upstream_message == _NOT_AUTHORIZED

    async def test_a_refusal_outranks_a_network_error_when_everything_fails(self):
        # The refusal names its own cause; a connection error says nothing
        # the operator can act on.
        upstream = _Upstream(
            fail_subtypes=_AU_SUBTYPES[:1],
            error_subtypes=_AU_SUBTYPES[1:],
            error_message=_NOT_AUTHORIZED,
        )
        with pytest.raises(maprad.MapradQueryError):
            await _fetch(upstream)


class TestGraphQLErrorsInTheWalk:
    """_paginate_query on its own: what a GraphQL error means depends on
    whether the walk had already collected anything."""

    async def _walk(self, upstream, max_pages=5):
        kwargs = {
            "source": "ca",
            "coords": "43.6532,-79.3832",
            "radius": "80",
            "devices": maprad._DEVICE_FIELDS,
            "subtype": "FM",
        }
        return await maprad._paginate_query(
            upstream, {}, maprad._SUBTYPE_QUERY, kwargs, max_pages=max_pages, page_size=maprad._PAGE_SIZE
        )

    async def test_an_error_on_the_first_page_raises(self):
        upstream = _Upstream(error_on_page=0, error_message=_NOT_AUTHORIZED)
        with pytest.raises(maprad.MapradQueryError) as info:
            await self._walk(upstream)
        assert info.value.source == "ca"
        assert _NOT_AUTHORIZED in str(info.value)

    async def test_an_error_after_earlier_pages_keeps_them(self, caplog):
        upstream = _Upstream(pages=5, error_on_page=2, error_message="internal error on large response")
        with caplog.at_level("WARNING", logger="clients.maprad"):
            systems = await self._walk(upstream)
        assert len(systems) == 2 * maprad._PAGE_SIZE
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("internal error on large response" in w and "page 3" in w for w in warnings), warnings

    async def test_a_long_upstream_message_is_cut_short(self):
        upstream = _Upstream(error_on_page=0, error_message="x" * 5000)
        with pytest.raises(maprad.MapradQueryError) as info:
            await self._walk(upstream)
        assert len(info.value.upstream_message) <= maprad._MAX_ERROR_TEXT


class TestCanada:
    """ISED's vocabulary is not ACMA's: the AU subtypes match nothing in CA."""

    async def test_canada_asks_for_its_own_subtypes(self):
        upstream = _Upstream()
        await _fetch_ca(upstream)
        assert sorted(upstream.subtypes_queried) == sorted(_CA_SUBTYPES)
        assert upstream.sources_queried == {"ca"}
        assert not set(upstream.subtypes_queried) & set(_AU_SUBTYPES)

    async def test_canada_subtypes_with_data_need_no_fallback(self):
        upstream = _Upstream(pages=1)
        systems = await _fetch_ca(upstream)
        assert upstream.fallback_queries == []
        assert {s["licence"]["subtype"] for s in systems} == set(_CA_SUBTYPES)

    async def test_empty_canadian_subtypes_fall_back_to_any_broadcast_licence(self):
        upstream = _Upstream(empty_subtypes=_CA_SUBTYPES, fallback_records=4)
        systems = await _fetch_ca(upstream)
        assert len(upstream.fallback_queries) == 1
        fallback = upstream.fallback_queries[0]
        assert 'field: licence_type, values: "Broadcast"' in fallback
        assert 'type: RANGE, values: ["54000000", "698000000"]' in fallback
        assert len(systems) == 4
        assert {s["licence"]["subtype"] for s in systems} == {"fallback"}

    async def test_the_fallback_walks_pages_too(self):
        upstream = _Upstream(empty_subtypes=_CA_SUBTYPES, fallback_records=maprad._PAGE_SIZE + 5)
        systems = await _fetch_ca(upstream)
        assert len(upstream.fallback_queries) == 2
        assert len(systems) == maprad._PAGE_SIZE + 5

    async def test_the_fallback_is_announced(self, caplog):
        with caplog.at_level("WARNING", logger="clients.maprad"):
            await _fetch_ca(_Upstream(empty_subtypes=_CA_SUBTYPES, fallback_records=1))
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("Broadcast" in w for w in warnings), warnings

    async def test_a_refused_fallback_raises(self):
        # Nothing collected and upstream refused: that is a failure, not an
        # empty area, on the fallback leg as on any other.
        upstream = _Upstream(empty_subtypes=_CA_SUBTYPES, error_subtypes=["fallback"], error_message=_NOT_AUTHORIZED)
        with pytest.raises(maprad.MapradQueryError) as info:
            await _fetch_ca(upstream)
        assert len(upstream.fallback_queries) == 1
        assert info.value.upstream_message == _NOT_AUTHORIZED

    async def test_canada_refused_outright_raises_without_a_fallback(self):
        upstream = _Upstream(error_subtypes=_CA_SUBTYPES, error_message=_NOT_AUTHORIZED)
        with pytest.raises(maprad.MapradQueryError) as info:
            await _fetch_ca(upstream)
        assert _NOT_AUTHORIZED in info.value.upstream_message
        assert upstream.fallback_queries == []

    async def test_canadian_eirp_is_read_as_dbw(self):
        # CKFM-FM, Toronto: ISED's 45.58 dBW is ~36 kW, not 45.58 W.
        upstream = _Upstream(devices=[{"callsign": "CKFM-FM", "eirp": 45.58469, "transmitPower": 17000.0}])
        systems = await _fetch_ca(upstream)
        dev = systems[0]["devices"][0]
        assert dev["eirp"] == pytest.approx(36_200, rel=0.01)
        assert dev["eirp_dbw"] == 45.58469
        assert dev["transmitPower"] == 17000.0

    async def test_a_missing_canadian_eirp_stays_missing(self):
        upstream = _Upstream(devices=[{"callsign": "CIUT-FM", "eirp": None, "transmitPower": None}])
        systems = await _fetch_ca(upstream)
        assert systems[0]["devices"][0]["eirp"] is None

    async def test_every_canadian_device_is_converted_once(self):
        # Two subtypes, one device each, fresh dicts per node: a device seen
        # twice would be raised to a power twice.
        upstream = _Upstream(pages=1, devices=[{"eirp": 30.0}])
        systems = await _fetch_ca(upstream)
        eirps = [d["eirp"] for s in systems for d in s["devices"]]
        assert len(eirps) == len(_CA_SUBTYPES) * maprad._PAGE_SIZE
        assert eirps == [pytest.approx(1000.0)] * len(eirps)


class TestAustraliaUnchanged:
    """AU works in production; the Canadian changes must not reach it."""

    async def test_australia_asks_only_its_subtypes(self):
        upstream = _Upstream()
        await _fetch(upstream)
        assert sorted(upstream.subtypes_queried) == sorted(
            ["Commercial Television", "National Broadcasting", "Commercial Radio"]
        )

    async def test_empty_australian_subtypes_do_not_fall_back(self):
        upstream = _Upstream(empty_subtypes=_AU_SUBTYPES, fallback_records=5)
        systems = await _fetch(upstream)
        assert systems == []
        assert upstream.fallback_queries == []

    async def test_australian_eirp_is_left_in_watts(self):
        upstream = _Upstream(devices=[{"callsign": "2SYD", "eirp": 246000.0}])
        systems = await _fetch(upstream)
        dev = systems[0]["devices"][0]
        assert dev["eirp"] == 246000.0
        assert "eirp_dbw" not in dev


class TestPageBudget:
    """What a single search can actually retrieve, and what it tells us when
    it could not retrieve all of it."""

    async def test_a_dense_location_is_returned_in_full(self):
        # Ninety systems to a subtype is inside what Sydney really holds, and
        # is more than one page however the walk is paged, so only a budget
        # that both pages widely and pages more than once returns the lot.
        upstream = _Upstream(records=90)
        systems = await _fetch(upstream)
        assert len(systems) == 90 * len(_AU_SUBTYPES)

    async def test_the_page_size_stays_inside_the_upstream_limit(self):
        # A page above 30 is refused outright with a GraphQL error, which now
        # fails the search; asking too big must not be what trips it.
        upstream = _Upstream(records=1, page_size_limit=30)
        systems = await _fetch(upstream)
        assert len(systems) == len(_AU_SUBTYPES)

    async def test_a_walk_cut_short_by_the_budget_says_which_subtype(self, caplog):
        upstream = _Upstream(records=10_000)
        with caplog.at_level("WARNING", logger="clients.maprad"):
            await _fetch(upstream)
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "a walk that left records behind reported nothing"
        for subtype in _AU_SUBTYPES:
            assert any(subtype in message for message in warnings), f"no warning named {subtype}"

    async def test_a_walk_that_reaches_the_end_is_quiet(self, caplog):
        upstream = _Upstream(records=3)
        with caplog.at_level("WARNING", logger="clients.maprad"):
            await _fetch(upstream)
        assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == []


class TestResultCache:
    """One search is several metered queries and ~7 s; the same search again
    inside the day is answered in-process."""

    async def test_a_repeated_search_asks_upstream_nothing(self):
        upstream = _Upstream(pages=2)
        first = await _fetch(upstream)
        asked = len(upstream.queries)
        assert asked == 2 * len(_AU_SUBTYPES)
        second = await _fetch(upstream)
        assert len(upstream.queries) == asked
        assert second == first

    async def test_a_point_within_the_rounding_shares_the_entry(self):
        # 43.6532 and 43.6541 both round to 43.65: ~100 m apart.
        upstream = _Upstream()
        with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
            await maprad.fetch_broadcast_systems("fake-key", 43.6532, -79.3832, source="ca")
            asked = len(upstream.queries)
            await maprad.fetch_broadcast_systems("fake-key", 43.6541, -79.3791, source="ca")
        assert len(upstream.queries) == asked

    async def test_the_rounded_point_is_what_upstream_is_asked(self):
        # So what the entry holds does not depend on which caller came first.
        upstream = _Upstream()
        await _fetch_ca(upstream)
        assert all('"43.65,-79.38"' in q for q in upstream.queries), upstream.queries[0]

    async def test_a_point_more_than_a_kilometre_away_misses(self):
        upstream = _Upstream()
        with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
            await maprad.fetch_broadcast_systems("fake-key", 43.6532, -79.3832, source="ca")
            asked = len(upstream.queries)
            # ~2.2 km north.
            await maprad.fetch_broadcast_systems("fake-key", 43.6732, -79.3832, source="ca")
        assert len(upstream.queries) == 2 * asked

    async def test_a_different_radius_misses(self):
        upstream = _Upstream()
        await _fetch(upstream, radius_km=80)
        asked = len(upstream.queries)
        await _fetch(upstream, radius_km=120)
        assert len(upstream.queries) == 2 * asked

    async def test_a_different_page_budget_misses(self):
        # A one-page walk is not an answer to a three-page question.
        upstream = _Upstream(pages=5)
        short = await _fetch(upstream, max_pages=1)
        longer = await _fetch(upstream, max_pages=3)
        assert len(longer) == 3 * len(short)

    async def test_a_different_source_misses(self):
        upstream = _Upstream()
        with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
            await maprad.fetch_broadcast_systems("fake-key", 45.0, -75.0, source="ca")
            await maprad.fetch_broadcast_systems("fake-key", 45.0, -75.0, source="au")
        assert upstream.sources_queried == {"ca", "au"}

    async def test_source_case_does_not_split_the_entry(self):
        # The upstream key is folded to lowercase; the cache key is the same one.
        upstream = _Upstream()
        with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
            await maprad.fetch_broadcast_systems("fake-key", 43.6532, -79.3832, source="CA")
            asked = len(upstream.queries)
            await maprad.fetch_broadcast_systems("fake-key", 43.6532, -79.3832, source="ca")
            await maprad.fetch_broadcast_systems("fake-key", 43.6532, -79.3832, source="Ca")
        assert len(upstream.queries) == asked
        assert upstream.sources_queried == {"ca"}

    async def test_an_entry_expires_after_a_day(self, monkeypatch):
        clock = [1_000.0]
        monkeypatch.setattr(maprad, "_clock", lambda: clock[0])
        upstream = _Upstream()
        await _fetch(upstream)
        asked = len(upstream.queries)

        clock[0] += maprad._CACHE_TTL_S - 1
        await _fetch(upstream)
        assert len(upstream.queries) == asked

        clock[0] += 2
        await _fetch(upstream)
        assert len(upstream.queries) == 2 * asked

    async def test_a_refused_query_is_not_held(self):
        refused = _Upstream(error_subtypes=_AU_SUBTYPES, error_message=_NOT_AUTHORIZED)
        with pytest.raises(maprad.MapradQueryError):
            await _fetch(refused)
        assert maprad._cache == {}

        # Access granted since: the next search asks again and is answered.
        upstream = _Upstream()
        systems = await _fetch(upstream)
        assert len(upstream.queries) == len(_AU_SUBTYPES)
        assert systems

    async def test_an_unreachable_upstream_is_not_held(self):
        with pytest.raises(httpx.ConnectError):
            await _fetch(_Upstream(fail_subtypes=_AU_SUBTYPES))
        assert maprad._cache == {}
        upstream = _Upstream()
        await _fetch(upstream)
        assert len(upstream.queries) == len(_AU_SUBTYPES)

    async def test_a_walk_cut_short_by_the_page_budget_is_still_held(self):
        # The budget is the answer to the question as asked, not a failure.
        upstream = _Upstream(records=10_000)
        first = await _fetch(upstream)
        asked = len(upstream.queries)
        assert await _fetch(upstream) == first
        assert len(upstream.queries) == asked

    async def test_a_caller_cannot_change_what_the_next_one_gets(self):
        upstream = _Upstream(devices=[{"callsign": "2SYD", "eirp": 246000.0}])
        first = await _fetch(upstream)
        pristine = [dict(s) for s in first]
        # What the route does to its records, and worse.
        first[0]["devices"][0]["eirp"] = 0.0
        first[0]["annotated"] = True
        first[1]["devices"].clear()
        first.pop()

        second = await _fetch(upstream)
        assert len(second) == len(pristine)
        assert second[0]["devices"][0]["eirp"] == 246000.0
        assert "annotated" not in second[0]
        assert second[1]["devices"]

        # Nor can a hit reach the one after it.
        second[0]["devices"][0]["eirp"] = 1.0
        assert (await _fetch(upstream))[0]["devices"][0]["eirp"] == 246000.0

    async def test_canadian_eirp_is_converted_once_across_hits(self):
        upstream = _Upstream(devices=[{"eirp": 30.0}])
        for _ in range(3):
            systems = await _fetch_ca(upstream)
        eirps = [d["eirp"] for s in systems for d in s["devices"]]
        assert eirps == [pytest.approx(1000.0)] * len(eirps)
        assert {d["eirp_dbw"] for s in systems for d in s["devices"]} == {30.0}
        assert len(upstream.queries) == len(_CA_SUBTYPES)

    async def test_clear_cache_empties_it(self):
        upstream = _Upstream()
        await _fetch(upstream)
        assert maprad._cache
        maprad.clear_cache()
        assert maprad._cache == {}
        await _fetch(upstream)
        assert len(upstream.queries) == 2 * len(_AU_SUBTYPES)

    async def test_the_cache_is_bounded_and_evicts_the_oldest(self):
        for i in range(maprad._CACHE_MAX_ENTRIES + 5):
            maprad._cache_put(("au", 80, 3, float(i), 0.0), "[]")
        assert len(maprad._cache) == maprad._CACHE_MAX_ENTRIES
        assert ("au", 80, 3, 0.0, 0.0) not in maprad._cache
        assert ("au", 80, 3, float(maprad._CACHE_MAX_ENTRIES + 4), 0.0) in maprad._cache

    async def test_expired_entries_are_evicted_before_live_ones(self, monkeypatch):
        clock = [1_000.0]
        monkeypatch.setattr(maprad, "_clock", lambda: clock[0])
        maprad._cache_put(("au", 80, 3, 0.0, 0.0), "[]")  # oldest, still live
        clock[0] += 10
        for i in range(1, maprad._CACHE_MAX_ENTRIES):
            maprad._cache_put(("au", 80, 3, float(i), 0.0), "[]")
        # Push the second entry past its life, then overflow by one.
        maprad._cache[("au", 80, 3, 1.0, 0.0)] = (clock[0] - 1, "[]")
        maprad._cache_put(("au", 80, 3, 999.0, 0.0), "[]")
        assert ("au", 80, 3, 1.0, 0.0) not in maprad._cache
        assert ("au", 80, 3, 0.0, 0.0) in maprad._cache

    async def test_hits_and_misses_are_logged_at_debug(self, caplog):
        upstream = _Upstream()
        with caplog.at_level("DEBUG", logger="clients.maprad"):
            await _fetch(upstream)
            await _fetch(upstream)
        debug = [r.getMessage() for r in caplog.records if r.levelname == "DEBUG"]
        assert any("cache miss" in m for m in debug), debug
        assert any("cache hit" in m for m in debug), debug


class TestInFlightSharing:
    """Two identical searches at once (a retry, a double click) pay once."""

    async def test_concurrent_identical_searches_share_one_walk(self):
        upstream = _Upstream(pages=2, delay=0.02)
        with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
            a, b = await asyncio.gather(
                maprad.fetch_broadcast_systems("fake-key", -33.87, 151.21, source="au"),
                maprad.fetch_broadcast_systems("fake-key", -33.87, 151.21, source="AU"),
            )
        assert len(upstream.queries) == 2 * len(_AU_SUBTYPES)
        assert a == b
        assert a is not b and a[0] is not b[0]
        assert maprad._in_flight == {}

    async def test_a_shared_failure_reaches_every_waiter_and_is_not_held(self):
        upstream = _Upstream(error_subtypes=_AU_SUBTYPES, error_message=_NOT_AUTHORIZED)
        with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
            results = await asyncio.gather(
                maprad.fetch_broadcast_systems("fake-key", -33.87, 151.21, source="au"),
                maprad.fetch_broadcast_systems("fake-key", -33.87, 151.21, source="au"),
                return_exceptions=True,
            )
        assert all(isinstance(r, maprad.MapradQueryError) for r in results), results
        assert len(upstream.queries) == len(_AU_SUBTYPES)
        assert maprad._cache == {}
        assert maprad._in_flight == {}

    async def test_a_cancelled_caller_does_not_cancel_the_one_beside_it(self):
        upstream = _Upstream(delay=0.05)
        with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
            first = asyncio.ensure_future(maprad.fetch_broadcast_systems("fake-key", -33.87, 151.21, source="au"))
            second = asyncio.ensure_future(maprad.fetch_broadcast_systems("fake-key", -33.87, 151.21, source="au"))
            await asyncio.sleep(0.01)
            first.cancel()
            systems = await second
        assert first.cancelled()
        assert systems
        assert len(upstream.queries) == len(_AU_SUBTYPES)
        # And the walk the cancelled caller started was still held.
        assert len(maprad._cache) == 1
