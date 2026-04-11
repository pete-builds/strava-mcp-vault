"""Tests for server.py validation helpers and tool functions."""

from unittest.mock import AsyncMock, patch

import pytest

# ── Validation helpers ─────────────────────────────────────────────────


def test_validate_radius_miles_valid():
    from server import _validate_radius_miles

    assert _validate_radius_miles(20.0) is None


def test_validate_radius_miles_zero():
    from server import _validate_radius_miles

    result = _validate_radius_miles(0)
    assert result is not None
    assert "greater than 0" in result


def test_validate_radius_miles_negative():
    from server import _validate_radius_miles

    result = _validate_radius_miles(-5)
    assert "greater than 0" in result


def test_validate_radius_miles_too_large():
    from server import _validate_radius_miles

    result = _validate_radius_miles(300)
    assert "250 miles" in result


def test_validate_radius_miles_boundary():
    from server import _validate_radius_miles

    assert _validate_radius_miles(250) is None
    assert _validate_radius_miles(0.01) is None


# ── Tool functions ─────────────────────────────────────────────────────


@pytest.fixture
def mock_manager():
    """Patch the module-level manager in server.py."""
    m = AsyncMock()
    m.db = AsyncMock()
    with patch("server.manager", m):
        yield m


async def test_get_recent_activities_tool(mock_manager):
    from server import get_recent_activities

    mock_manager.get_recent_activities.return_value = []
    result = await get_recent_activities(count=5)
    assert "No recent activities" in result


async def test_get_recent_activities_rate_limit(mock_manager):
    from clients.strava import RateLimitError
    from server import get_recent_activities

    mock_manager.get_recent_activities.side_effect = RateLimitError("Rate limited!")
    result = await get_recent_activities()
    assert "Rate limited" in result


async def test_query_vault_tool(mock_manager):
    from server import query_vault

    mock_manager.query_vault.return_value = {
        "total_activities": 0,
        "breakdown_by_type": [],
        "total_distance_meters": 0,
        "total_moving_time_seconds": 0,
        "total_elevation_meters": 0,
        "filters": {"sport_type": None, "after": None, "before": None},
    }
    result = await query_vault()
    assert "No activities match" in result


async def test_get_activity_tool(mock_manager):
    from server import get_activity

    mock_manager.get_activity.return_value = {
        "id": 999,
        "name": "Test Ride",
        "sport_type": "Ride",
        "distance": 40000,
        "moving_time": 3600,
        "elapsed_time": 3900,
        "total_elevation_gain": 300,
        "average_speed": 11.0,
        "start_date_local": "2026-04-01T08:00:00",
    }
    result = await get_activity(999)
    assert "Test Ride" in result


async def test_get_cache_stats_tool(mock_manager):
    from server import get_cache_stats

    mock_manager.get_cache_stats.return_value = {
        "vault": {"total_activities": 0, "date_range": None, "sync_log": None},
        "total_cached_items": 0,
        "db_size_bytes": 0,
        "categories": {},
        "rate_limit": None,
    }
    result = await get_cache_stats()
    assert "Vault" in result


async def test_get_activities_near_empty_location(mock_manager):
    from server import get_activities_near

    result = await get_activities_near(location="")
    assert "Location is required" in result


async def test_get_activities_near_geocode_failure(mock_manager):
    from server import get_activities_near

    with patch("server.forward_geocode", return_value=None):
        result = await get_activities_near(location="Nonexistent Place XYZ")
    assert "Could not geocode" in result


async def test_delete_vault_activity_empty_ids(mock_manager):
    from server import delete_vault_activity

    result = await delete_vault_activity(activity_ids=[])
    assert "No activity IDs" in result


async def test_sync_activities_tool(mock_manager):
    from server import sync_activities

    mock_manager.sync_activities.return_value = {
        "mode": "full",
        "activities_fetched": 10,
        "new_activities": 10,
        "total_in_vault": 10,
        "api_calls_used": 1,
        "date_range": None,
    }
    result = await sync_activities()
    assert "Sync Complete" in result


async def test_set_activity_location_tool(mock_manager):
    from server import set_activity_location

    mock_manager.db.set_location_override.return_value = True
    result = await set_activity_location(activity_id=123, location="Ithaca, NY")
    assert "Ithaca, NY" in result


async def test_set_activity_location_not_found(mock_manager):
    from server import set_activity_location

    mock_manager.db.set_location_override.return_value = False
    result = await set_activity_location(activity_id=999)
    assert "not found" in result
