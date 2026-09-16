"""Shared fixtures.

The elevation cache and cooldown are module state that outlives a test, so a
coordinate one test looked up would otherwise be answered from the cache in
the next, and a test that trips the cooldown would silence the upstream call
every test after it. The fan-out gate is rebuilt for the same reason and one
more: an asyncio.Semaphore binds to the loop that first contends on it, and
every test here gets a loop of its own.
"""

import asyncio

import pytest

from services import elevation


@pytest.fixture(autouse=True)
def _clean_elevation_state():
    elevation._cache.clear()
    elevation._cooldown_until = 0.0
    elevation._consecutive_failures = 0
    elevation._fanout = asyncio.Semaphore(elevation._MAX_CONCURRENT_REQUESTS)
    yield
    elevation._cache.clear()
    elevation._cooldown_until = 0.0
    elevation._consecutive_failures = 0
