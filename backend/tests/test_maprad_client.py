"""The subtype fan-out and cursor walk behind maprad.io.

Maprad answers for AU and CA only, and it is the sole source for both, so
what is pinned here is that every broadcast subtype is asked, that they go
together rather than in turn, and what a subtype that fails costs the ones
beside it.
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

    # Constructed as httpx.AsyncClient(timeout=...) and entered as a context
    # manager, so it stands in for both.
    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    @property
    def subtypes_queried(self):
        return [_SUBTYPE_RE.search(q).group(1) for q in self.queries]

    @property
    def sources_queried(self):
        return {_SOURCE_RE.search(q).group(1) for q in self.queries}

    async def post(self, url, json=None, headers=None):
        query = json["query"]
        subtype = _SUBTYPE_RE.search(query).group(1)
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
                return _Response({"errors": [{"message": "cannot query field"}]})

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
            if self._records is None:
                served, has_next = page_size, page + 1 < self._pages
            else:
                served = max(0, min(page_size, self._records - page * page_size))
                has_next = (page + 1) * page_size < self._records
            edges = [
                {
                    "cursor": f"{subtype}|{0 if self._stuck_cursor else page}",
                    "node": {"id": f"{subtype}-{page}-{i}", "devices": [], "licence": {"subtype": subtype}},
                }
                for i in range(served)
            ]
            return _Response({"data": {"systems": {"edges": edges, "pageInfo": {"hasNextPage": has_next}}}})
        finally:
            self.in_flight -= 1


async def _fetch(upstream, **kwargs):
    with unittest.mock.patch.object(maprad.httpx, "AsyncClient", upstream):
        return await maprad.fetch_broadcast_systems("fake-key", -33.87, 151.21, source="au", **kwargs)


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
        assert sorted(upstream.subtypes_queried) == sorted(maprad._BROADCAST_SUBTYPES)

    async def test_the_subtypes_go_together_not_in_turn(self):
        upstream = _Upstream()
        await _fetch(upstream)
        assert upstream.peak == len(maprad._BROADCAST_SUBTYPES), (
            f"expected all subtypes in flight at once, peak was {upstream.peak}"
        )

    async def test_results_from_every_subtype_are_returned(self):
        systems = await _fetch(_Upstream(pages=1))
        subtypes = {s["licence"]["subtype"] for s in systems}
        assert subtypes == set(maprad._BROADCAST_SUBTYPES)


class TestPagination:
    async def test_the_cursor_walk_follows_pages(self):
        systems = await _fetch(_Upstream(pages=2))
        # Two full pages, for each of the three subtypes.
        assert len(systems) == 2 * maprad._PAGE_SIZE * len(maprad._BROADCAST_SUBTYPES)

    async def test_the_walk_stops_when_the_upstream_says_so(self):
        upstream = _Upstream(pages=1)
        await _fetch(upstream, max_pages=10)
        assert len(upstream.queries) == len(maprad._BROADCAST_SUBTYPES)

    async def test_max_pages_caps_the_walk(self):
        upstream = _Upstream(pages=99)
        systems = await _fetch(upstream, max_pages=2)
        assert len(upstream.queries) == 2 * len(maprad._BROADCAST_SUBTYPES)
        assert len(systems) == 2 * maprad._PAGE_SIZE * len(maprad._BROADCAST_SUBTYPES)

    async def test_a_cursor_that_does_not_advance_stops_the_walk(self):
        # hasNextPage stays true while the cursor repeats, which would
        # otherwise re-fetch the same page up to max_pages.
        upstream = _Upstream(pages=99, stuck_cursor=True)
        await _fetch(upstream, max_pages=10)
        assert len(upstream.queries) == 2 * len(maprad._BROADCAST_SUBTYPES)


class TestFailureIsolation:
    async def test_one_subtype_failing_leaves_the_others(self):
        failed = maprad._BROADCAST_SUBTYPES[0]
        systems = await _fetch(_Upstream(fail_subtypes=[failed]))
        subtypes = {s["licence"]["subtype"] for s in systems}
        assert subtypes == set(maprad._BROADCAST_SUBTYPES) - {failed}

    async def test_graphql_errors_stop_only_their_own_subtype(self):
        broken = maprad._BROADCAST_SUBTYPES[1]
        systems = await _fetch(_Upstream(error_subtypes=[broken]))
        subtypes = {s["licence"]["subtype"] for s in systems}
        assert subtypes == set(maprad._BROADCAST_SUBTYPES) - {broken}

    async def test_every_subtype_failing_is_an_empty_list_not_a_raise(self):
        systems = await _fetch(_Upstream(fail_subtypes=maprad._BROADCAST_SUBTYPES))
        assert systems == []


class TestPageBudget:
    """What a single search can actually retrieve, and what it tells us when
    it could not retrieve all of it."""

    async def test_a_dense_location_is_returned_in_full(self):
        # Ninety systems to a subtype is inside what Sydney really holds, and
        # is more than one page however the walk is paged, so only a budget
        # that both pages widely and pages more than once returns the lot.
        upstream = _Upstream(records=90)
        systems = await _fetch(upstream)
        assert len(systems) == 90 * len(maprad._BROADCAST_SUBTYPES)

    async def test_the_page_size_stays_inside_the_upstream_limit(self):
        # A page above 30 is refused outright, and _paginate_query reads a
        # GraphQL error as "stop here", so asking too big empties the subtype
        # instead of failing loudly.
        upstream = _Upstream(records=1, page_size_limit=30)
        systems = await _fetch(upstream)
        assert len(systems) == len(maprad._BROADCAST_SUBTYPES)

    async def test_a_walk_cut_short_by_the_budget_says_which_subtype(self, caplog):
        upstream = _Upstream(records=10_000)
        with caplog.at_level("WARNING", logger="clients.maprad"):
            await _fetch(upstream)
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "a walk that left records behind reported nothing"
        for subtype in maprad._BROADCAST_SUBTYPES:
            assert any(subtype in message for message in warnings), f"no warning named {subtype}"

    async def test_a_walk_that_reaches_the_end_is_quiet(self, caplog):
        upstream = _Upstream(records=3)
        with caplog.at_level("WARNING", logger="clients.maprad"):
            await _fetch(upstream)
        assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == []
