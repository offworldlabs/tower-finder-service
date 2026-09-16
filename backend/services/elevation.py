"""Ground elevation for coordinates, from open-meteo.

The elevation of a point does not change, so an answer is kept for the life of
the process rather than asked for again. A tower search over one metro returns
largely the same coordinates search after search, and this free endpoint is the
only thing in the service that rate-limits us.

Two upstream rules shape the rest. At most 100 coordinates may go in one
request, and past that the request is rejected whole, so coordinates go in
chunks and a chunk that fails costs only its own share. An upstream that will
not answer, a 429 above all, is met with a cooldown rather than a retry:
retrying is what sustains a rate limit, and every attempt spends its caller the
full timeout to learn what the last one already knew.
"""

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_URL = "https://api.open-meteo.com/v1/elevation"

# Tight on purpose. Elevation is an enrichment, so a tower search that waits on
# it is a tower search that outlives its caller: retina-server's deploy probe
# asserts the tower contract under a fixed budget and fails the whole of it on
# a slow answer.
_TIMEOUT_S = 5.0

# open-meteo's documented ceiling for this endpoint.
_MAX_COORDS_PER_REQUEST = 100

# Bounded for memory, not for staleness: a coordinate's elevation is fixed, so
# an entry is never wrong, only eventually surplus.
_CACHE_MAX_ENTRIES = 50_000

# The first cooldown is short because the commonest failure is a blip, and the
# altitude prefill is typed against: a minute of 503s for one slow response is
# worse than the request it saved. A sustained refusal doubles its way up to
# _MAX_COOLDOWN_S within a handful of attempts.
_COOLDOWN_S = 10.0
_MAX_COOLDOWN_S = 10 * 60.0

# The routes that feed this are unauthenticated, so a burst against /api/towers
# must not become a simultaneous burst against a free endpoint. The geocoder
# caps its own fan-out for the same reason.
_MAX_CONCURRENT_REQUESTS = 4

# Under a metre at the equator. Callers key on the same rounding, so a tower
# whose coordinates differ in the seventh decimal is not a second lookup.
_COORD_DP = 6

Coord = tuple[float, float]

_fanout = asyncio.Semaphore(_MAX_CONCURRENT_REQUESTS)


class ElevationUnavailable(Exception):
    """open-meteo could not be reached, or would not answer.

    Distinct from a point it simply has no data for, which is a valid answer
    and comes back as None.
    """


# Coordinate -> elevation, or None where upstream has no data for the point.
# Absent means never asked. Insertion-ordered, so the oldest entry is the first
# key.
_cache: dict[Coord, float | None] = {}

# Monotonic deadline before which no request leaves this process, and the run
# of failures that set it. Module state rather than per-caller, so one caller's
# discovery that open-meteo is refusing spares every other caller the wait.
_cooldown_until: float = 0.0
_consecutive_failures: int = 0


def key(lat: float, lon: float) -> Coord:
    return (round(lat, _COORD_DP), round(lon, _COORD_DP))


def _cache_put(key: Coord, value: float | None) -> None:
    # Re-inserting moves the key to the end, so a re-read entry is not the next
    # one evicted.
    _cache.pop(key, None)
    _cache[key] = value
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))


def _retry_after_s(exc: Exception) -> float:
    """The upstream's own instruction, when it sends one that reads as seconds.

    Anything else, including the HTTP-date form the RFC also allows, falls back
    to our own backoff rather than being guessed at.
    """
    response = getattr(exc, "response", None)
    try:
        return float(response.headers["Retry-After"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return 0.0


def _enter_cooldown(exc: Exception) -> None:
    global _cooldown_until, _consecutive_failures
    now = time.monotonic()
    # The requests already in flight when the first refusal landed are one
    # incident, not several: escalating once per failed request turns a single
    # blip into the blackout a sustained outage is meant to earn.
    if now < _cooldown_until:
        return
    backoff = min(_COOLDOWN_S * 2**_consecutive_failures, _MAX_COOLDOWN_S)
    # Counting past saturation only grows an exponent nothing reads, and that
    # no float eventually takes. It would raise here inside the handler,
    # displacing the ElevationUnavailable the route answers 503 on.
    if backoff < _MAX_COOLDOWN_S:
        _consecutive_failures += 1
    _cooldown_until = now + max(backoff, _retry_after_s(exc))


def _raise_if_cooling() -> None:
    if time.monotonic() < _cooldown_until:
        raise ElevationUnavailable("in cooldown after a recent open-meteo failure")


def _clear_cooldown() -> None:
    global _cooldown_until, _consecutive_failures
    _consecutive_failures = 0
    _cooldown_until = 0.0


async def _fetch(chunk: list[Coord]) -> dict[Coord, float | None]:
    """One upstream request, at most _MAX_COORDS_PER_REQUEST coordinates.

    Every coordinate asked for comes back keyed, None where upstream has no
    data for it, so the caller can cache the nulls as readily as the numbers.
    """
    _raise_if_cooling()

    params = {
        "latitude": ",".join(str(c[0]) for c in chunk),
        "longitude": ",".join(str(c[1]) for c in chunk),
    }
    try:
        async with _fanout:
            # Read again on the way out of the queue: a whole burst is admitted
            # past the first check before any of it comes back, so without this
            # the callers behind the gate each repeat a refusal already known,
            # driving the backoff to its ceiling in one go.
            _raise_if_cooling()
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
                resp = await client.get(_URL, params=params)
                resp.raise_for_status()
                # Parsed inside the try, as the geocoder does: a shape error
                # raised by a bug here must reach the caller as a 500 rather
                # than wearing open-meteo's name, so only the narrow types
                # below are converted.
                raw = resp.json().get("elevation", [])
                # Short of what we asked for is not "these points have no
                # data": a 200 carrying an error body reads as every coordinate
                # being barren, and caching that would mark them so for the
                # life of the process. Nothing is cached from a request that
                # fails here.
                if len(raw) < len(chunk):
                    raise ValueError(f"open-meteo answered {len(raw)} of {len(chunk)} coordinates")
                values = [None if raw[i] is None else float(raw[i]) for i in range(len(chunk))]
    # Narrow deliberately: a transport fault, a 5xx or 429, or a body that will
    # not read as numbers is open-meteo's failure. Anything else is a fault in
    # the code above and must not be dressed up as the dependency being down,
    # which the post-deploy smoke passes on.
    except (httpx.HTTPError, ValueError) as exc:
        # A 4xx is open-meteo rejecting the request we built, which is ours to
        # answer for: it must reach the caller as a 500. 429 is the exception,
        # being its rate limit rather than anything wrong with the request.
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status < 500 and status != 429:
                raise
        logger.warning("Elevation lookup failed for %d coordinate(s): %s", len(chunk), exc)
        _enter_cooldown(exc)
        raise ElevationUnavailable(str(exc)) from exc

    _clear_cooldown()
    return dict(zip(chunk, values, strict=True))


async def lookup(lat: float, lon: float) -> float | None:
    """Ground elevation at one point, or None where upstream has no data.

    Raises ElevationUnavailable rather than degrading: one coordinate has no
    partial answer, and /api/elevation's caller needs those two apart.
    """
    coord = key(lat, lon)
    if coord in _cache:
        return _cache[coord]
    value = (await _fetch([coord]))[coord]
    _cache_put(coord, value)
    return value


async def lookup_many(coords: list[tuple[float, float]]) -> dict[Coord, float]:
    """Ground elevation for each of these coordinates that has one.

    Best-effort by contract: coordinates upstream would not answer for are
    simply absent from the result, and so are the ones a failed chunk carried.
    The caller is tower search, whose answer is the tower list, and an
    elevation missing from a row is a poorer row rather than a failed search.
    """
    wanted = list(dict.fromkeys(key(lat, lon) for lat, lon in coords))
    known = {k: _cache[k] for k in wanted if k in _cache}
    unknown = [k for k in wanted if k not in _cache]

    for i in range(0, len(unknown), _MAX_COORDS_PER_REQUEST):
        try:
            fetched = await _fetch(unknown[i : i + _MAX_COORDS_PER_REQUEST])
        except ElevationUnavailable:
            # Sequential, and abandoned at the first failure: the chunks share
            # one rate limit, so pressing on past a refusal only deepens it.
            break
        except Exception:
            # A request we built wrong, which lookup() surfaces as the 500 it
            # owes. This caller is best-effort, so it is logged loudly and the
            # coordinates already resolved are kept rather than thrown out
            # alongside it.
            logger.exception("Elevation lookup failed on a chunk of its own making")
            break
        for coord, value in fetched.items():
            _cache_put(coord, value)
        known.update(fetched)

    return {k: v for k, v in known.items() if v is not None}
