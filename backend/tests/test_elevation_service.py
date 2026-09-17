"""Cache, chunking and cooldown around the open-meteo elevation endpoint."""

import asyncio
import time
import unittest.mock

import httpx
import pytest

from services import elevation as elev
from tests._helpers import make_httpx_mock, status_error_response


@pytest.fixture(autouse=True)
def _clean_module_state():
    elev._cache.clear()
    elev._cooldown_until = 0.0
    elev._consecutive_failures = 0
    yield
    elev._cache.clear()
    elev._cooldown_until = 0.0
    elev._consecutive_failures = 0


def elevation_response(values):
    """An open-meteo answer carrying one elevation per requested coordinate."""
    resp = unittest.mock.MagicMock()
    resp.raise_for_status = unittest.mock.MagicMock()
    resp.json = unittest.mock.MagicMock(return_value={"elevation": list(values)})
    return resp


def requested_coord_count(patcher):
    """How many coordinates each upstream call carried, in order."""
    return [len(call.kwargs["params"]["latitude"].split(",")) for call in patcher.mock_client.get.call_args_list]


# ── Cache ────────────────────────────────────────────────────────────────────


class TestCache:
    async def test_no_coordinates_asks_upstream_nothing(self):
        patcher = make_httpx_mock(get_return=elevation_response([]))
        with patcher:
            assert await elev.lookup_many([]) == {}

        assert patcher.mock_client.get.call_count == 0

    async def test_a_repeated_coordinate_is_asked_for_once(self):
        patcher = make_httpx_mock(get_return=elevation_response([123.4]))
        with patcher:
            first = await elev.lookup_many([(33.9, -84.6)])
            second = await elev.lookup_many([(33.9, -84.6)])

        assert first == second == {(33.9, -84.6): 123.4}
        assert patcher.mock_client.get.call_count == 1

    async def test_only_the_coordinates_not_already_known_are_asked_for(self):
        patcher = make_httpx_mock(get_side_effect=[elevation_response([10.0]), elevation_response([20.0])])
        with patcher:
            await elev.lookup_many([(1.0, 1.0)])
            result = await elev.lookup_many([(1.0, 1.0), (2.0, 2.0)])

        assert result == {(1.0, 1.0): 10.0, (2.0, 2.0): 20.0}
        assert requested_coord_count(patcher) == [1, 1]

    async def test_a_point_upstream_has_no_data_for_is_not_asked_for_twice(self):
        """A null is a stable property of the coordinate, not a failure."""
        patcher = make_httpx_mock(get_return=elevation_response([None]))
        with patcher:
            assert await elev.lookup_many([(0.0, 0.0)]) == {}
            assert await elev.lookup_many([(0.0, 0.0)]) == {}

        assert patcher.mock_client.get.call_count == 1

    async def test_coordinates_differing_below_the_rounding_are_one_entry(self):
        patcher = make_httpx_mock(get_return=elevation_response([50.0]))
        with patcher:
            await elev.lookup_many([(33.9000001, -84.6000001)])
            await elev.lookup_many([(33.9000002, -84.6000002)])

        assert patcher.mock_client.get.call_count == 1

    def test_the_cache_is_bounded_and_evicts_the_oldest(self):
        for i in range(elev._CACHE_MAX_ENTRIES + 5):
            elev._cache_put((float(i), 0.0), None)

        assert len(elev._cache) == elev._CACHE_MAX_ENTRIES
        assert (0.0, 0.0) not in elev._cache
        assert (float(elev._CACHE_MAX_ENTRIES + 4), 0.0) in elev._cache


# ── Chunking ─────────────────────────────────────────────────────────────────


class TestChunking:
    async def test_a_request_at_the_cap_is_not_split(self):
        coords = [(float(i), 0.0) for i in range(100)]
        patcher = make_httpx_mock(get_return=elevation_response([1.0] * 100))
        with patcher:
            result = await elev.lookup_many(coords)

        assert requested_coord_count(patcher) == [100]
        assert len(result) == 100

    async def test_above_the_cap_every_coordinate_still_gets_an_elevation(self):
        """The whole of 200 coordinates used to come back null, because one
        over-sized request was rejected and took the lot with it."""
        coords = [(float(i), 0.0) for i in range(200)]
        patcher = make_httpx_mock(get_side_effect=[elevation_response([1.0] * 100), elevation_response([2.0] * 100)])
        with patcher:
            result = await elev.lookup_many(coords)

        assert requested_coord_count(patcher) == [100, 100]
        assert len(result) == 200

    async def test_a_chunk_that_fails_does_not_discard_the_chunks_that_worked(self):
        coords = [(float(i), 0.0) for i in range(200)]
        patcher = make_httpx_mock(get_side_effect=[elevation_response([1.0] * 100), status_error_response(429)])
        with patcher:
            result = await elev.lookup_many(coords)

        assert len(result) == 100

    async def test_a_chunk_rejected_as_our_own_fault_keeps_the_rest_too(self):
        """A non-429 4xx is a request we built wrong, and /api/elevation owes a
        500 for it. Tower search is best-effort, so here it costs its own chunk
        and no more."""
        coords = [(float(i), 0.0) for i in range(200)]
        patcher = make_httpx_mock(get_side_effect=[elevation_response([1.0] * 100), status_error_response(400)])
        with patcher:
            result = await elev.lookup_many(coords)

        assert len(result) == 100


# ── Failure classification ───────────────────────────────────────────────────


class TestSinglePointLookup:
    async def test_a_known_point_returns_its_elevation(self):
        with make_httpx_mock(get_return=elevation_response([123.4])):
            assert await elev.lookup(33.9, -84.6) == 123.4

    async def test_a_point_with_no_data_returns_none(self):
        with make_httpx_mock(get_return=elevation_response([None])):
            assert await elev.lookup(0.0, 0.0) is None

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    async def test_an_upstream_failure_reads_as_the_dependency(self, status):
        with make_httpx_mock(get_return=status_error_response(status)):
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(33.9, -84.6)

    async def test_a_timeout_reads_as_the_dependency(self):
        with make_httpx_mock(get_side_effect=httpx.TimeoutException("timed out")):
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(33.9, -84.6)

    async def test_a_connection_error_reads_as_the_dependency(self):
        with make_httpx_mock(get_side_effect=httpx.ConnectError("connection refused")):
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(33.9, -84.6)

    async def test_an_unreadable_body_reads_as_the_dependency(self):
        with make_httpx_mock(get_return=elevation_response(["not a number"])):
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(33.9, -84.6)

    @pytest.mark.parametrize("status", [400, 404, 422])
    async def test_a_rejected_request_is_not_reported_as_the_dependency(self, status):
        """A 4xx is open-meteo rejecting the request we built, so it is ours to
        answer for. Reporting it as the dependency would answer 503, which the
        post-deploy smoke passes on, gating a permanently broken route green."""
        with make_httpx_mock(get_return=status_error_response(status)):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await elev.lookup(33.9, -84.6)
        assert not isinstance(exc_info.value, elev.ElevationUnavailable)

    async def test_a_fault_of_our_own_is_not_reported_as_the_dependency(self):
        resp = unittest.mock.MagicMock()
        resp.raise_for_status = unittest.mock.MagicMock()
        # A dict where the code indexes a list: the shape a refactor gets wrong.
        resp.json = unittest.mock.MagicMock(return_value={"elevation": {"0": 123.4}})

        with make_httpx_mock(get_return=resp):
            with pytest.raises(Exception) as exc_info:  # noqa: PT011
                await elev.lookup(33.9, -84.6)
        assert not isinstance(exc_info.value, elev.ElevationUnavailable)


# ── Cooldown ─────────────────────────────────────────────────────────────────


class TestCooldown:
    async def test_after_a_rate_limit_the_next_call_does_not_reach_upstream(self):
        """Retrying into a rate limiter is what sustains it, and each attempt
        costs a caller the full timeout."""
        patcher = make_httpx_mock(get_return=status_error_response(429))
        with patcher:
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(1.0, 1.0)
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(2.0, 2.0)

        assert patcher.mock_client.get.call_count == 1

    async def test_a_cached_coordinate_is_still_served_during_a_cooldown(self):
        patcher = make_httpx_mock(get_side_effect=[elevation_response([77.0]), status_error_response(429)])
        with patcher:
            await elev.lookup_many([(1.0, 1.0)])
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(2.0, 2.0)
            assert await elev.lookup_many([(1.0, 1.0)]) == {(1.0, 1.0): 77.0}

        assert patcher.mock_client.get.call_count == 2

    async def test_upstream_is_tried_again_once_the_cooldown_expires(self):
        patcher = make_httpx_mock(get_side_effect=[status_error_response(429), elevation_response([5.0])])
        with patcher:
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(1.0, 1.0)
            elev._cooldown_until = 0.0
            assert await elev.lookup(2.0, 2.0) == 5.0

    async def test_repeated_failures_lengthen_the_cooldown(self):
        patcher = make_httpx_mock(get_return=status_error_response(429))
        with patcher:
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(1.0, 1.0)
            first = elev._cooldown_until
            elev._cooldown_until = 0.0
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(2.0, 2.0)
            second = elev._cooldown_until

        assert second - first > elev._COOLDOWN_S

    async def test_a_success_clears_the_backoff(self):
        patcher = make_httpx_mock(get_side_effect=[status_error_response(429), elevation_response([5.0])])
        with patcher:
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(1.0, 1.0)
            elev._cooldown_until = 0.0
            await elev.lookup(2.0, 2.0)

        assert elev._consecutive_failures == 0
        assert elev._cooldown_until == 0.0

    async def test_a_retry_after_longer_than_our_own_backoff_is_honoured(self):
        resp = status_error_response(429)
        resp.raise_for_status.side_effect.response.headers = {"Retry-After": "300"}

        patcher = make_httpx_mock(get_return=resp)
        with patcher:
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(1.0, 1.0)

        assert elev._cooldown_until - time.monotonic() > elev._COOLDOWN_S


# ── Answers that are not answers ─────────────────────────────────────────────


class TestPartialAnswers:
    async def test_fewer_elevations_than_coordinates_reads_as_the_dependency(self):
        with make_httpx_mock(get_return=elevation_response([1.0, 2.0])):
            with pytest.raises(elev.ElevationUnavailable):
                await elev._fetch([(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)])

    async def test_a_body_carrying_no_elevations_is_never_cached_as_no_data(self):
        """open-meteo answers some faults 200 with an error body. Padding the
        missing values with None and caching them would mark every coordinate
        in the request permanently barren."""
        error_body = unittest.mock.MagicMock()
        error_body.raise_for_status = unittest.mock.MagicMock()
        error_body.json = unittest.mock.MagicMock(return_value={"error": True, "reason": "something upstream"})

        patcher = make_httpx_mock(get_side_effect=[error_body, elevation_response([55.0])])
        with patcher:
            assert await elev.lookup_many([(1.0, 1.0)]) == {}
            assert elev._cache == {}
            elev._cooldown_until = 0.0
            assert await elev.lookup_many([(1.0, 1.0)]) == {(1.0, 1.0): 55.0}


# ── Pressure on a free endpoint ──────────────────────────────────────────────


class TestFanOut:
    async def test_a_single_blip_does_not_blind_the_service_for_a_minute(self):
        """The prefill is typed against. A transient timeout must cost seconds,
        not the whole cooldown a sustained rate limit earns."""
        with make_httpx_mock(get_side_effect=httpx.TimeoutException("timed out")):
            with pytest.raises(elev.ElevationUnavailable):
                await elev.lookup(1.0, 1.0)

        assert elev._cooldown_until - time.monotonic() <= 15.0

    async def test_concurrent_lookups_are_capped(self):
        """Unauthenticated routes feed this, so a burst against /api/towers
        must not become a simultaneous burst against open-meteo."""
        in_flight = 0
        peak = 0

        async def slow_get(*args, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return elevation_response([1.0])

        patcher = make_httpx_mock()
        patcher.mock_client.get = slow_get
        with patcher:
            await asyncio.gather(*(elev.lookup(float(i), 0.0) for i in range(20)))

        assert peak <= elev._MAX_CONCURRENT_REQUESTS

    async def test_a_burst_stops_at_the_cooldown_the_first_of_it_earned(self):
        """The callers queued behind the fan-out gate were admitted before the
        cooldown existed. Going out anyway spends the whole burst on a refusal
        already known, and drives the backoff to its ceiling in one go."""
        calls = 0

        async def refusing_get(*args, **kwargs):
            nonlocal calls
            calls += 1
            # Yields, so the whole burst is admitted past the cooldown check
            # before the first refusal comes back, which is the real shape.
            await asyncio.sleep(0.01)
            return status_error_response(429)

        patcher = make_httpx_mock()
        patcher.mock_client.get = refusing_get
        with patcher:
            results = await asyncio.gather(*(elev.lookup(float(i), 0.0) for i in range(20)), return_exceptions=True)

        assert all(isinstance(r, elev.ElevationUnavailable) for r in results)
        assert calls <= elev._MAX_CONCURRENT_REQUESTS

    async def test_one_incident_is_one_escalation_however_many_requests_it_caught(self):
        """The requests already in flight when a refusal lands are the same
        incident. Counting each of them escalates a single blip into the
        blackout a sustained outage is supposed to earn."""

        async def refusing_get(*args, **kwargs):
            await asyncio.sleep(0.01)
            return status_error_response(429)

        patcher = make_httpx_mock()
        patcher.mock_client.get = refusing_get
        with patcher:
            await asyncio.gather(*(elev.lookup(float(i), 0.0) for i in range(20)), return_exceptions=True)

        assert elev._consecutive_failures == 1
        assert elev._cooldown_until - time.monotonic() <= elev._COOLDOWN_S

    async def test_a_long_outage_saturates_rather_than_overflowing(self):
        """The backoff is capped but the count behind it is not, and an
        exponent that grows past what a float takes raises inside the handler,
        so /api/elevation would answer 500 where it owes a 503."""
        with make_httpx_mock(get_return=status_error_response(429)):
            for i in range(1100):
                elev._cooldown_until = 0.0
                with pytest.raises(elev.ElevationUnavailable):
                    await elev.lookup(float(i), 0.0)

        assert elev._cooldown_until - time.monotonic() <= elev._MAX_COOLDOWN_S
