"""API-fallback path on `get_recent_activities` must honor every filter.

Before this fix the fallback path silently dropped sport_type, has_power,
before, and after. The user-facing symptom was Claude calling
`strava_get_recent_activities(sport_type="cycling")` against an empty vault,
seeing walks and Pilates mixed into the result, and concluding the filter
was broken.

These tests pin the behavior so it can't regress.
"""

import pytest


def _make_activity(idx, sport_type, has_power=False, when="2026-04-01T08:00:00"):
    """Minimal activity dict matching the shape Strava returns from /athlete/activities."""
    activity = {
        "id": 10000 + idx,
        "name": f"{sport_type} {idx}",
        "type": sport_type,
        "sport_type": sport_type,
        "distance": 10000.0,
        "moving_time": 1800,
        "elapsed_time": 1900,
        "start_date": when + "Z",
        "start_date_local": when,
        "total_elevation_gain": 100.0,
        "average_speed": 5.5,
    }
    if has_power:
        activity["average_watts"] = 200.0
        activity["weighted_average_watts"] = 215.0
        activity["max_watts"] = 600.0
        activity["kilojoules"] = 360.0
        activity["device_watts"] = True
    return activity


@pytest.fixture
def mixed_api_response():
    """What Strava might return on an unfiltered list call."""
    return [
        _make_activity(1, "Ride", has_power=True),
        _make_activity(2, "Walk"),
        _make_activity(3, "GravelRide", has_power=True),
        _make_activity(4, "Run"),
        _make_activity(5, "Pilates"),
        _make_activity(6, "VirtualRide"),  # ride without power meter
        _make_activity(7, "WeightTraining"),
    ]


async def test_fallback_sport_type_literal_filters_correctly(
    cache_manager, mock_strava_client, mixed_api_response
):
    """sport_type='Ride' (literal CamelCase) returns only Ride rows."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    results = await cache_manager.get_recent_activities(count=10, sport_type="Ride")
    assert {a["sport_type"] for a in results} == {"Ride"}


async def test_fallback_sport_type_alias_expands(
    cache_manager, mock_strava_client, mixed_api_response
):
    """sport_type='rides' expands to all ride types."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    results = await cache_manager.get_recent_activities(count=10, sport_type="rides")
    sports = {a["sport_type"] for a in results}
    assert sports == {"Ride", "GravelRide", "VirtualRide"}
    assert "Walk" not in sports
    assert "Pilates" not in sports


async def test_fallback_sport_type_comma_list(
    cache_manager, mock_strava_client, mixed_api_response
):
    """sport_type='Run,Walk' returns both types."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    results = await cache_manager.get_recent_activities(count=10, sport_type="Run,Walk")
    assert {a["sport_type"] for a in results} == {"Run", "Walk"}


async def test_fallback_has_power_true(cache_manager, mock_strava_client, mixed_api_response):
    """has_power=True drops everything without power data."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    results = await cache_manager.get_recent_activities(count=10, has_power=True)
    assert all(a.get("average_watts") is not None for a in results)
    assert {a["sport_type"] for a in results} == {"Ride", "GravelRide"}


async def test_fallback_has_power_false(cache_manager, mock_strava_client, mixed_api_response):
    """has_power=False keeps only activities without power."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    results = await cache_manager.get_recent_activities(count=10, has_power=False)
    assert all(a.get("average_watts") is None for a in results)


async def test_fallback_combined_filters(cache_manager, mock_strava_client, mixed_api_response):
    """sport_type + has_power combine — only power-meter rides."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    results = await cache_manager.get_recent_activities(
        count=10, sport_type="rides", has_power=True
    )
    assert {a["sport_type"] for a in results} == {"Ride", "GravelRide"}


async def test_fallback_after_before_passed_to_api(cache_manager, mock_strava_client):
    """ISO before/after convert to epoch and reach the Strava client call."""
    mock_strava_client.get_activities.return_value = []
    await cache_manager.get_recent_activities(count=5, after="2026-01-01", before="2026-04-01")
    call_kwargs = mock_strava_client.get_activities.call_args.kwargs
    # 2026-01-01 UTC == 1767225600 epoch; 2026-04-01 UTC == 1774982400 epoch.
    # Allow ±1 day slack because _iso_to_epoch uses local timezone for naive
    # ISO strings — we just want to confirm the call was made with int epochs.
    assert isinstance(call_kwargs["after"], int)
    assert isinstance(call_kwargs["before"], int)
    assert call_kwargs["after"] < call_kwargs["before"]


async def test_fallback_widens_fetch_when_filtering(cache_manager, mock_strava_client):
    """Filtering requests per_page=200 to give client-side filter room to work."""
    mock_strava_client.get_activities.return_value = []
    await cache_manager.get_recent_activities(count=5, sport_type="rides")
    assert mock_strava_client.get_activities.call_args.kwargs["per_page"] == 200


async def test_fallback_no_filter_uses_count(cache_manager, mock_strava_client):
    """Unfiltered fallback only asks for `count` rows from the API."""
    mock_strava_client.get_activities.return_value = []
    await cache_manager.get_recent_activities(count=5)
    assert mock_strava_client.get_activities.call_args.kwargs["per_page"] == 5


async def test_fallback_caches_by_filter_signature(
    cache_manager, mock_strava_client, mixed_api_response
):
    """Different filter combos cache separately so they don't collide."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    # Call 1: unfiltered
    r1 = await cache_manager.get_recent_activities(count=3)
    # Call 2: filtered — must hit the API again (not return cached unfiltered)
    r2 = await cache_manager.get_recent_activities(count=3, sport_type="rides")
    assert mock_strava_client.get_activities.call_count == 2
    # Same filter combo: cache hit, no extra API call
    r3 = await cache_manager.get_recent_activities(count=3, sport_type="rides")
    assert mock_strava_client.get_activities.call_count == 2
    assert {a["sport_type"] for a in r2} == {a["sport_type"] for a in r3}
    # Sanity: unfiltered result includes non-ride types
    sports_unfiltered = {a["sport_type"] for a in r1}
    assert sports_unfiltered - {"Ride", "GravelRide", "VirtualRide"}


async def test_fallback_offset_applied_after_filter(
    cache_manager, mock_strava_client, mixed_api_response
):
    """offset skips post-filter, so paginating filtered results works."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    # 3 rides total; offset=1 skips the first one, count=10 takes the rest.
    results = await cache_manager.get_recent_activities(count=10, offset=1, sport_type="rides")
    assert len(results) == 2


# ── Consistency: query_vault and get_recent_activities use same data source ─
#
# When the vault is empty both tools fall back to the Strava API with the
# same filter semantics, so callers asking the same question get the same
# answer regardless of vault state. This was the root cause of the bug
# where get_recent_activities(sport_type="cycling", after=April) returned
# 16 rides and query_vault with the same filters returned 0.


async def test_query_vault_uses_api_fallback_on_empty_vault(
    cache_manager, mock_strava_client, mixed_api_response
):
    """query_vault on empty vault returns real aggregates from the API."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    result = await cache_manager.query_vault(sport_type="rides")
    # 3 rides in mixed_api_response: Ride, GravelRide, VirtualRide
    assert result["total_activities"] == 3
    assert result["api_fallback"] is True
    sports = {b["sport_type"] for b in result["breakdown_by_type"]}
    assert sports == {"Ride", "GravelRide", "VirtualRide"}


async def test_query_vault_aggregates_power_via_api_fallback(
    cache_manager, mock_strava_client, mixed_api_response
):
    """Power aggregates work on the API fallback path."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    result = await cache_manager.query_vault(sport_type="rides", has_power=True)
    # Two power-meter rides in fixture (Ride + GravelRide, each with 360 kJ)
    assert result["power_rides_count"] == 2
    assert result["total_kilojoules"] == pytest.approx(720.0)
    assert result["avg_weighted_power"] == pytest.approx(215.0)


async def test_query_vault_truncation_flag_when_full_page(cache_manager, mock_strava_client):
    """When the API returns a full page, truncated flag warns of incomplete totals."""
    full_page = [_make_activity(i, "Ride") for i in range(200)]
    mock_strava_client.get_activities.return_value = full_page
    result = await cache_manager.query_vault()
    assert result["truncated"] is True


async def test_query_vault_not_truncated_when_partial_page(
    cache_manager, mock_strava_client, mixed_api_response
):
    """Partial page → truncated False, totals are complete for the window."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    result = await cache_manager.query_vault()
    assert result["truncated"] is False


async def test_get_recent_and_query_vault_agree_on_count(
    cache_manager, mock_strava_client, mixed_api_response
):
    """Same filters, same data source — same answer. This is the regression."""
    mock_strava_client.get_activities.return_value = mixed_api_response
    list_result = await cache_manager.get_recent_activities(count=50, sport_type="rides")
    agg_result = await cache_manager.query_vault(sport_type="rides")
    assert len(list_result) == agg_result["total_activities"]


async def test_query_vault_uses_vault_when_populated(cache_manager, sample_activity):
    """Once the vault has data, query_vault uses it (no API call)."""
    await cache_manager.db.upsert_activity(sample_activity)
    result = await cache_manager.query_vault()
    assert result["total_activities"] >= 1
    assert result["api_fallback"] is False
    # No API call should have happened on the vault path
    cache_manager.client.get_activities.assert_not_called()


def test_format_vault_query_renders_truncation_note():
    """format_vault_query shows the truncation warning when set."""
    from strava_mcp_vault.formatters import format_vault_query

    result = {
        "total_activities": 200,
        "breakdown_by_type": [],
        "total_distance_meters": 0,
        "total_moving_time_seconds": 0,
        "total_elevation_meters": 0,
        "truncated": True,
        "filters": {},
    }
    output = format_vault_query(result)
    assert "most recent 200" in output
    assert "strava_sync_activities" in output


def test_format_vault_query_no_truncation_note_when_complete():
    """No truncation note on a clean result."""
    from strava_mcp_vault.formatters import format_vault_query

    result = {
        "total_activities": 5,
        "breakdown_by_type": [],
        "total_distance_meters": 0,
        "total_moving_time_seconds": 0,
        "total_elevation_meters": 0,
        "truncated": False,
        "filters": {},
    }
    output = format_vault_query(result)
    assert "most recent 200" not in output
