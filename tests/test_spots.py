"""The public ride-spot export publishes only what was explicitly curated.

This file is the gate on a page that faces the internet, so most of it is about
what must NOT come out. Three properties matter more than the happy path:

- An activity Strava does not mark visible to everyone never appears.
- A cluster nobody named never appears, not even under a geocoded fallback.
- No recorded start coordinate appears, only route polylines and curated pins.

A clean export and a broken filter look identical from the outside, which is why
each of these has a test that fails when the guarantee is removed rather than a
test that merely observes today's data.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from strava_mcp_vault.spots import (
    assign_to_spots,
    build_export,
    is_public,
    slugify,
)

SHINDAGIN = {"name": "Shindagin Hollow", "lat": 42.3451, "lon": -76.3505, "radius_miles": 1.0}
HAMMOND = {"name": "Hammond Hill", "lat": 42.4371, "lon": -76.3059, "radius_miles": 1.0}

# Coordinates of the real largest cluster in Pete's vault: 49 road rides out of a
# neighborhood, which outranks every trailhead. Never curated, so never exported.
NEIGHBORHOOD = (42.4696, -76.4696)


def ride(
    activity_id: int,
    lat: float,
    lon: float,
    *,
    visibility: str | None = "everyone",
    sport_type: str = "MountainBikeRide",
    polyline: str = "abcdef",
    date: str = "2026-09-12T10:35:00",
    distance: float = 19198.6,
    elevation: float = 309.0,
    private: int = 0,
    name: str = "A Ride",
) -> dict:
    activity = {
        "id": activity_id,
        "name": name,
        "sport_type": sport_type,
        "type": sport_type,
        "distance": distance,
        "moving_time": 6230,
        "total_elevation_gain": elevation,
        "start_date": date + "Z",
        "start_date_local": date,
        "start_latlng": [lat, lon],
        "private": private,
        "map": {"id": f"a{activity_id}", "summary_polyline": polyline},
    }
    if visibility is not None:
        activity["visibility"] = visibility
    return activity


SPORTS = ["Ride", "MountainBikeRide", "GravelRide"]


# --- The visibility allowlist ---


@pytest.mark.parametrize(
    "visibility,expected",
    [
        ("everyone", True),
        ("followers_only", False),
        ("only_me", False),
        (None, False),
        ("", False),
        ("EVERYONE", False),
        ("some_future_value_strava_invents", False),
    ],
)
def test_only_explicit_everyone_is_public(visibility, expected):
    """Fails closed. An unrecognized or absent value is not publishable."""
    assert is_public(ride(1, *NEIGHBORHOOD, visibility=visibility)) is expected


def test_private_flag_beats_a_public_visibility():
    assert is_public(ride(1, 42.3451, -76.3505, private=1)) is False


def test_a_followers_only_ride_never_reaches_the_export():
    """The exact shape of the one non-public activity in the real vault."""
    activities = [
        ride(1, 42.3451, -76.3505, name="Public ride"),
        ride(2, 42.3452, -76.3506, visibility="followers_only", name="Private ride"),
    ]
    payload = build_export(activities, [SHINDAGIN], SPORTS)

    exported_ids = [a["id"] for s in payload["spots"] for a in s["activities"]]
    assert exported_ids == [1]
    assert payload["privacy"]["withheld_not_public"] == 1
    assert "Private ride" not in json.dumps(payload)


def test_withholding_is_reported_not_silent():
    """A withheld ride is counted, so a miscount is visible rather than invisible."""
    activities = [ride(i, 42.3451, -76.3505, visibility="followers_only") for i in range(3)]
    payload = build_export(activities, [SHINDAGIN], SPORTS)
    assert payload["privacy"]["withheld_not_public"] == 3
    assert payload["spots"] == []


# --- The allowlist, and the neighborhood it exists to keep out ---


def test_an_uncurated_cluster_is_counted_never_named():
    """The home-address case. 40 rides from one point, no spot, nothing published."""
    activities = [ride(i, *NEIGHBORHOOD, sport_type="Ride") for i in range(40)]
    payload = build_export(activities, [SHINDAGIN, HAMMOND], SPORTS)

    assert payload["spots"] == []
    assert payload["unassigned_rides"] == 40
    body = json.dumps(payload)
    assert "42.4696" not in body
    assert "-76.4696" not in body


def test_no_spots_curated_means_nothing_is_exported():
    activities = [ride(1, 42.3451, -76.3505)]
    payload = build_export(activities, [], SPORTS)
    assert payload["spots"] == []
    assert payload["unassigned_rides"] == 1


def test_a_curated_spot_exports_its_rides():
    activities = [
        ride(1, 42.3451, -76.3505, date="2026-09-12T10:00:00"),
        ride(2, 42.3455, -76.3510, date="2025-05-04T10:00:00"),
        ride(3, *NEIGHBORHOOD, sport_type="Ride"),
    ]
    payload = build_export(activities, [SHINDAGIN], SPORTS)

    assert len(payload["spots"]) == 1
    spot = payload["spots"][0]
    assert spot["name"] == "Shindagin Hollow"
    assert spot["slug"] == "shindagin-hollow"
    assert spot["ride_count"] == 2
    assert spot["first_ride"] == "2025-05-04"
    assert spot["last_ride"] == "2026-09-12"
    assert spot["sport_types"] == {"MountainBikeRide": 2}
    assert payload["unassigned_rides"] == 1
    assert payload["totals"]["rides"] == 2


# --- What geometry is allowed out ---


def test_start_coordinates_are_never_exported():
    """The polyline may be trimmed by Strava; start_latlng never is. So it stays in."""
    activities = [ride(1, 42.34517, -76.35051, polyline="xyzzy")]
    payload = build_export(activities, [SHINDAGIN], SPORTS)
    body = json.dumps(payload)

    assert "start_latlng" not in body
    assert "42.34517" not in body
    assert "-76.35051" not in body
    assert "xyzzy" in body
    assert payload["privacy"]["start_coordinates_exported"] is False


def test_the_pin_is_the_curated_coordinate_not_a_ride_start():
    activities = [ride(1, 42.3490, -76.3540)]
    payload = build_export(activities, [SHINDAGIN], SPORTS)
    spot = payload["spots"][0]
    assert (spot["lat"], spot["lon"]) == (SHINDAGIN["lat"], SHINDAGIN["lon"])


def test_a_ride_with_no_polyline_is_dropped():
    activities = [ride(1, 42.3451, -76.3505, polyline="")]
    payload = build_export(activities, [SHINDAGIN], SPORTS)
    assert payload["spots"] == []


def test_units_are_converted_for_the_page():
    activities = [ride(1, 42.3451, -76.3505, distance=19198.6, elevation=309.0)]
    row = build_export(activities, [SHINDAGIN], SPORTS)["spots"][0]["activities"][0]
    assert row["distance_miles"] == pytest.approx(11.93, abs=0.01)
    assert row["elevation_ft"] == 1014


# --- Assignment ---


def test_overlapping_spots_do_not_double_count():
    near_shindagin = {**HAMMOND, "name": "Overlapping", "lat": 42.3455, "lon": -76.3510}
    activities = [ride(1, 42.3451, -76.3505)]
    buckets, unassigned = assign_to_spots(activities, [SHINDAGIN, near_shindagin])
    assigned = sum(len(v) for v in buckets.values())
    assert assigned == 1
    assert unassigned == 0
    assert len(buckets["Shindagin Hollow"]) == 1


def test_a_ride_just_outside_the_radius_is_unassigned():
    tight = {**SHINDAGIN, "radius_miles": 0.1}
    activities = [ride(1, 42.3600, -76.3505)]
    _, unassigned = assign_to_spots(activities, [tight])
    assert unassigned == 1


def test_a_ride_with_no_coordinates_is_unassigned_not_crashing():
    activity = ride(1, 42.3451, -76.3505)
    activity["start_latlng"] = []
    _, unassigned = assign_to_spots([activity], [SHINDAGIN])
    assert unassigned == 1


@pytest.mark.parametrize(
    "name,slug",
    [
        ("Shindagin Hollow", "shindagin-hollow"),
        ("Hammond Hill State Forest", "hammond-hill-state-forest"),
        ("Greek Peak / Virgil", "greek-peak-virgil"),
        ("  Spaced  Out  ", "spaced-out"),
    ],
)
def test_slugify(name, slug):
    assert slugify(name) == slug


# --- The database layer ---


async def test_db_filter_withholds_non_public_activities(tmp_db):
    await tmp_db.upsert_activity(ride(1, 42.3451, -76.3505))
    await tmp_db.upsert_activity(ride(2, 42.3452, -76.3506, visibility="followers_only"))
    await tmp_db.upsert_activity(ride(3, 42.3453, -76.3507, visibility=None))

    rows = await tmp_db.get_public_activities_with_geometry(SPORTS)
    assert [r["id"] for r in rows] == [1]


async def test_db_filter_requires_geometry(tmp_db):
    await tmp_db.upsert_activity(ride(1, 42.3451, -76.3505, polyline=""))
    rows = await tmp_db.get_public_activities_with_geometry(SPORTS)
    assert rows == []


async def test_db_filter_honors_sport_types_and_dates(tmp_db):
    await tmp_db.upsert_activity(ride(1, 42.3451, -76.3505, sport_type="Snowboard"))
    await tmp_db.upsert_activity(ride(2, 42.3451, -76.3505, date="2024-01-01T10:00:00"))
    await tmp_db.upsert_activity(ride(3, 42.3451, -76.3505, date="2026-09-12T10:00:00"))

    rows = await tmp_db.get_public_activities_with_geometry(SPORTS, after="2025-01-01")
    assert [r["id"] for r in rows] == [3]


async def test_ride_spot_round_trip(tmp_db):
    await tmp_db.upsert_ride_spot("Shindagin Hollow", 42.3451, -76.3505, 1.0, "Classic singletrack")
    spots = await tmp_db.get_ride_spots()
    assert len(spots) == 1
    assert spots[0]["name"] == "Shindagin Hollow"
    assert spots[0]["blurb"] == "Classic singletrack"

    await tmp_db.upsert_ride_spot("Shindagin Hollow", 42.3460, -76.3510, 2.0)
    spots = await tmp_db.get_ride_spots()
    assert len(spots) == 1
    assert spots[0]["radius_miles"] == 2.0

    assert await tmp_db.delete_ride_spot("Shindagin Hollow") is True
    assert await tmp_db.get_ride_spots() == []
    assert await tmp_db.delete_ride_spot("Shindagin Hollow") is False


async def test_a_sync_cannot_wipe_curated_spots(tmp_db):
    """Audit finding #1 wipes location_override on re-sync via INSERT OR REPLACE.

    Ride spots live in their own table for exactly this reason. Re-upserting every
    activity the way sync does must leave curation untouched.
    """
    await tmp_db.upsert_ride_spot("Shindagin Hollow", 42.3451, -76.3505)
    await tmp_db.upsert_activities_batch([ride(i, 42.3451, -76.3505) for i in range(5)])
    await tmp_db.upsert_activities_batch([ride(i, 42.3451, -76.3505) for i in range(5)])

    spots = await tmp_db.get_ride_spots()
    assert [s["name"] for s in spots] == ["Shindagin Hollow"]


# --- The tool contract as a client sees it ---


@pytest.fixture(scope="module")
def tools():
    from strava_mcp_vault.server import mcp

    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_export_tool_documents_its_return_shape(tools):
    """Guards against the Args: truncation that silently drops everything below it.

    A caller that cannot see the return shape has to guess at the JSON, and a
    docstring can lose its lower half without any visible symptom.
    """
    description = tools["export_ride_spots"].description
    for expected in ("JSON", "polyline", "spots", "visible to everyone"):
        assert expected in description, f"{expected!r} missing from the tool description"


def test_export_tool_is_marked_local_and_read_only(tools):
    """It reads the vault and makes no Strava call, so a client should not be warned."""
    tool = tools["export_ride_spots"]
    assert tool.annotations is not None
