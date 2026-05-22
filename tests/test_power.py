"""Power-meter data: DB columns, has_power filter, formatters, query aggregates."""

import pytest

from strava_mcp_vault.formatters import (
    format_activity_detail,
    format_recent_activities,
    format_recent_activities_compact,
    format_vault_query,
)

# ── DB layer: columns + backfill + filter ─────────────────────────────


async def test_power_columns_exist_after_init(tmp_db):
    """Migration adds the five power columns to existing DBs."""
    cursor = await tmp_db._db.execute("PRAGMA table_info(activities)")
    rows = await cursor.fetchall()
    col_names = {row[1] for row in rows}
    assert {
        "average_watts",
        "weighted_average_watts",
        "max_watts",
        "kilojoules",
        "device_watts",
    }.issubset(col_names)


async def test_upsert_populates_power_columns(tmp_db, sample_power_ride):
    """Future syncs write power into the dedicated columns."""
    await tmp_db.upsert_activity(sample_power_ride)
    cursor = await tmp_db._db.execute(
        "SELECT average_watts, weighted_average_watts, max_watts, kilojoules, device_watts "
        "FROM activities WHERE id = ?",
        (sample_power_ride["id"],),
    )
    row = await cursor.fetchone()
    assert row == (195.0, 220.0, 841.0, 1247.0, 1)


async def test_upsert_batch_populates_power_columns(
    tmp_db, sample_power_ride, sample_estimated_power_ride
):
    await tmp_db.upsert_activities_batch([sample_power_ride, sample_estimated_power_ride])
    cursor = await tmp_db._db.execute(
        "SELECT id, average_watts, device_watts FROM activities ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert (sample_estimated_power_ride["id"], 150.0, 0) in rows
    assert (sample_power_ride["id"], 195.0, 1) in rows


async def test_upsert_skips_power_for_non_power_activity(tmp_db, sample_activity):
    """Activities without power leave the columns NULL."""
    await tmp_db.upsert_activity(sample_activity)
    cursor = await tmp_db._db.execute(
        "SELECT average_watts FROM activities WHERE id = ?", (sample_activity["id"],)
    )
    row = await cursor.fetchone()
    assert row[0] is None


async def test_has_power_filter_true(
    tmp_db, sample_activity, sample_power_ride, sample_estimated_power_ride
):
    """has_power=True returns only activities with power data."""
    await tmp_db.upsert_activities_batch(
        [sample_activity, sample_power_ride, sample_estimated_power_ride]
    )
    results = await tmp_db.get_vault_activities(limit=100, has_power=True)
    ids = {a["id"] for a in results}
    assert ids == {sample_power_ride["id"], sample_estimated_power_ride["id"]}


async def test_has_power_filter_false(tmp_db, sample_activity, sample_power_ride):
    """has_power=False returns only activities without power data."""
    await tmp_db.upsert_activities_batch([sample_activity, sample_power_ride])
    results = await tmp_db.get_vault_activities(limit=100, has_power=False)
    ids = {a["id"] for a in results}
    assert ids == {sample_activity["id"]}


async def test_has_power_filter_none_returns_all(tmp_db, sample_activity, sample_power_ride):
    """Default has_power=None applies no power filter."""
    await tmp_db.upsert_activities_batch([sample_activity, sample_power_ride])
    results = await tmp_db.get_vault_activities(limit=100)
    assert len(results) == 2


async def test_has_power_count(tmp_db, sample_activity, sample_power_ride):
    """get_vault_activity_count respects has_power."""
    await tmp_db.upsert_activities_batch([sample_activity, sample_power_ride])
    assert await tmp_db.get_vault_activity_count(has_power=True) == 1
    assert await tmp_db.get_vault_activity_count(has_power=False) == 1
    assert await tmp_db.get_vault_activity_count() == 2


# ── Backfill from existing JSON blobs (simulating pre-migration state) ─


async def test_backfill_populates_columns_from_json_blob(tmp_db, sample_power_ride):
    """Activities stored before the migration get power columns from the JSON blob."""
    import json
    import time

    # Simulate a pre-migration row: data blob has power fields, but columns
    # are NULL because the row was inserted before the new columns existed.
    await tmp_db._db.execute(
        "INSERT INTO activities (id, data, start_date, start_date_local, sport_type, "
        "average_watts, weighted_average_watts, max_watts, kilojoules, device_watts, synced_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?)",
        (
            sample_power_ride["id"],
            json.dumps(sample_power_ride),
            sample_power_ride["start_date"],
            sample_power_ride["start_date_local"],
            sample_power_ride["sport_type"],
            time.time(),
        ),
    )
    await tmp_db._db.commit()

    # Run init() again — the backfill UPDATE should fill the columns from JSON.
    await tmp_db.init()

    cursor = await tmp_db._db.execute(
        "SELECT average_watts, kilojoules, device_watts FROM activities WHERE id = ?",
        (sample_power_ride["id"],),
    )
    row = await cursor.fetchone()
    assert row == (195.0, 1247.0, 1)


# ── Formatters: detail Power section ─────────────────────────────────


def test_format_activity_detail_renders_power_section(sample_power_ride):
    output = format_activity_detail(sample_power_ride)
    assert "### ⚡ Power" in output
    assert "Avg Power" in output
    assert "195 W" in output
    assert "Weighted Avg Power" in output
    assert "220 W" in output
    assert "Max Power" in output
    assert "841 W" in output
    assert "Work" in output
    assert "1,247 kJ" in output
    assert "Source:" in output
    assert "Power meter" in output


def test_format_activity_detail_power_marks_estimated(sample_estimated_power_ride):
    output = format_activity_detail(sample_estimated_power_ride)
    assert "Estimated" in output
    assert "Power meter" not in output


def test_format_activity_detail_skips_power_section_when_no_data(sample_activity):
    output = format_activity_detail(sample_activity)
    assert "⚡ Power" not in output


# ── Formatters: recent activities (full + compact) ───────────────────


def test_format_recent_activities_adds_power_row_for_ride(sample_power_ride):
    # Shape the ride the way the manager would
    from strava_mcp_vault.cache.manager import _shape_activity

    shaped = _shape_activity(sample_power_ride)
    output = format_recent_activities([shaped])
    assert "⚡ Avg" in output
    assert "195 W" in output
    assert "NP" in output
    assert "220 W" in output


def test_format_recent_activities_omits_power_row_for_non_power(sample_activity):
    from strava_mcp_vault.cache.manager import _shape_activity

    shaped = _shape_activity(sample_activity)
    output = format_recent_activities([shaped])
    assert "⚡ Avg" not in output


def test_format_recent_activities_compact_includes_power_columns(
    sample_power_ride, sample_activity
):
    from strava_mcp_vault.cache.manager import _shape_activity

    shaped = [_shape_activity(sample_power_ride), _shape_activity(sample_activity)]
    output = format_recent_activities_compact(shaped)
    # Header has the new columns
    assert "Avg W" in output
    assert "NP" in output
    # Power ride shows numbers
    assert "195" in output
    assert "220" in output
    # Non-power row uses em-dash
    assert "—" in output


# ── query_vault aggregates ───────────────────────────────────────────


async def test_query_vault_aggregates_power(
    cache_manager, sample_activity, sample_power_ride, sample_estimated_power_ride
):
    await cache_manager.db.upsert_activities_batch(
        [sample_activity, sample_power_ride, sample_estimated_power_ride]
    )
    result = await cache_manager.query_vault()
    # Both power rides count regardless of device_watts
    assert result["power_rides_count"] == 2
    # Total work is sum of kJ across both
    assert result["total_kilojoules"] == pytest.approx(1247.0 + 720.0)
    # Avg weighted is the mean of the two weighted_average_watts samples
    assert result["avg_weighted_power"] == pytest.approx((220.0 + 165.0) / 2)


async def test_query_vault_omits_power_aggregates_when_no_power(cache_manager, sample_activity):
    await cache_manager.db.upsert_activities_batch([sample_activity])
    result = await cache_manager.query_vault()
    assert result["power_rides_count"] == 0
    assert result["total_kilojoules"] is None
    assert result["avg_weighted_power"] is None


def test_format_vault_query_renders_power_section_when_present():
    result = {
        "total_activities": 2,
        "breakdown_by_type": [],
        "total_distance_meters": 100000,
        "total_moving_time_seconds": 3600,
        "total_elevation_meters": 500,
        "total_kilojoules": 1900.0,
        "avg_weighted_power": 195.0,
        "power_rides_count": 2,
        "filters": {},
    }
    output = format_vault_query(result)
    assert "⚡ Power" in output
    assert "Power-meter rides" in output
    assert "1,900 kJ" in output
    assert "195 W" in output


def test_format_vault_query_omits_power_section_when_absent():
    result = {
        "total_activities": 1,
        "breakdown_by_type": [],
        "total_distance_meters": 100000,
        "total_moving_time_seconds": 3600,
        "total_elevation_meters": 500,
        "total_kilojoules": None,
        "avg_weighted_power": None,
        "power_rides_count": 0,
        "filters": {},
    }
    output = format_vault_query(result)
    assert "⚡ Power" not in output
