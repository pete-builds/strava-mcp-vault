"""The vault check: find holes and deletions, repair them, respect the read budget.

Why this exists: sync_activities only asks Strava for activities newer than the
newest one stored, so it can never fill a hole. A real vault lost May and June
2026 while every sync reported success, and a ride-map built from it published
161 rides and 1,814 miles when the truth was 181 and 2,040.4.

No test here reaches the network. The Strava side is either the shared
AsyncMock client from conftest, given a list endpoint that pages like Strava,
or the real StravaClient talking to respx.
"""

import json
import sqlite3
import time
from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from strava_mcp_vault.cache import manager as manager_module
from strava_mcp_vault.cache.db import CacheDB
from strava_mcp_vault.cache.manager import CacheManager
from strava_mcp_vault.clients.strava import StravaClient
from strava_mcp_vault.exceptions import RateLimitError, StravaAPIError
from strava_mcp_vault.formatters import format_cache_stats, format_vault_check

METERS_PER_MILE = 1609.344
LIST_URL = "https://www.strava.com/api/v3/athlete/activities"


def _ride(i: int, month: int, day: int, sport: str = "Ride", miles: float = 10.0) -> dict:
    """A list-endpoint summary. Local time is UTC minus four hours."""
    return {
        "id": 7_000_000 + i,
        "name": f"Ride {i}",
        "type": "Ride",
        "sport_type": sport,
        "distance": miles * METERS_PER_MILE,
        "moving_time": 3600,
        "total_elevation_gain": 100.0,
        "start_date": f"2026-{month:02d}-{day:02d}T14:00:00Z",
        "start_date_local": f"2026-{month:02d}-{day:02d}T10:00:00Z",
        "start_latlng": [42.44, -76.50],
        "visibility": "everyone",
        "athlete": {"id": 12345, "resource_state": 1},
    }


def _season() -> list[dict]:
    """Jan to Sep 2026: three rides a month (one gravel) and one run in March."""
    out, i = [], 0
    for month in range(1, 10):
        for day, sport in ((3, "Ride"), (12, "Ride"), (21, "GravelRide")):
            i += 1
            out.append(_ride(i, month, day, sport, miles=10.0 + i))
    i += 1
    run = _ride(i, 3, 15, "Run", miles=5.0)
    run["type"] = "Run"
    out.append(run)
    return out


def _epoch(a: dict) -> int:
    return int(datetime.fromisoformat(a["start_date"]).timestamp())


def strava_list(activities: list[dict]):
    """A stand-in for GET /athlete/activities: filters on after/before, oldest first."""

    def get_activities(page=1, per_page=30, after=None, before=None):
        rows = sorted(
            (
                a
                for a in activities
                if (after is None or _epoch(a) > after) and (before is None or _epoch(a) < before)
            ),
            key=_epoch,
        )
        return [dict(a) for a in rows[(page - 1) * per_page : page * per_page]]

    return get_activities


def strava_detail(answers: dict):
    """A stand-in for GET /activities/{id}.

    answers maps an id to 404 or another status (raised as StravaAPIError), or
    to "rate" (a read budget refusal). Any other id answers 200: still there.
    """

    async def get_activity(activity_id):
        answer = answers.get(activity_id)
        if answer == "rate":
            raise RateLimitError("Strava read budget used up")
        if isinstance(answer, int):
            raise StravaAPIError(answer, f"/activities/{activity_id}")
        return {"id": activity_id}

    return get_activity


# ── Holes: reported by month, then restored by repair ──────────────────


async def test_missing_month_is_reported_by_month_then_restored(cache_manager, tmp_db):
    season = _season()
    may = [a for a in season if a["start_date_local"].startswith("2026-05")]
    await tmp_db.upsert_activities_batch([a for a in season if a not in may])
    cache_manager.client.get_activities.side_effect = strava_list(season)

    report = await cache_manager.check_vault(
        after="2026-01-01", before="2027-01-01", sport_types="Ride,GravelRide"
    )

    assert report["complete"] is True
    assert sorted(a["id"] for a in report["missing"]) == sorted(a["id"] for a in may)
    by_month = {m["month"]: m for m in report["months"]}
    assert by_month["2026-05"] == {
        "month": "2026-05",
        "strava": 3,
        "vault": 0,
        "missing": 3,
        "gone": 0,
    }
    assert all(m["missing"] == 0 for k, m in by_month.items() if k != "2026-05")
    rides = [a for a in season if a["sport_type"] != "Run"]
    assert report["strava"]["count"] == len(rides) == 27
    assert report["vault"]["count"] == 24
    assert report["strava"]["distance_meters"] == pytest.approx(sum(a["distance"] for a in rides))
    assert report["gone"] == []
    # A check writes nothing.
    assert await tmp_db.get_vault_activity_count() == len(season) - 3

    repaired = await cache_manager.repair_vault(
        after="2026-01-01", before="2027-01-01", sport_types="Ride,GravelRide"
    )
    assert sorted(repaired["repair"]["added"]) == sorted(a["id"] for a in may)
    assert repaired["repair"]["vault_after"]["count"] == report["strava"]["count"]
    assert repaired["repair"]["vault_after"]["distance_meters"] == pytest.approx(
        report["strava"]["distance_meters"]
    )

    again = await cache_manager.check_vault(
        after="2026-01-01", before="2027-01-01", sport_types="Ride,GravelRide"
    )
    assert again["missing"] == [] and again["gone"] == []
    assert again["vault"] == {
        "count": again["strava"]["count"],
        "distance_meters": pytest.approx(again["strava"]["distance_meters"]),
    }


async def test_report_reads_plainly(cache_manager, tmp_db):
    season = _season()
    await tmp_db.upsert_activities_batch(
        [a for a in season if not a["start_date_local"].startswith("2026-06")]
    )
    cache_manager.client.get_activities.side_effect = strava_list(season)

    text = format_vault_check(await cache_manager.check_vault(after="2026-01-01"))

    assert "3 activities on Strava missing from the vault" in text
    assert "| 2026-06 | 3 | 0 | 3 | 0 |" in text
    assert 'run repair_vault(after="2026-01-01")' in text


# ── Deletions on Strava: flagged, removed only on the second flag ──────


async def test_deleted_on_strava_is_flagged_and_only_removed_with_the_flag(cache_manager, tmp_db):
    season = _season()
    await tmp_db.upsert_activities_batch(season)
    deleted = [season[4], season[17]]
    remaining = [a for a in season if a not in deleted]
    cache_manager.client.get_activities.side_effect = strava_list(remaining)
    cache_manager.client.get_activity.side_effect = strava_detail({a["id"]: 404 for a in deleted})

    report = await cache_manager.check_vault()
    assert sorted(r["id"] for r in report["gone"]) == sorted(a["id"] for a in deleted)
    assert report["missing"] == []

    kept = await cache_manager.repair_vault()
    assert kept["repair"]["removed"] == []
    assert await tmp_db.get_vault_activity_count() == len(season)
    kept_text = format_vault_check(kept)
    assert "kept" in kept_text and "token" in kept_text
    assert "pass delete_gone" not in kept_text
    cache_manager.client.get_activity.assert_not_awaited()

    removed = await cache_manager.repair_vault(delete_gone=True)
    assert sorted(r["id"] for r in removed["repair"]["removed"]) == sorted(a["id"] for a in deleted)
    assert await tmp_db.get_vault_activity_count() == len(remaining)
    for a in deleted:
        assert await tmp_db.get_activity_row(a["id"]) is None
    for a in remaining:
        assert await tmp_db.get_activity_row(a["id"]) is not None
    text = format_vault_check(removed)
    assert f"(id {deleted[0]['id']})" in text and "Removed (2)" in text


# ── A repair keeps what a person set by hand (PR #70's guarantee) ──────


async def test_repair_does_not_wipe_a_location_override(cache_manager, tmp_db):
    season = _season()
    target = season[0]
    await tmp_db.upsert_activities_batch(season[:5])
    await tmp_db.set_location_override(target["id"], "Shindagin Hollow")
    renamed = [{**a, "name": "Renamed on Strava"} if a is target else a for a in season]
    cache_manager.client.get_activities.side_effect = strava_list(renamed)

    report = await cache_manager.repair_vault()

    assert report["repair"]["updated"] == 5
    assert await tmp_db.get_activity_row(target["id"]) == {
        **target,
        "name": "Renamed on Strava",
    }
    near = await tmp_db.get_activities_near_location(42.44, -76.50, radius_miles=1)
    overrides = {a["id"]: a.get("_location_override") for a in near}
    assert overrides[target["id"]] == "Shindagin Hollow"


# ── The read budget: stop cleanly, say how far, resume ─────────────────


@pytest.fixture
async def real_client():
    tokens = AsyncMock()
    tokens.get_tokens.return_value = {
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": int(time.time()) + 7200,
    }
    client = StravaClient("id", "secret", cache_db=tokens)
    await client.init_tokens()
    return client


class FakeStrava:
    """respx handler for the list endpoint, reporting read usage per call."""

    def __init__(self, activities, read_usage_start, read_limit=100, fail_on_call=None):
        self.list = strava_list(activities)
        self.usage = read_usage_start
        self.limit = read_limit
        self.calls = 0
        self.fail_on_call = fail_on_call

    def __call__(self, request):
        self.calls += 1
        self.usage += 1
        if self.calls == self.fail_on_call:
            return httpx.Response(429, json={"message": "Rate Limit Exceeded"})
        p = request.url.params
        rows = self.list(
            page=int(p["page"]),
            per_page=int(p["per_page"]),
            after=int(p["after"]) if "after" in p else None,
            before=int(p["before"]) if "before" in p else None,
        )
        headers = {
            "X-RateLimit-Usage": f"{self.usage},{self.usage}",
            "X-RateLimit-Limit": "200,2000",
            "X-ReadRateLimit-Usage": f"{self.usage},{self.usage}",
            "X-ReadRateLimit-Limit": f"{self.limit},1000",
        }
        return httpx.Response(200, json=rows, headers=headers)


@respx.mock
async def test_budget_stop_is_clean_and_a_second_run_resumes(tmp_db, real_client, monkeypatch):
    monkeypatch.setattr(manager_module, "LIST_PAGE_SIZE", 4)
    season = _season()  # 28 activities, 7 pages of 4
    # The vault holds January only, plus one ride from September.
    sept = season[-2]
    await tmp_db.upsert_activities_batch(season[:3] + [sept])
    mgr = CacheManager(tmp_db, real_client)

    # Strava reports read usage 94, then 95: after the second call only 5 of
    # 100 are left, which is the reserve, so the third page is never asked for.
    fake = FakeStrava(season, read_usage_start=93)
    respx.get(LIST_URL).mock(side_effect=fake)

    report = await mgr.check_vault(after="2026-01-01")

    assert report["complete"] is False
    assert fake.calls == 2 and report["api_calls"] == 2
    assert "5 Strava read calls left" in report["stop_reason"]
    assert "15-minute read limit resets at" in report["stop_reason"]
    # Oldest first: eight activities seen, January into early March.
    assert report["checked_through"] == "2026-03-12T10:00:00Z"
    # Resume a day early: a rider who crossed time zones can have a later
    # ride with an earlier local time. The upsert makes the overlap harmless.
    assert report["resume"] == {"after": "2026-03-11T10:00:00", "before": None}
    # "Gone" is never judged on a part-read window: September is not missing.
    assert report["gone"] is None
    assert report["vault"]["count"] == 3
    text = format_vault_check(report)
    assert "Stopped early" in text
    assert 'run check_vault(after="2026-03-11T10:00:00")' in text

    # Without a window reset the client still knows only 5 are left, so a
    # second run right away fetches nothing at all rather than spend them.
    blocked = await mgr.check_vault(after="2026-01-01")
    assert blocked["api_calls"] == 0 and fake.calls == 2
    assert blocked["resume"] == {"after": "2026-01-01", "before": None}

    # After the 15-minute window resets, a repair stopped the same way keeps
    # what it fetched and deletes nothing, even when asked to, because
    # September was simply never read.
    real_client._now = lambda: time.time() + 900
    fake.usage = 93
    fake.calls = 0
    partial = await mgr.repair_vault(after="2026-01-01", delete_gone=True)
    assert partial["complete"] is False
    assert partial["repair"]["removed"] == []
    assert "Nothing was deleted" in partial["repair"]["delete_note"]
    assert len(partial["repair"]["added"]) == 5  # Feb x3, Mar 3 and Mar 12
    assert await tmp_db.get_activity_row(sept["id"]) is not None
    history = await tmp_db.get_sync_history()
    assert history[0]["mode"] == "repair" and history[0]["completed"] is False

    # Another window later, resume from the hint the report gave.
    real_client._now = lambda: time.time() + 1800
    fake.usage = 0
    resumed = await mgr.repair_vault(**partial["resume"])
    assert resumed["complete"] is True
    final = await mgr.check_vault(after="2026-01-01")
    assert final["complete"] is True
    assert final["missing"] == [] and final["gone"] == []
    assert final["vault"]["count"] == final["strava"]["count"] == len(season)
    assert final["vault"]["distance_meters"] == pytest.approx(sum(a["distance"] for a in season))
    assert (await tmp_db.get_sync_history())[0]["completed"] is True


@respx.mock
async def test_a_real_429_mid_check_is_reported_not_raised(tmp_db, real_client, monkeypatch):
    monkeypatch.setattr(manager_module, "LIST_PAGE_SIZE", 4)
    season = _season()
    fake = FakeStrava(season, read_usage_start=0, fail_on_call=2)
    respx.get(LIST_URL).mock(side_effect=fake)

    report = await CacheManager(tmp_db, real_client).check_vault()

    assert report["complete"] is False
    assert "rate limit" in report["stop_reason"].lower()
    assert report["checked_through"] == "2026-02-03T10:00:00Z"
    assert report["api_calls"] == 2  # the 429 reached Strava, so it counts


async def test_check_rejects_a_bad_window(cache_manager):
    with pytest.raises(ValueError, match="ISO date"):
        await cache_manager.check_vault(after="last May")
    with pytest.raises(ValueError, match="earlier than"):
        await cache_manager.check_vault(after="2026-06-01", before="2026-05-01")
    cache_manager.client.get_activities.assert_not_called()


# ── Sync history ───────────────────────────────────────────────────────


async def test_every_sync_appends_history_and_a_failed_one_says_so(cache_manager, tmp_db):
    season = _season()
    cache_manager.client.get_activities.side_effect = strava_list(season)
    await cache_manager.sync_activities()

    cache_manager.client.get_activities.side_effect = RuntimeError("network down")
    with pytest.raises(RuntimeError):
        await cache_manager.sync_activities(days_back=7)

    history = await tmp_db.get_sync_history()
    assert [(h["mode"], h["completed"]) for h in history] == [
        ("window_7d", False),
        ("full", True),
    ]
    assert history[1]["added"] == len(season)
    assert "network down" in history[0]["note"]
    stats = await cache_manager.get_cache_stats()
    text = format_cache_stats(stats)
    assert "Recent syncs" in text and "| window_7d |" in text and "| NO |" in text


# ── A vault created before sync_history existed upgrades in place ──────


OLD_LAYOUT = """
CREATE TABLE cache (cache_key TEXT PRIMARY KEY, category TEXT NOT NULL, data TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE cache_stats (category TEXT PRIMARY KEY, hits INTEGER DEFAULT 0,
                          misses INTEGER DEFAULT 0);
CREATE TABLE tokens (id INTEGER PRIMARY KEY CHECK (id = 1), access_token TEXT NOT NULL,
                     refresh_token TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE activities (id INTEGER PRIMARY KEY, data TEXT NOT NULL, start_date TEXT,
                         start_date_local TEXT, sport_type TEXT, synced_at REAL NOT NULL);
CREATE TABLE sync_log (id INTEGER PRIMARY KEY CHECK (id = 1), last_sync_at REAL,
                       total_synced INTEGER DEFAULT 0, mode TEXT);
"""


async def test_an_old_layout_vault_gains_history_and_keeps_its_data(tmp_path, mock_strava_client):
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(OLD_LAYOUT)
    old = _ride(1, 1, 3)
    raw.execute(
        "INSERT INTO activities VALUES (?, ?, ?, ?, ?, ?)",
        (old["id"], json.dumps(old), old["start_date"], old["start_date_local"], "Ride", 1.0),
    )
    raw.execute("INSERT INTO sync_log VALUES (1, 1700000000.0, 1, 'incremental')")
    raw.commit()
    raw.close()

    db = CacheDB(path)
    await db.init()
    try:
        assert await db.get_sync_history() == []
        assert await db.get_meta("athlete_id") is None  # vault_meta was added too
        assert await db.get_activity_row(old["id"]) == old
        assert (await db.get_sync_log())["mode"] == "incremental"

        mock_strava_client.get_activities.side_effect = strava_list([old, _ride(2, 1, 12)])
        await CacheManager(db, mock_strava_client).sync_activities()
        history = await db.get_sync_history()
        assert [(h["mode"], h["added"], h["completed"]) for h in history] == [
            ("incremental", 1, True)
        ]
    finally:
        await db.close()

    # Starting again on the upgraded file changes nothing and loses nothing.
    db = CacheDB(path)
    await db.init()
    try:
        assert len(await db.get_sync_history()) == 1
    finally:
        await db.close()


# ── Review findings: a ride is deleted only when Strava confirms it ────
# "The scan read the whole window" is weak proof a ride is gone: Strava can
# answer an empty list, the token can belong to someone else, a deletion
# between pages shifts a live ride out of sight, and a narrow token scope
# hides private rides. Each test below is one of those.


async def test_f1a_each_deletion_is_confirmed_with_strava_first(cache_manager, tmp_db):
    season = _season()
    await tmp_db.upsert_activities_batch(season)
    deleted, shifted, erroring = season[2], season[8], season[14]
    listed = [a for a in season if a not in (deleted, shifted, erroring)]
    cache_manager.client.get_activities.side_effect = strava_list(listed)
    cache_manager.client.get_activity.side_effect = strava_detail(
        {deleted["id"]: 404, erroring["id"]: 500}
    )

    report = await cache_manager.repair_vault(delete_gone=True)

    assert [r["id"] for r in report["repair"]["removed"]] == [deleted["id"]]
    kept = {k["id"]: k["reason"] for k in report["repair"]["kept"]}
    assert set(kept) == {shifted["id"], erroring["id"]}
    assert "still on Strava" in kept[shifted["id"]]
    assert "500" in kept[erroring["id"]]
    assert await tmp_db.get_activity_row(shifted["id"]) is not None
    assert await tmp_db.get_vault_activity_count() == len(season) - 1
    text = format_vault_check(report)
    assert "Removed (1)" in text and f"(id {shifted['id']})" in text


@pytest.mark.parametrize("how", ["reserve reached", "refused by the client"])
async def test_f1a_confirmation_stops_when_the_read_budget_runs_out(cache_manager, tmp_db, how):
    season = _season()
    await tmp_db.upsert_activities_batch(season)
    first, second = season[2], season[8]
    cache_manager.client.get_activities.side_effect = strava_list(
        [a for a in season if a not in (first, second)]
    )
    client = cache_manager.client
    if how == "reserve reached":
        client.get_activity.side_effect = strava_detail({first["id"]: 404, second["id"]: 404})
        # Plenty left until the first confirmation, then only the reserve.
        client.read_budget_remaining.side_effect = lambda: (
            None if client.get_activity.await_count == 0 else 5
        )
    else:
        client.get_activity.side_effect = strava_detail({first["id"]: 404, second["id"]: "rate"})

    report = await cache_manager.repair_vault(delete_gone=True)

    assert [r["id"] for r in report["repair"]["removed"]] == [first["id"]]
    assert [k["id"] for k in report["repair"]["kept"]] == [second["id"]]
    assert "read" in report["repair"]["delete_note"].lower()
    assert await tmp_db.get_activity_row(second["id"]) is not None


async def test_f1b_an_empty_answer_from_strava_deletes_nothing(cache_manager, tmp_db):
    season = _season()
    await tmp_db.upsert_activities_batch(season)
    cache_manager.client.get_activities.side_effect = strava_list([])
    cache_manager.client.get_activity.side_effect = strava_detail({a["id"]: 404 for a in season})

    report = await cache_manager.repair_vault(delete_gone=True, max_delete=1000)

    assert report["repair"]["removed"] == []
    assert "no activities" in report["repair"]["delete_note"]
    assert await tmp_db.get_vault_activity_count() == len(season)
    cache_manager.client.get_activity.assert_not_awaited()


async def test_f1c_a_mass_delete_is_refused_unless_max_delete_allows_it(cache_manager, tmp_db):
    season = _season()
    await tmp_db.upsert_activities_batch(season)
    gone = season[:12]
    cache_manager.client.get_activities.side_effect = strava_list(season[12:])
    cache_manager.client.get_activity.side_effect = strava_detail({a["id"]: 404 for a in gone})

    refused = await cache_manager.repair_vault(delete_gone=True)
    assert refused["repair"]["removed"] == []
    assert sorted(refused["repair"]["would_delete"]) == sorted(a["id"] for a in gone)
    assert (
        "12" in refused["repair"]["delete_note"]
        and "max_delete" in refused["repair"]["delete_note"]
    )
    assert await tmp_db.get_vault_activity_count() == len(season)
    cache_manager.client.get_activity.assert_not_awaited()

    allowed = await cache_manager.repair_vault(delete_gone=True, max_delete=12)
    assert len(allowed["repair"]["removed"]) == 12
    assert await tmp_db.get_vault_activity_count() == len(season) - 12

    with pytest.raises(ValueError, match="max_delete"):
        await cache_manager.repair_vault(delete_gone=True, max_delete=-1)


async def test_f1d_another_accounts_token_refuses_repair_and_delete(cache_manager, tmp_db):
    season = _season()
    await tmp_db.upsert_activities_batch(season)
    stranger = [{**_ride(500 + i, 2, 1 + i), "athlete": {"id": 999}} for i in range(5)]
    cache_manager.client.get_activities.side_effect = strava_list(stranger)
    cache_manager.client.get_activity.side_effect = strava_detail({a["id"]: 404 for a in season})

    check = await cache_manager.check_vault()
    assert "999" in check["athlete_warning"] and "12345" in check["athlete_warning"]

    for delete_gone in (False, True):
        report = await cache_manager.repair_vault(delete_gone=delete_gone, max_delete=1000)
        assert "999" in report["repair"]["refused"]
        assert report["repair"]["added"] == [] and report["repair"]["removed"] == []
    assert await tmp_db.get_vault_activity_count() == len(season)
    for a in stranger:
        assert await tmp_db.get_activity_row(a["id"]) is None
    assert "Nothing was stored or deleted" in format_vault_check(report)


async def test_f1d_a_new_vault_records_the_signed_in_athlete_once(cache_manager, tmp_db):
    season = _season()
    cache_manager.client.get_activities.side_effect = strava_list(season)

    await cache_manager.repair_vault()
    assert await tmp_db.get_meta("athlete_id") == "12345"
    assert cache_manager.client.get_athlete.await_count == 1
    assert await tmp_db.get_vault_activity_count() == len(season)


async def test_f1d_a_recorded_athlete_refuses_a_different_account(cache_manager, tmp_db):
    await tmp_db.set_meta("athlete_id", "12345")
    stranger = [{**_ride(500 + i, 2, 1 + i), "athlete": {"id": 999}} for i in range(3)]
    cache_manager.client.get_activities.side_effect = strava_list(stranger)

    report = await cache_manager.repair_vault()

    assert "999" in report["repair"]["refused"]
    assert await tmp_db.get_vault_activity_count() == 0
    cache_manager.client.get_athlete.assert_not_awaited()


async def test_f1_a_private_ride_is_never_deleted(cache_manager, tmp_db):
    season = _season()
    private = {**season[5], "visibility": "only_me", "private": True}
    season[5] = private
    await tmp_db.upsert_activities_batch(season)
    cache_manager.client.get_activities.side_effect = strava_list(
        [a for a in season if a is not private]
    )
    cache_manager.client.get_activity.side_effect = strava_detail({private["id"]: 404})

    report = await cache_manager.repair_vault(delete_gone=True)

    assert report["repair"]["removed"] == []
    assert "private" in report["repair"]["kept"][0]["reason"]
    assert await tmp_db.get_activity_row(private["id"]) is not None


# ── F2: "matches" means counts AND distance; changed rides are named ───


async def test_f2_matches_needs_equal_distance_too(cache_manager, tmp_db):
    season = _season()
    await tmp_db.upsert_activities_batch(season)
    edited = [{**a, "distance": a["distance"] + 50_000} if a is season[3] else a for a in season]
    cache_manager.client.get_activities.side_effect = strava_list(edited)

    before = await cache_manager.check_vault()
    assert before["missing"] == [] and before["gone"] == []
    assert before["matches"] is False
    assert "matches Strava" not in format_vault_check(before)

    await cache_manager.repair_vault()
    after = await cache_manager.check_vault()
    assert after["matches"] is True
    assert "the vault matches Strava" in format_vault_check(after)


@pytest.mark.parametrize("change", ["retyped", "redated"])
async def test_f2_f3_a_ride_changed_on_strava_is_changed_not_gone(cache_manager, tmp_db, change):
    season = _season()
    target = season[0]  # 3 Jan, a Ride
    await tmp_db.upsert_activities_batch(season)
    if change == "retyped":
        moved = {**target, "sport_type": "VirtualRide"}
    else:  # moved to New Year's Eve: still inside the padding, outside the window
        moved = {
            **target,
            "start_date": "2026-01-01T02:00:00Z",
            "start_date_local": "2025-12-31T21:00:00Z",
        }
    strava = [moved if a is target else a for a in season]
    cache_manager.client.get_activities.side_effect = strava_list(strava)
    cache_manager.client.get_activity.side_effect = strava_detail({target["id"]: 404})
    window = {"after": "2026-01-01", "sport_types": "Ride,GravelRide"}

    report = await cache_manager.check_vault(**window)
    assert report["gone"] == []
    assert [c["id"] for c in report["changed"]] == [target["id"]]
    assert report["matches"] is False
    assert f"(id {target['id']})" in format_vault_check(report)

    repaired = await cache_manager.repair_vault(delete_gone=True, **window)
    assert repaired["repair"]["removed"] == []
    assert await tmp_db.get_activity_row(target["id"]) == moved
    again = await cache_manager.check_vault(**window)
    assert again["changed"] == [] and again["matches"] is True


# ── F3: the guards that keep deletes honest ────────────────────────────


async def test_f3_a_ride_just_after_midnight_east_of_utc_is_not_gone(cache_manager, tmp_db):
    tokyo = {
        **_ride(90, 1, 1),
        "start_date": "2025-12-31T23:00:00Z",
        "start_date_local": "2026-01-01T08:00:00Z",
    }
    rides = [tokyo, *_season()]
    await tmp_db.upsert_activities_batch(rides)
    cache_manager.client.get_activities.side_effect = strava_list(rides)

    report = await cache_manager.check_vault(after="2026-01-01")

    assert report["gone"] == []
    assert report["matches"] is True


async def test_f3_f6_an_error_page_raises_deletes_nothing_and_is_dated(
    cache_manager, tmp_db, monkeypatch
):
    monkeypatch.setattr(manager_module, "LIST_PAGE_SIZE", 4)
    season = _season()
    await tmp_db.upsert_activities_batch(season)
    pages = strava_list(season)

    def failing(page=1, per_page=30, after=None, before=None):
        if page == 3:
            raise StravaAPIError(500, "/athlete/activities", "Internal Server Error")
        return pages(page=page, per_page=per_page, after=after, before=before)

    cache_manager.client.get_activities.side_effect = failing
    cache_manager.client.get_activity.side_effect = strava_detail({a["id"]: 404 for a in season})

    with pytest.raises(StravaAPIError):
        await cache_manager.check_vault()
    with pytest.raises(StravaAPIError):
        await cache_manager.repair_vault(delete_gone=True, max_delete=1000)

    assert await tmp_db.get_vault_activity_count() == len(season)
    history = await tmp_db.get_sync_history()
    assert history[0]["mode"] == "repair" and history[0]["completed"] is False
    assert "500" in history[0]["note"]


# ── F4: resume overlaps a day, so a time-zone hop is not skipped ───────


@respx.mock
async def test_f4_resume_does_not_skip_a_ride_across_time_zones(tmp_db, real_client, monkeypatch):
    monkeypatch.setattr(manager_module, "LIST_PAGE_SIZE", 1)
    tokyo = {
        **_ride(1, 3, 10),
        "start_date": "2026-03-10T03:00:00Z",
        "start_date_local": "2026-03-10T12:00:00Z",
    }
    # Two hours later in UTC, but the evening before in local time.
    hawaii = {
        **_ride(2, 3, 9),
        "start_date": "2026-03-10T05:00:00Z",
        "start_date_local": "2026-03-09T19:00:00Z",
    }
    fake = FakeStrava([tokyo, hawaii], read_usage_start=94)
    respx.get(LIST_URL).mock(side_effect=fake)
    respx.get("https://www.strava.com/api/v3/athlete").mock(
        return_value=httpx.Response(200, json={"id": 12345})
    )
    mgr = CacheManager(tmp_db, real_client)

    partial = await mgr.repair_vault(after="2026-03-01")
    assert partial["complete"] is False
    assert partial["checked_through"] == tokyo["start_date_local"]

    real_client._now = lambda: time.time() + 900
    fake.usage = 0
    await mgr.repair_vault(**partial["resume"])

    assert await tmp_db.get_activity_row(hawaii["id"]) == hawaii


# ── F5: window dates are normalised, not compared as typed ─────────────


async def test_f5_compact_and_spaced_dates_are_normalised(cache_manager, tmp_db):
    season = _season()
    await tmp_db.upsert_activities_batch(season)
    cache_manager.client.get_activities.side_effect = strava_list(season)

    report = await cache_manager.check_vault(after="20260101", before="2026-07-01 00:00")

    assert report["window"]["after"] == "2026-01-01"
    assert report["window"]["before"] == "2026-07-01T00:00:00"
    assert report["strava"]["count"] == report["vault"]["count"] == 19
    assert report["matches"] is True


# ── F6: a failed history write never hides the real error ─────────────


async def test_f6_a_failed_history_write_does_not_hide_the_real_error(
    cache_manager, tmp_db, monkeypatch
):
    async def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(tmp_db, "add_sync_history", broken)
    cache_manager.client.get_activities.side_effect = RuntimeError("network down")

    with pytest.raises(RuntimeError, match="network down"):
        await cache_manager.sync_activities()
    with pytest.raises(RuntimeError, match="network down"):
        await cache_manager.repair_vault()
