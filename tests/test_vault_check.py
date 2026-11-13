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

    report = await cache_manager.check_vault()
    assert sorted(r["id"] for r in report["gone"]) == sorted(a["id"] for a in deleted)
    assert report["missing"] == []

    kept = await cache_manager.repair_vault()
    assert kept["repair"]["removed"] == []
    assert await tmp_db.get_vault_activity_count() == len(season)
    assert "delete_gone=true" in format_vault_check(kept)

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
    assert report["resume"] == {"after": "2026-03-12T10:00:00Z", "before": None}
    # "Gone" is never judged on a part-read window: September is not missing.
    assert report["gone"] is None
    assert report["vault"]["count"] == 3
    text = format_vault_check(report)
    assert "Stopped early" in text
    assert 'run check_vault(after="2026-03-12T10:00:00Z")' in text

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
