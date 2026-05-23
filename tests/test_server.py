"""Tests for server.py validation helpers and tool functions."""

import json
from unittest.mock import AsyncMock, patch

import pytest

# ── Validation helpers ─────────────────────────────────────────────────


def test_validate_radius_miles_valid():
    from strava_mcp_vault.server import _validate_radius_miles

    assert _validate_radius_miles(20.0) is None


def test_validate_radius_miles_zero():
    from strava_mcp_vault.server import _validate_radius_miles

    result = _validate_radius_miles(0)
    assert result is not None
    assert "greater than 0" in result


def test_validate_radius_miles_negative():
    from strava_mcp_vault.server import _validate_radius_miles

    result = _validate_radius_miles(-5)
    assert "greater than 0" in result


def test_validate_radius_miles_too_large():
    from strava_mcp_vault.server import _validate_radius_miles

    result = _validate_radius_miles(300)
    assert "250 miles" in result


def test_validate_radius_miles_boundary():
    from strava_mcp_vault.server import _validate_radius_miles

    assert _validate_radius_miles(250) is None
    assert _validate_radius_miles(0.01) is None


# ── Tool functions ─────────────────────────────────────────────────────


@pytest.fixture
def mock_manager():
    """Patch the module-level manager in server.py."""
    m = AsyncMock()
    m.db = AsyncMock()
    with patch("strava_mcp_vault.server.manager", m):
        yield m


async def test_get_recent_activities_tool(mock_manager):
    from strava_mcp_vault.server import get_recent_activities

    mock_manager.get_recent_activities.return_value = []
    result = await get_recent_activities(count=5)
    assert "No recent activities" in result


async def test_get_recent_activities_rate_limit(mock_manager):
    from strava_mcp_vault.clients.strava import RateLimitError
    from strava_mcp_vault.server import get_recent_activities

    mock_manager.get_recent_activities.side_effect = RateLimitError("Rate limited!")
    result = await get_recent_activities()
    assert "Rate limited" in result


async def test_query_vault_tool(mock_manager):
    from strava_mcp_vault.server import query_vault

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
    from strava_mcp_vault.server import get_activity

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
    from strava_mcp_vault.server import get_cache_stats

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
    from strava_mcp_vault.server import get_activities_near

    result = await get_activities_near(location="")
    assert "Location is required" in result


async def test_get_activities_near_geocode_failure(mock_manager):
    from strava_mcp_vault.server import get_activities_near

    with patch("strava_mcp_vault.server.forward_geocode", return_value=None):
        result = await get_activities_near(location="Nonexistent Place XYZ")
    assert "Could not geocode" in result


async def test_delete_vault_activity_empty_ids(mock_manager):
    from strava_mcp_vault.server import delete_vault_activity

    result = await delete_vault_activity(activity_ids=[])
    assert "No activity IDs" in result


async def test_sync_activities_tool(mock_manager):
    from strava_mcp_vault.server import sync_activities

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
    from strava_mcp_vault.server import set_activity_location

    mock_manager.db.set_location_override.return_value = True
    result = await set_activity_location(activity_id=123, location="Ithaca, NY")
    assert "Ithaca, NY" in result


async def test_set_activity_location_not_found(mock_manager):
    from strava_mcp_vault.server import set_activity_location

    mock_manager.db.set_location_override.return_value = False
    result = await set_activity_location(activity_id=999)
    assert "not found" in result


def test_format_error_401_with_scope_hint_says_rerun_oauth():
    """When 401 detail mentions a missing scope, point users at OAuth, not reseed."""
    from strava_mcp_vault.exceptions import StravaAPIError
    from strava_mcp_vault.server import _tool_error

    err = StravaAPIError(
        status_code=401,
        path="/athlete/activities",
        detail='{"message":"Authorization Error","errors":[{"resource":"AccessToken","field":"activity:read_permission","code":"missing"}]}',
    )
    msg = _tool_error("strava_get_recent_activities", err)

    assert "scope" in msg.lower()
    assert "activity:read_all" in msg
    assert "reseed" not in msg.lower()


def test_format_error_401_without_scope_hint_says_reseed():
    """Plain 401 (no scope marker in body) still recommends reseeding tokens."""
    from strava_mcp_vault.exceptions import StravaAPIError
    from strava_mcp_vault.server import _tool_error

    err = StravaAPIError(
        status_code=401,
        path="/athlete/activities",
        detail="",
    )
    msg = _tool_error("strava_get_recent_activities", err)

    assert "reseed" in msg.lower() or "re-seed" in msg.lower()


# ── get_activity_streams: defensive filter + downsample ───────────────


@pytest.mark.asyncio
async def test_get_activity_streams_filters_extra_streams(mock_manager):
    """Defensive: even if manager returns extras, server filters to requested."""
    mock_manager.get_streams_normalized.return_value = {
        "heartrate": [120, 130],
        "watts": [200, 210],
    }
    import json

    from strava_mcp_vault.server import get_activity_streams
    result = await get_activity_streams(
        activity_id=1, stream_types="heartrate", response_format="json"
    )
    payload = json.loads(result)
    assert "watts" not in payload["streams"]
    assert payload["streams"]["heartrate"] == [120, 130]


@pytest.mark.asyncio
async def test_get_activity_streams_downsample_block_present(mock_manager):
    mock_manager.get_streams_normalized.return_value = {"heartrate": list(range(1000))}
    import json

    from strava_mcp_vault.server import get_activity_streams
    result = await get_activity_streams(
        activity_id=1, stream_types="heartrate", max_points=100, response_format="json"
    )
    payload = json.loads(result)
    assert payload["downsample"]["original_points"] == 1000
    assert payload["downsample"]["returned_points"] == 100
    assert payload["downsample"]["reason"] == "user_requested"
    assert len(payload["streams"]["heartrate"]) == 100


@pytest.mark.asyncio
async def test_get_activity_streams_no_downsample_reason_none(mock_manager):
    mock_manager.get_streams_normalized.return_value = {"heartrate": [120, 130, 140]}
    import json

    from strava_mcp_vault.server import get_activity_streams
    result = await get_activity_streams(
        activity_id=1, stream_types="heartrate", response_format="json"
    )
    payload = json.loads(result)
    assert payload["downsample"]["reason"] == "none"


# ── get_activity_streams: pre-flight size guard ───────────────────────


@pytest.mark.asyncio
async def test_get_activity_streams_size_guard_fires_when_oversize():
    # 5 streams x 50_000 points x 10 bytes x 1.2 = 3MB → triggers guard
    async def fake_normalized(activity_id, stream_types):
        return {k: list(range(50_000)) for k in ["heartrate", "watts", "time", "altitude", "cadence"]}

    m = AsyncMock()
    m.get_streams_normalized = fake_normalized
    with patch("strava_mcp_vault.server.manager", m):
        from strava_mcp_vault.server import get_activity_streams
        result = await get_activity_streams(
            activity_id=1,
            stream_types="heartrate,watts,time,altitude,cadence",
            response_format="json",
        )
    payload = json.loads(result)
    assert payload["error"] == "response_too_large"
    assert payload["original_points"] == 50_000
    assert payload["recommended_max_points"] > 100
    assert "max_points=" in payload["message"]


@pytest.mark.asyncio
async def test_get_activity_streams_size_guard_bypassed_when_max_points_set():
    async def fake_normalized(activity_id, stream_types):
        return {k: list(range(50_000)) for k in ["heartrate", "watts", "time"]}

    m = AsyncMock()
    m.get_streams_normalized = fake_normalized
    with patch("strava_mcp_vault.server.manager", m):
        from strava_mcp_vault.server import get_activity_streams
        result = await get_activity_streams(
            activity_id=1,
            stream_types="heartrate,watts,time",
            max_points=500,
            response_format="json",
        )
    payload = json.loads(result)
    assert "error" not in payload
    assert payload["downsample"]["returned_points"] == 500


# ── get_activity_streams: export_path mode ───────────────────────────


@pytest.mark.asyncio
async def test_get_activity_streams_export_path_writes_file(tmp_path):
    """export_path mode writes full dataset to disk + returns pointer."""
    export_file = tmp_path / "streams-out.json"

    async def fake_normalized(activity_id, stream_types):
        return {"heartrate": list(range(100)), "watts": list(range(100, 200))}

    m = AsyncMock()
    m.get_streams_normalized = fake_normalized
    with patch("strava_mcp_vault.server.manager", m):
        from strava_mcp_vault.server import get_activity_streams
        result = await get_activity_streams(
            activity_id=42,
            stream_types="heartrate,watts",
            export_path=str(export_file),
            response_format="json",
        )

    payload = json.loads(result)
    assert payload["path"] == str(export_file)
    assert payload["original_points"] == 100
    assert payload["streams_written"] == ["heartrate", "watts"]
    assert payload["schema_version"] == "1"
    assert payload["size_bytes"] > 0
    assert export_file.exists()

    file_content = json.loads(export_file.read_text())
    assert file_content["streams"]["heartrate"] == list(range(100))
    assert file_content["streams"]["watts"] == list(range(100, 200))
    assert file_content["downsample"]["reason"] == "none"


@pytest.mark.asyncio
async def test_get_activity_streams_export_path_bypasses_size_guard(tmp_path):
    """Even when oversize, export_path writes to disk regardless."""
    export_file = tmp_path / "big.json"

    async def fake_normalized(activity_id, stream_types):
        return {k: list(range(50_000)) for k in ["heartrate", "watts", "time", "altitude", "cadence"]}

    m = AsyncMock()
    m.get_streams_normalized = fake_normalized
    with patch("strava_mcp_vault.server.manager", m):
        from strava_mcp_vault.server import get_activity_streams
        result = await get_activity_streams(
            activity_id=1,
            stream_types="heartrate,watts,time,altitude,cadence",
            export_path=str(export_file),
            response_format="json",
        )

    payload = json.loads(result)
    assert "error" not in payload
    assert export_file.exists()


@pytest.mark.asyncio
async def test_get_activity_streams_export_path_unwritable_errors():
    """Unwritable path errors cleanly (no silent fallback)."""
    async def fake_normalized(activity_id, stream_types):
        return {"heartrate": [120, 130, 140]}

    m = AsyncMock()
    m.get_streams_normalized = fake_normalized
    with patch("strava_mcp_vault.server.manager", m):
        from strava_mcp_vault.server import get_activity_streams
        result = await get_activity_streams(
            activity_id=1,
            stream_types="heartrate",
            export_path="/nonexistent_root_dir_xyz/file.json",
            response_format="json",
        )

    assert "error" in result.lower() or "permission" in result.lower() or "no such" in result.lower()


@pytest.mark.asyncio
async def test_get_zone_distribution_tool():
    """Tool wires together stream fetch, zones fetch, compute, and format."""
    # heartrate stream: 600 samples at 120 bpm (1 Hz → 600 s total)
    hr_stream = [120] * 600
    fake_streams = {"heartrate": hr_stream, "time": list(range(600))}

    fake_zones = {
        "heart_rate": {
            "zones": [
                {"min": 0, "max": 115},
                {"min": 115, "max": 152},
                {"min": 152, "max": 171},
                {"min": 171, "max": 190},
                {"min": 190, "max": -1},
            ]
        },
        "power": {"zones": [{"min": 0, "max": 180}]},
    }

    m = AsyncMock()
    m.get_streams_normalized = AsyncMock(return_value=fake_streams)
    m.get_athlete_zones = AsyncMock(return_value=fake_zones)

    with patch("strava_mcp_vault.server.manager", m):
        from strava_mcp_vault.server import get_zone_distribution
        result = await get_zone_distribution(
            activity_id=1,
            zone_type="hr",
            response_format="json",
        )

    payload = json.loads(result)
    assert payload["hr"] is not None
    # All time should land in zone 2 (115–152), since HR=120 is in that range.
    # time=[0..599] → 599 deltas of 1 s each → 599 s total in zone 2.
    zone2 = next(z for z in payload["hr"] if z["zone"] == 2)
    assert zone2["time_s"] == 599


@pytest.mark.asyncio
async def test_get_power_curve_tool():
    """Tool fetches watts stream, computes power curve, returns json."""
    # 600 samples at 200 W → best at any duration == 200 W
    m = AsyncMock()
    m.get_streams_normalized = AsyncMock(return_value={"watts": [200] * 600})
    with patch("strava_mcp_vault.server.manager", m):
        from strava_mcp_vault.server import get_power_curve
        result = await get_power_curve(
            activity_id=1,
            durations="5,60,300",
            response_format="json",
        )
    payload = json.loads(result)
    assert payload["activity_id"] == 1
    assert all(p["best_watts"] == 200 for p in payload["points"])


@pytest.mark.asyncio
async def test_get_hr_power_decoupling_tool():
    """Tool fetches streams, computes decoupling, returns json."""
    # 600 samples at 140 bpm + 200 W → both segments identical → 0% decoupling
    m = AsyncMock()
    m.get_streams_normalized = AsyncMock(
        return_value={"heartrate": [140] * 600, "watts": [200] * 600}
    )
    with patch("strava_mcp_vault.server.manager", m):
        from strava_mcp_vault.server import get_hr_power_decoupling
        result = await get_hr_power_decoupling(activity_id=1, response_format="json")
    payload = json.loads(result)
    assert payload["activity_id"] == 1
    assert payload["first_segment"]["avg_hr"] == 140.0
