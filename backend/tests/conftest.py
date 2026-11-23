"""Shared fixtures.

The elevation cache and cooldown are module state that outlives a test, so a
coordinate one test looked up would otherwise be answered from the cache in
the next, and a test that trips the cooldown would silence the upstream call
every test after it. The fan-out gate is rebuilt for the same reason and one
more: an asyncio.Semaphore binds to the loop that first contends on it, and
every test here gets a loop of its own.

Terrain tiles in the default folder, fetched there for a developer's own runs,
would otherwise move every figure scored near them: each test starts with the
tile folder pointed at an empty one and no tile reader held over. A test that
needs terrain sets its own.
"""

import asyncio

import pytest

from services import elevation, tower_scoring


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


@pytest.fixture(autouse=True)
def _no_terrain_tiles(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("TOWER_FINDER_DEM_DIR", str(tmp_path_factory.mktemp("no-terrain-tiles")))
    tower_scoring._DEMS.clear()
    yield
    tower_scoring._DEMS.clear()
