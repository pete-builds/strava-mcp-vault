"""Shared test fixtures for strava-mcp-vault."""

from unittest.mock import AsyncMock

import pytest

from cache.db import CacheDB


@pytest.fixture
async def tmp_db(tmp_path):
    """Create a fresh CacheDB in a temp directory."""
    db_path = str(tmp_path / "test-vault.db")
    db = CacheDB(db_path)
    await db.init()
    yield db
    await db.close()


@pytest.fixture
def mock_strava_client():
    """Return an AsyncMock StravaClient with preset return values."""
    client = AsyncMock()
    client.client_id = "test_id"
    client.client_secret = "test_secret"
    client._access_token = "test_access_token"
    client._refresh_token = "test_refresh_token"
    client._expires_at = 9999999999

    client.get_athlete.return_value = {
        "id": 12345,
        "firstname": "Pete",
        "lastname": "Test",
        "city": "Ithaca",
        "state": "New York",
        "country": "United States",
        "weight": 80.0,
        "ftp": 250,
        "follower_count": 42,
        "friend_count": 10,
        "premium": True,
    }
    client.get_activities.return_value = []
    client.get_activity.return_value = {}
    client.get_activity_streams.return_value = {}
    client.get_athlete_stats.return_value = {}
    client.get_gear.return_value = {"name": "Trek Domane"}
    client.rate_limit_remaining = {
        "short": {"usage": 10, "limit": 100},
        "long": {"usage": 200, "limit": 1000},
    }
    return client


@pytest.fixture
async def cache_manager(tmp_db, mock_strava_client):
    """Create a CacheManager with a real DB and mocked client."""
    from cache.manager import CacheManager

    return CacheManager(tmp_db, mock_strava_client)


@pytest.fixture
def sample_activity():
    """Return a realistic Strava activity dict."""
    return {
        "id": 100001,
        "name": "Morning Ride",
        "type": "Ride",
        "sport_type": "Ride",
        "distance": 40233.6,  # ~25 miles in meters
        "moving_time": 3600,
        "elapsed_time": 3900,
        "start_date": "2026-04-01T12:00:00Z",
        "start_date_local": "2026-04-01T08:00:00",
        "total_elevation_gain": 305.0,
        "average_speed": 11.176,  # m/s
        "max_speed": 15.5,
        "average_heartrate": 145.0,
        "max_heartrate": 172.0,
        "calories": 850,
        "gear_id": "g12345",
        "location_city": "Ithaca",
        "location_state": "New York",
        "location_country": "US",
        "kudos_count": 5,
        "achievement_count": 2,
        "suffer_score": 67,
        "start_latlng": [42.4440, -76.5019],
    }


@pytest.fixture
def sample_activities_batch(sample_activity):
    """Return a list of 5 diverse activities for batch testing."""
    base = sample_activity
    return [
        base,
        {
            **base,
            "id": 100002,
            "name": "Evening Run",
            "type": "Run",
            "sport_type": "Run",
            "distance": 8046.72,  # ~5 miles
            "moving_time": 2400,
            "elapsed_time": 2500,
            "start_date": "2026-04-02T22:00:00Z",
            "start_date_local": "2026-04-02T18:00:00",
            "average_speed": 3.353,
            "start_latlng": [42.4500, -76.4800],
        },
        {
            **base,
            "id": 100003,
            "name": "Gravel Adventure",
            "type": "Ride",
            "sport_type": "GravelRide",
            "distance": 64373.76,  # ~40 miles
            "moving_time": 7200,
            "elapsed_time": 7800,
            "start_date": "2026-04-03T14:00:00Z",
            "start_date_local": "2026-04-03T10:00:00",
            "start_latlng": [42.4600, -76.5100],
        },
        {
            **base,
            "id": 100004,
            "name": "Powder Day",
            "type": "Snowboard",
            "sport_type": "Snowboard",
            "distance": 16093.44,
            "moving_time": 14400,
            "elapsed_time": 21600,
            "start_date": "2026-03-15T16:00:00Z",
            "start_date_local": "2026-03-15T11:00:00",
            "total_elevation_gain": 3048.0,
            "start_latlng": [44.1340, -72.8460],
        },
        {
            **base,
            "id": 100005,
            "name": "Afternoon Walk",
            "type": "Walk",
            "sport_type": "Walk",
            "distance": 4828.03,  # ~3 miles
            "moving_time": 3600,
            "elapsed_time": 4000,
            "start_date": "2026-04-04T19:00:00Z",
            "start_date_local": "2026-04-04T15:00:00",
            "average_speed": 1.341,
            "start_latlng": [42.4440, -76.5019],
        },
    ]
