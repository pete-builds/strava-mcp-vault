"""Tests for cache/manager.py."""

import pytest

from strava_mcp_vault.cache.manager import _format_duration, _shape_activity

# ── Helper functions ───────────────────────────────────────────────────


def test_format_duration_zero():
    assert _format_duration(0) == "0:00:00"


def test_format_duration_one_hour():
    assert _format_duration(3661) == "1:01:01"


def test_shape_activity_converts_distance():
    raw = {
        "id": 1,
        "distance": 40233.6,  # ~25 miles
        "moving_time": 3600,
        "elapsed_time": 3900,
        "location_city": "Ithaca",
        "location_state": "New York",
    }
    shaped = _shape_activity(raw)
    assert shaped["distance"] == 25.0
    assert shaped["moving_time"] == "1:00:00"
    assert shaped["location"] == "Ithaca, New York"


def test_shape_activity_none_distance():
    raw = {"id": 1, "distance": None, "moving_time": None}
    shaped = _shape_activity(raw)
    assert shaped["distance"] is None
    assert shaped["moving_time"] is None


def test_shape_activity_no_location():
    raw = {"id": 1, "location_city": None, "location_state": None}
    shaped = _shape_activity(raw)
    assert shaped["location"] is None


# ── CacheManager with real DB ──────────────────────────────────────────


async def test_get_recent_activities_from_vault(cache_manager, tmp_db, sample_activities_batch):
    """When vault has data, it reads from vault, not API."""
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    result = await cache_manager.get_recent_activities(count=5)
    assert len(result) > 0
    # Client should NOT be called since vault has data
    cache_manager.client.get_activities.assert_not_called()


async def test_get_recent_activities_empty_vault_uses_api(cache_manager):
    """When vault is empty, falls back to API."""
    cache_manager.client.get_activities.return_value = [
        {
            "id": 1,
            "name": "API Ride",
            "distance": 10000,
            "moving_time": 1800,
            "elapsed_time": 1900,
            "sport_type": "Ride",
            "location_city": None,
            "location_state": None,
        }
    ]
    result = await cache_manager.get_recent_activities(count=5)
    assert len(result) == 1
    cache_manager.client.get_activities.assert_called_once()


async def test_get_recent_activities_with_sport_filter(
    cache_manager,
    tmp_db,
    sample_activities_batch,
):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    result = await cache_manager.get_recent_activities(count=10, sport_type="Run")
    assert all(a.get("sport_type") == "Run" or a.get("type") == "Run" for a in result)


async def test_get_activity_caches_result(cache_manager):
    """Second call should hit cache, not API."""
    cache_manager.client.get_activity.return_value = {"id": 999, "name": "Test"}
    await cache_manager.get_activity(999)
    await cache_manager.get_activity(999)
    # Client called only once; second call served from cache
    assert cache_manager.client.get_activity.call_count == 1


async def test_get_activity_streams_cache_key_consistency(cache_manager):
    """Same stream types in different order should hit the same cache."""
    cache_manager.client.get_activity_streams.return_value = {"heartrate": [1, 2, 3]}
    # Call with one order
    await cache_manager.get_activity_streams(999, "heartrate,altitude,distance")
    # Call with different order: should hit cache, not call API again
    await cache_manager.get_activity_streams(999, "distance,heartrate,altitude")
    assert cache_manager.client.get_activity_streams.call_count == 1


async def test_get_athlete_profile_caches(cache_manager):
    await cache_manager.get_athlete_profile()
    await cache_manager.get_athlete_profile()
    assert cache_manager.client.get_athlete.call_count == 1


async def test_get_athlete_stats_chains_profile(cache_manager):
    """get_athlete_stats should first fetch profile to get athlete_id."""
    cache_manager.client.get_athlete_stats.return_value = {"ytd_ride_totals": {"count": 5}}
    await cache_manager.get_athlete_stats()
    cache_manager.client.get_athlete.assert_called()
    cache_manager.client.get_athlete_stats.assert_called_with(12345)


async def test_query_vault(cache_manager, tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    result = await cache_manager.query_vault()
    assert result["total_activities"] == 5
    assert result["total_distance_meters"] > 0
    assert len(result["breakdown_by_type"]) > 0


async def test_query_vault_with_filter(cache_manager, tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    result = await cache_manager.query_vault(sport_type="Ride")
    assert result["filters"]["sport_type"] == "Ride"


async def test_sync_activities_full(cache_manager, tmp_db):
    """Empty vault triggers full sync."""
    activity = {
        "id": 1,
        "name": "A",
        "start_date": "2026-01-01T00:00:00Z",
        "start_date_local": "2026-01-01",
        "sport_type": "Ride",
    }
    cache_manager.client.get_activities.side_effect = [
        [activity],
        [],  # second page empty = done
    ]
    result = await cache_manager.sync_activities()
    assert result["mode"] == "full"
    assert result["activities_fetched"] == 1
    assert result["total_in_vault"] == 1


async def test_sync_activities_incremental(cache_manager, tmp_db, sample_activities_batch):
    """Vault with data triggers incremental sync."""
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    new_activity = {
        "id": 200,
        "name": "New",
        "start_date": "2026-04-10T00:00:00Z",
        "start_date_local": "2026-04-10",
        "sport_type": "Ride",
    }
    cache_manager.client.get_activities.side_effect = [
        [new_activity],
        [],
    ]
    result = await cache_manager.sync_activities()
    assert result["mode"] == "incremental"
    assert result["new_activities"] == 1


async def test_sync_activities_window(cache_manager, tmp_db):
    cache_manager.client.get_activities.side_effect = [
        [],
    ]
    result = await cache_manager.sync_activities(days_back=7)
    assert result["mode"] == "window_7d"


async def test_get_cache_stats(cache_manager, tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    stats = await cache_manager.get_cache_stats()
    assert "vault" in stats
    assert stats["vault"]["total_activities"] == 5
    assert "rate_limit" in stats


async def test_gear_resolution(cache_manager, tmp_db, sample_activity):
    """Activities with gear_id should get gear_name resolved."""
    await tmp_db.upsert_activities_batch([sample_activity])
    result = await cache_manager.get_recent_activities(count=5)
    # The mock client returns {"name": "Trek Domane"} for get_gear
    assert any(a.get("gear_name") == "Trek Domane" for a in result)


# ── Data integrity across re-syncs ─────────────────────────────────────


async def test_window_sync_keeps_location_override(cache_manager, tmp_db, sample_activity):
    """A days_back re-sync re-pulls activities the vault already holds.

    The location a user set by hand must survive it, and must still be what
    get_activities_near reports.
    """
    await tmp_db.upsert_activities_batch([sample_activity])
    await tmp_db.set_location_override(sample_activity["id"], "Ithaca, NY")

    cache_manager.client.get_activities.side_effect = [[sample_activity], []]
    await cache_manager.sync_activities(days_back=30)

    near = await tmp_db.get_activities_near_location(42.4440, -76.5019, radius_miles=1)
    assert [a["_location_override"] for a in near] == ["Ithaca, NY"]


async def test_sync_does_not_shadow_full_activity_detail(cache_manager, sample_activity):
    """Strava's list endpoint returns summaries: no description, gear, or laps.

    After a sync, get_activity must still fetch the full record rather than
    serve the summary the sync just pulled.
    """
    summary = {k: v for k, v in sample_activity.items() if k != "calories"}
    detail = {
        **sample_activity,
        "description": "Cayuga loop with the crew",
        "gear": {"id": "g12345", "name": "Trek Domane"},
        "laps": [{"lap_index": 1}],
    }
    cache_manager.client.get_activities.side_effect = [[summary], []]
    cache_manager.client.get_activity.return_value = detail

    await cache_manager.sync_activities()
    result = await cache_manager.get_activity(sample_activity["id"])

    assert cache_manager.client.get_activity.call_count == 1
    assert result["description"] == "Cayuga loop with the crew"
    assert result["gear"]["name"] == "Trek Domane"


async def test_query_vault_totals_count_every_activity(cache_manager, tmp_db):
    """Totals must cover every matching activity, not the first 1,000."""
    rides = [
        {
            "id": 500000 + i,
            "sport_type": "Ride",
            "distance": 1609.344,  # one mile
            "moving_time": 50,
            "total_elevation_gain": 1.0,
            "start_date": f"2020-01-01T00:{i // 60 % 60:02d}:{i % 60:02d}Z",
            "start_date_local": f"20{20 + i // 400}-01-01T00:00:00",
        }
        for i in range(1200)
    ]
    runs = [
        {
            "id": 900000 + i,
            "sport_type": "Run",
            "distance": 5000.0,
            "moving_time": 1500,
            "total_elevation_gain": 20.0,
            "start_date": "2024-06-01T00:00:00Z",
            "start_date_local": "2024-06-01T00:00:00",
        }
        for i in range(3)
    ]
    # One summary with nulls, as Strava returns for manual entries.
    blank = {
        "id": 999999,
        "sport_type": "Ride",
        "distance": None,
        "moving_time": None,
        "total_elevation_gain": None,
        "start_date": "2024-06-02T00:00:00Z",
        "start_date_local": "2024-06-02T00:00:00",
    }
    await tmp_db.upsert_activities_batch(rides + runs + [blank])

    everything = await cache_manager.query_vault()
    assert everything["total_activities"] == 1204
    assert everything["total_distance_meters"] == pytest.approx(1200 * 1609.344 + 3 * 5000)
    assert everything["total_moving_time_seconds"] == 1200 * 50 + 3 * 1500
    assert everything["total_elevation_meters"] == pytest.approx(1200 * 1.0 + 3 * 20.0)

    only_rides = await cache_manager.query_vault(sport_type="Ride")
    assert only_rides["total_activities"] == 1201
    assert only_rides["total_distance_meters"] / 1609.344 == pytest.approx(1200)
    assert only_rides["total_moving_time_seconds"] == 60000

    # Rides dated 2020, 2021, 2022 (400 each); the date window keeps one year.
    one_year = await cache_manager.query_vault(
        sport_type="Ride", after="2021-01-01", before="2022-01-01"
    )
    assert one_year["total_activities"] == 400
    assert one_year["total_moving_time_seconds"] == 400 * 50


async def test_query_vault_totals_empty_vault_are_zero(cache_manager):
    result = await cache_manager.query_vault()
    assert result["total_activities"] == 0
    assert result["total_distance_meters"] == 0
    assert result["total_moving_time_seconds"] == 0
    assert result["total_elevation_meters"] == 0
