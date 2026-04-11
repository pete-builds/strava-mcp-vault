"""Tests for cache/db.py."""

from cache.db import _haversine_miles

# ── Haversine ──────────────────────────────────────────────────────────


def test_haversine_known_distance():
    """Ithaca, NY to Syracuse, NY is roughly 45-55 miles."""
    d = _haversine_miles(42.4440, -76.5019, 43.0481, -76.1474)
    assert 40 < d < 58


def test_haversine_same_point():
    assert _haversine_miles(42.0, -76.0, 42.0, -76.0) == 0.0


# ── Cache CRUD ─────────────────────────────────────────────────────────


async def test_set_and_get_cached(tmp_db):
    await tmp_db.set_cached("key1", "test_cat", {"foo": "bar"}, ttl_seconds=3600)
    result = await tmp_db.get_cached("key1")
    assert result == {"foo": "bar"}


async def test_cache_miss_returns_none(tmp_db):
    result = await tmp_db.get_cached("nonexistent")
    assert result is None


async def test_cache_expiry(tmp_db):
    await tmp_db.set_cached("expire_me", "test_cat", {"x": 1}, ttl_seconds=0)
    # TTL=0 means it expires immediately (expires_at = now + 0)
    # Need a tiny delay so time.time() > expires_at
    import asyncio

    await asyncio.sleep(0.01)
    result = await tmp_db.get_cached("expire_me")
    assert result is None


async def test_invalidate_key(tmp_db):
    await tmp_db.set_cached("k", "cat", {"a": 1}, ttl_seconds=3600)
    await tmp_db.invalidate("k")
    assert await tmp_db.get_cached("k") is None


async def test_invalidate_category(tmp_db):
    await tmp_db.set_cached("k1", "mycat", {"a": 1}, ttl_seconds=3600)
    await tmp_db.set_cached("k2", "mycat", {"b": 2}, ttl_seconds=3600)
    await tmp_db.set_cached("k3", "other", {"c": 3}, ttl_seconds=3600)
    await tmp_db.invalidate_category("mycat")
    assert await tmp_db.get_cached("k1") is None
    assert await tmp_db.get_cached("k2") is None
    assert await tmp_db.get_cached("k3") == {"c": 3}


# ── Cache stats ────────────────────────────────────────────────────────


async def test_cache_stats(tmp_db):
    await tmp_db.set_cached("s1", "cat_a", {"x": 1}, ttl_seconds=3600)
    await tmp_db.get_cached("s1")  # hit
    await tmp_db.get_cached("s1")  # hit
    await tmp_db.get_cached("nope")  # miss

    stats = await tmp_db.get_stats()
    assert stats["total_cached_items"] == 1
    cat_a = stats["categories"].get("cat_a", {})
    assert cat_a["hits"] == 2


# ── Tokens ─────────────────────────────────────────────────────────────


async def test_set_and_get_tokens(tmp_db, monkeypatch):
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    # Reset encryption module so it uses plaintext
    import cache.encryption as enc

    enc._fernet = None
    enc._initialized = False

    await tmp_db.set_tokens("access123", "refresh456", 9999999999)
    tokens = await tmp_db.get_tokens()
    assert tokens["access_token"] == "access123"
    assert tokens["refresh_token"] == "refresh456"
    assert tokens["expires_at"] == 9999999999


async def test_get_tokens_empty(tmp_db):
    tokens = await tmp_db.get_tokens()
    assert tokens is None


# ── Vault operations ───────────────────────────────────────────────────


async def test_upsert_and_get_activity(tmp_db, sample_activity):
    await tmp_db.upsert_activity(sample_activity)
    activities = await tmp_db.get_vault_activities(limit=10)
    assert len(activities) == 1
    assert activities[0]["id"] == sample_activity["id"]


async def test_upsert_activities_batch(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    count = await tmp_db.get_vault_activity_count()
    assert count == 5


async def test_get_vault_activities_with_sport_filter(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    rides = await tmp_db.get_vault_activities(limit=100, sport_type="Ride")
    assert all(a["sport_type"] == "Ride" for a in rides)


async def test_get_vault_activities_with_date_filter(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    recent = await tmp_db.get_vault_activities(limit=100, after="2026-04-02T00:00:00")
    for a in recent:
        assert a["start_date_local"] >= "2026-04-02T00:00:00"


async def test_get_vault_activity_count(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    assert await tmp_db.get_vault_activity_count() == 5
    assert await tmp_db.get_vault_activity_count(sport_type="Run") == 1


async def test_get_vault_sport_type_summary(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    summary = await tmp_db.get_vault_sport_type_summary()
    types = {s["sport_type"] for s in summary}
    assert "Ride" in types
    assert "Run" in types


# ── Geo queries ────────────────────────────────────────────────────────


async def test_get_activities_near_location(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    # Query near Ithaca (42.4440, -76.5019), 10 mile radius
    results = await tmp_db.get_activities_near_location(42.4440, -76.5019, radius_miles=10)
    # Should find the Ithaca-area activities but NOT the snowboard (Vermont)
    ids = {a["id"] for a in results}
    assert 100001 in ids  # Ithaca ride
    assert 100004 not in ids  # Vermont snowboard


async def test_near_location_with_sport_filter(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    results = await tmp_db.get_activities_near_location(
        42.4440, -76.5019, radius_miles=10, sport_type="Run"
    )
    assert all(a["sport_type"] == "Run" for a in results)


# ── Location override ─────────────────────────────────────────────────


async def test_set_location_override(tmp_db, sample_activity):
    await tmp_db.upsert_activity(sample_activity)
    found = await tmp_db.set_location_override(sample_activity["id"], "Custom Location")
    assert found is True


async def test_clear_location_override(tmp_db, sample_activity):
    await tmp_db.upsert_activity(sample_activity)
    await tmp_db.set_location_override(sample_activity["id"], "Custom")
    found = await tmp_db.set_location_override(sample_activity["id"], None)
    assert found is True


async def test_set_location_override_not_found(tmp_db):
    found = await tmp_db.set_location_override(999999, "Nowhere")
    assert found is False


# ── Delete ─────────────────────────────────────────────────────────────


async def test_delete_activities(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    deleted = await tmp_db.delete_activities([100001, 100002])
    assert deleted == 2
    assert await tmp_db.get_vault_activity_count() == 3


async def test_delete_nonexistent_returns_zero(tmp_db):
    deleted = await tmp_db.delete_activities([999999])
    assert deleted == 0


async def test_delete_empty_list(tmp_db):
    deleted = await tmp_db.delete_activities([])
    assert deleted == 0


# ── Sync log ───────────────────────────────────────────────────────────


async def test_update_and_get_sync_log(tmp_db):
    await tmp_db.update_sync_log(42, "incremental")
    log = await tmp_db.get_sync_log()
    assert log["total_synced"] == 42
    assert log["mode"] == "incremental"
    assert log["last_sync_at"] is not None


async def test_get_sync_log_empty(tmp_db):
    assert await tmp_db.get_sync_log() is None


# ── Date range ─────────────────────────────────────────────────────────


async def test_get_vault_date_range(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    dr = await tmp_db.get_vault_date_range()
    assert dr is not None
    assert dr["earliest"] <= dr["latest"]


async def test_get_vault_date_range_empty(tmp_db):
    assert await tmp_db.get_vault_date_range() is None


# ── Latest epoch ───────────────────────────────────────────────────────


async def test_get_latest_activity_epoch(tmp_db, sample_activities_batch):
    await tmp_db.upsert_activities_batch(sample_activities_batch)
    epoch = await tmp_db.get_latest_activity_epoch()
    assert epoch is not None
    assert isinstance(epoch, int)
    assert epoch > 0


async def test_get_latest_activity_epoch_empty(tmp_db):
    assert await tmp_db.get_latest_activity_epoch() is None


# ── Cleanup ────────────────────────────────────────────────────────────


async def test_cleanup_expired(tmp_db):
    await tmp_db.set_cached("old", "test", {"x": 1}, ttl_seconds=0)
    await tmp_db.set_cached("fresh", "test", {"y": 2}, ttl_seconds=3600)
    import asyncio

    await asyncio.sleep(0.01)
    await tmp_db.cleanup_expired()
    assert await tmp_db.get_cached("fresh") == {"y": 2}
