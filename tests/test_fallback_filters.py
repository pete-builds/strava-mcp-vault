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
