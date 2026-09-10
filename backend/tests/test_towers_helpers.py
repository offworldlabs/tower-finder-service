"""Unit tests for tower helper functions: _detect_source, _nearby_states."""

import pytest
from clients.fcc import _nearby_states
from fastapi import HTTPException
from routes.towers import _detect_source


# ── Nearby States ───────────────────────────────────────────────────────────


class TestNearbyStates:
    """Tests for _nearby_states(lat, lon) -> list[str]."""

    def test_long_island_includes_ny(self):
        """Long Island is in NY — NY must be queried despite its distant centroid."""
        states = _nearby_states(40.777229, -73.081408)
        assert "NY" in states

    def test_chicago_includes_il(self):
        states = _nearby_states(41.88, -87.63)
        assert "IL" in states

    def test_hawaii(self):
        states = _nearby_states(21.31, -157.86)
        assert "HI" in states
        assert len(states) == 1


# ── Source Detection ─────────────────────────────────────────────────────────


class TestDetectSource:
    """Tests for _detect_source(lat, lon) -> str."""

    def test_australia_sydney(self):
        """Sydney is in the Australian region."""
        assert _detect_source(-33.9, 151.2) == "au"

    def test_canada_toronto(self):
        """Toronto is in the Canadian region."""
        assert _detect_source(43.7, -79.4) == "ca"

    def test_us_mainland_atlanta(self):
        """Atlanta is in the US mainland region."""
        assert _detect_source(33.7, -84.4) == "us"

    def test_offshore_alaska_edge_raises(self):
        """These offshore coords aren't inside the mapped US region polygons;
        _detect_source now raises instead of failing open to 'us'
        (edge-of-region false negatives are accepted for now)."""
        with pytest.raises(HTTPException) as exc_info:
            _detect_source(61.0, -150.0)
        assert exc_info.value.status_code == 422
        assert "supported region" in exc_info.value.detail

    def test_offshore_hawaii_edge_raises(self):
        """South of the main Hawaiian islands — outside the mapped polygons —
        now raises rather than defaulting to 'us'."""
        with pytest.raises(HTTPException) as exc_info:
            _detect_source(20.0, -157.0)
        assert exc_info.value.status_code == 422
        assert "supported region" in exc_info.value.detail

    def test_pacific_ocean_raises(self):
        """Middle of Pacific Ocean is not in a supported region — must raise."""
        with pytest.raises(HTTPException) as exc_info:
            _detect_source(0.0, 170.0)
        assert exc_info.value.status_code == 422
        assert "supported region" in exc_info.value.detail

    def test_south_america_raises(self):
        """São Paulo (outside defined regions) must raise, not default to 'us'."""
        with pytest.raises(HTTPException) as exc_info:
            _detect_source(-23.5, -46.6)
        assert exc_info.value.status_code == 422
        assert "supported region" in exc_info.value.detail
