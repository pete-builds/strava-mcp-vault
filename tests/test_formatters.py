"""Tests for formatters.py."""

from formatters import (
    _activity_category,
    _format_date,
    _format_distance,
    _format_duration,
    _format_elevation,
    _format_pace,
    _format_speed_mph,
    format_activities_near,
    format_activity_detail,
    format_activity_streams,
    format_athlete_profile,
    format_athlete_stats,
    format_cache_stats,
    format_delete_activities,
    format_recent_activities,
    format_recent_activities_compact,
    format_sync_result,
    format_vault_query,
)

# ── Helper functions ───────────────────────────────────────────────────


def test_format_pace():
    # ~7:18/mi at 3.66 m/s
    result = _format_pace(3.66)
    assert "/mi" in result
    assert result != "N/A"


def test_format_pace_zero():
    assert _format_pace(0) == "N/A"


def test_format_pace_none():
    assert _format_pace(None) == "N/A"


def test_format_speed_mph():
    # 11.176 m/s = ~25 mph
    result = _format_speed_mph(11.176)
    assert "mph" in result


def test_format_speed_mph_zero():
    assert _format_speed_mph(0) == "N/A"


def test_format_date_valid():
    result = _format_date("2026-04-01T08:00:00")
    assert "Apr" in result
    assert "2026" in result


def test_format_date_none():
    assert _format_date(None) == "Unknown"


def test_format_date_iso_z():
    result = _format_date("2026-04-01T12:00:00Z")
    assert "2026" in result


def test_format_distance():
    assert _format_distance(25.0) == "25.00 mi"


def test_format_distance_none():
    assert _format_distance(None) == "N/A"


def test_format_elevation():
    # 305 meters = ~1000 feet
    result = _format_elevation(305.0)
    assert "ft" in result
    assert "1000" in result or "1001" in result


def test_format_elevation_none():
    assert _format_elevation(None) == "N/A"


def test_format_duration_zero():
    assert _format_duration(0) == "0:00:00"


def test_format_duration_3661():
    assert _format_duration(3661) == "1:01:01"


def test_format_duration_none():
    assert _format_duration(None) == "N/A"


def test_activity_category_ride():
    assert _activity_category("Ride") == "ride"
    assert _activity_category("GravelRide") == "ride"
    assert _activity_category("VirtualRide") == "ride"


def test_activity_category_run():
    assert _activity_category("Run") == "run"
    assert _activity_category("TrailRun") == "run"


def test_activity_category_snow():
    assert _activity_category("Snowboard") == "snow"
    assert _activity_category("AlpineSki") == "snow"


def test_activity_category_walk():
    assert _activity_category("Walk") == "walk"
    assert _activity_category("Hike") == "walk"


def test_activity_category_swim():
    assert _activity_category("Swim") == "swim"


def test_activity_category_unknown():
    assert _activity_category("WeightTraining") == "other"
    assert _activity_category(None) == "other"


# ── format_recent_activities ───────────────────────────────────────────


def _make_shaped_activity(**overrides):
    """Build a shaped activity dict (post _shape_activity processing)."""
    base = {
        "id": 100001,
        "name": "Morning Ride",
        "type": "Ride",
        "sport_type": "Ride",
        "distance": 25.0,
        "moving_time": "1:00:00",
        "elapsed_time": "1:05:00",
        "start_date_local": "2026-04-01T08:00:00",
        "total_elevation_gain": 305.0,
        "average_speed": 11.176,
        "max_speed": 15.5,
        "average_heartrate": 145.0,
        "max_heartrate": 172.0,
        "calories": 850,
        "gear_id": None,
        "location_city": "Ithaca",
        "location_state": "New York",
        "location_country": "US",
        "kudos_count": 5,
        "achievement_count": 2,
        "suffer_score": 67,
        "location": "Ithaca, New York",
    }
    base.update(overrides)
    return base


def test_format_recent_activities_empty():
    assert format_recent_activities([]) == "No recent activities found."


def test_format_recent_activities_ride():
    activities = [_make_shaped_activity()]
    result = format_recent_activities(activities)
    assert "Morning Ride" in result
    assert "Ride" in result
    assert "145 bpm" in result


def test_format_recent_activities_run():
    activities = [_make_shaped_activity(sport_type="Run", type="Run", name="Evening Run")]
    result = format_recent_activities(activities)
    assert "Evening Run" in result
    assert "Pace" in result


def test_format_recent_activities_compact_empty():
    assert "No activities found" in format_recent_activities_compact([])


def test_format_recent_activities_compact():
    activities = [_make_shaped_activity()]
    result = format_recent_activities_compact(activities)
    assert "Morning Ride" in result
    assert "|" in result  # table format


# ── format_activity_detail ─────────────────────────────────────────────


def _make_raw_activity(**overrides):
    """Build a raw activity dict (from Strava API)."""
    base = {
        "id": 100001,
        "name": "Morning Ride",
        "sport_type": "Ride",
        "distance": 40233.6,
        "moving_time": 3600,
        "elapsed_time": 3900,
        "start_date_local": "2026-04-01T08:00:00",
        "total_elevation_gain": 305.0,
        "average_speed": 11.176,
        "max_speed": 15.5,
        "average_heartrate": 145.0,
        "max_heartrate": 172.0,
        "calories": 850,
    }
    base.update(overrides)
    return base


def test_format_activity_detail_ride():
    result = format_activity_detail(_make_raw_activity())
    assert "Morning Ride" in result
    assert "Speed" in result
    assert "Performance" in result


def test_format_activity_detail_run():
    result = format_activity_detail(_make_raw_activity(sport_type="Run"))
    assert "Pace" in result


def test_format_activity_detail_snow():
    result = format_activity_detail(
        _make_raw_activity(sport_type="Snowboard", elapsed_time=21600, moving_time=14400)
    )
    assert "Time on Mountain" in result


def test_format_activity_detail_walk():
    result = format_activity_detail(_make_raw_activity(sport_type="Walk"))
    assert "Time" in result


def test_format_activity_detail_swim():
    result = format_activity_detail(_make_raw_activity(sport_type="Swim"))
    assert "yd" in result or "Pace" in result


# ── format_activity_streams ────────────────────────────────────────────


def test_format_activity_streams_dict():
    streams = {
        "heartrate": {"data": [120, 130, 140, 150]},
        "altitude": {"data": [100.0, 200.0, 300.0]},
    }
    result = format_activity_streams(streams, 100001)
    assert "Heartrate" in result
    assert "Altitude" in result
    assert "bpm" in result


def test_format_activity_streams_list():
    streams = [
        {"type": "heartrate", "data": [120, 130, 140]},
    ]
    result = format_activity_streams(streams, 100001)
    assert "Heartrate" in result


def test_format_activity_streams_empty():
    result = format_activity_streams({}, 100001)
    assert "No stream data" in result


# ── format_athlete_profile ─────────────────────────────────────────────


def test_format_athlete_profile():
    profile = {
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
    result = format_athlete_profile(profile)
    assert "Pete Test" in result
    assert "Ithaca" in result
    assert "FTP" in result
    assert "250" in result


# ── format_athlete_stats ──────────────────────────────────────────────


def test_format_athlete_stats():
    stats = {
        "ytd_ride_totals": {
            "count": 50,
            "distance": 4023360.0,
            "moving_time": 180000,
            "elevation_gain": 30000.0,
        },
        "all_ride_totals": {
            "count": 200,
            "distance": 16093440.0,
            "moving_time": 720000,
            "elevation_gain": 120000.0,
        },
        "ytd_run_totals": {"count": 0, "distance": 0, "moving_time": 0},
    }
    result = format_athlete_stats(stats)
    assert "Year-to-Date Rides" in result
    assert "All-Time Rides" in result
    # Zero-count sections should be skipped
    assert "Year-to-Date Runs" not in result


def test_format_athlete_stats_biggest_ride():
    stats = {
        "biggest_ride_distance": 160934.0,
        "biggest_climb_elevation_gain": 1500.0,
    }
    result = format_athlete_stats(stats)
    assert "Longest Ride" in result
    assert "Biggest Climb" in result


# ── format_cache_stats ─────────────────────────────────────────────────


def test_format_cache_stats():
    stats = {
        "vault": {
            "total_activities": 150,
            "date_range": {
                "earliest": "2024-01-01T08:00:00",
                "latest": "2026-04-01T08:00:00",
            },
            "sync_log": {
                "last_sync_at": 1743523200.0,
                "total_synced": 150,
                "mode": "incremental",
            },
        },
        "total_cached_items": 25,
        "db_size_bytes": 102400,
        "categories": {
            "activity_detail": {"hits": 10, "misses": 5},
        },
        "rate_limit": {
            "short": {"usage": 10, "limit": 100},
            "long": {"usage": 200, "limit": 1000},
        },
    }
    result = format_cache_stats(stats)
    assert "150" in result
    assert "incremental" in result
    assert "Hit Rate" in result


def test_format_cache_stats_no_sync():
    stats = {
        "vault": {"total_activities": 0, "date_range": None, "sync_log": None},
        "total_cached_items": 0,
        "db_size_bytes": 0,
        "categories": {},
        "rate_limit": None,
    }
    result = format_cache_stats(stats)
    assert "Never" in result


# ── format_sync_result ─────────────────────────────────────────────────


def test_format_sync_result_full():
    result = format_sync_result(
        {
            "mode": "full",
            "activities_fetched": 150,
            "new_activities": 150,
            "total_in_vault": 150,
            "api_calls_used": 1,
            "date_range": {"earliest": "2024-01-01T08:00:00", "latest": "2026-04-01T08:00:00"},
        }
    )
    assert "Full historical sync" in result
    assert "150" in result


def test_format_sync_result_incremental():
    result = format_sync_result({"mode": "incremental", "activities_fetched": 3})
    assert "Incremental" in result


def test_format_sync_result_window():
    result = format_sync_result({"mode": "window_7d", "activities_fetched": 5})
    assert "7 days" in result


# ── format_vault_query ─────────────────────────────────────────────────


def test_format_vault_query():
    result_data = {
        "total_activities": 50,
        "breakdown_by_type": [
            {"sport_type": "Ride", "count": 30},
            {"sport_type": "Run", "count": 20},
        ],
        "total_distance_meters": 1609344.0,
        "total_moving_time_seconds": 180000,
        "total_elevation_meters": 15000.0,
        "filters": {"sport_type": None, "after": "2026-01-01", "before": None},
    }
    result = format_vault_query(result_data)
    assert "50" in result
    assert "Ride" in result
    assert "Run" in result


def test_format_vault_query_empty():
    result_data = {
        "total_activities": 0,
        "breakdown_by_type": [],
        "total_distance_meters": 0,
        "total_moving_time_seconds": 0,
        "total_elevation_meters": 0,
        "filters": {"sport_type": None, "after": None, "before": None},
    }
    result = format_vault_query(result_data)
    assert "No activities match" in result


# ── format_activities_near ─────────────────────────────────────────────


def test_format_activities_near():
    activities = [
        {
            "id": 1,
            "name": "Ride",
            "sport_type": "Ride",
            "distance": 40000,
            "moving_time": 3600,
            "start_date_local": "2026-04-01T08:00:00",
            "_location": "Ithaca, New York",
            "_distance_from_query_miles": 2.3,
        },
    ]
    result = format_activities_near(activities, "Ithaca, NY", 20.0)
    assert "Ithaca" in result
    assert "1 activities found" in result


def test_format_activities_near_empty():
    result = format_activities_near([], "Nowhere", 10.0)
    assert "No activities found" in result


# ── format_delete_activities ───────────────────────────────────────────


def test_format_delete_activities_empty_ids():
    result = format_delete_activities(0, [])
    assert "No IDs provided" in result


def test_format_delete_activities_success():
    result = format_delete_activities(2, [1, 2])
    assert "Deleted" in result
    assert "2" in result


def test_format_delete_activities_partial():
    result = format_delete_activities(1, [1, 2, 3])
    assert "Not found" in result
