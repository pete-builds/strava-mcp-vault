"""Cache-aside orchestration layer.

Sits between MCP tools (server.py) and the Strava API client.
Tools call the manager, which checks the local vault (SQLite) first
and falls back to the API on a cache miss.

Vault vs Cache:
- Vault (activities table): Permanent storage for activity summaries.
  Populated via sync_activities. Never expires.
- Cache (cache table): TTL-based storage for detailed data (streams,
  full activity detail, athlete profile/stats). Expires per category.
"""

import logging
import time

METERS_PER_MILE = 1609.344

logger = logging.getLogger(__name__)

TTL = {
    "activities_list": 3600,       # 1 hour
    "activity_detail": 86400,      # 24 hours
    "activity_streams": 604800,    # 7 days
    "athlete_profile": 86400,      # 24 hours
    "athlete_stats": 86400,        # 1 day
}

# Fields to extract when shaping activity list responses
_ACTIVITY_LIST_FIELDS = [
    "id",
    "name",
    "type",
    "sport_type",
    "distance",
    "moving_time",
    "elapsed_time",
    "start_date_local",
    "total_elevation_gain",
    "average_speed",
    "max_speed",
    "average_heartrate",
    "max_heartrate",
    "calories",
    "gear_id",
    # Location
    "location_city",
    "location_state",
    "location_country",
    # Social / effort
    "kudos_count",
    "achievement_count",
    "suffer_score",
]


def _format_duration(seconds: int) -> str:
    """Convert seconds to H:MM:SS format."""
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _shape_activity(raw: dict) -> dict:
    """Extract and transform fields for the activity list view."""
    shaped = {}
    for field in _ACTIVITY_LIST_FIELDS:
        shaped[field] = raw.get(field)

    # Convert distance from meters to miles
    if shaped["distance"] is not None:
        shaped["distance"] = round(shaped["distance"] / METERS_PER_MILE, 2)

    # Format moving_time as H:MM:SS
    if shaped["moving_time"] is not None:
        shaped["moving_time"] = _format_duration(shaped["moving_time"])

    # Format elapsed_time as H:MM:SS
    if shaped["elapsed_time"] is not None:
        shaped["elapsed_time"] = _format_duration(shaped["elapsed_time"])

    # Build a compact location string from city/state/country
    loc_parts = [
        shaped.get("location_city"),
        shaped.get("location_state"),
    ]
    loc_parts = [p for p in loc_parts if p]
    shaped["location"] = ", ".join(loc_parts) if loc_parts else None

    return shaped


class CacheManager:
    """Cache-aside manager for Strava API data.

    get_recent_activities is local-first: reads from the vault when
    populated, only hitting the API if the vault is empty.

    sync_activities is incremental-aware: uses the latest activity
    timestamp to fetch only new activities after the first full sync.
    """

    def __init__(self, cache_db, strava_client):
        self.db = cache_db
        self.client = strava_client

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def get_recent_activities(self, count: int = 10) -> list:
        """Return a shaped list of recent activities.

        Local-first: reads from the vault if it has data.
        Falls back to the API if the vault is empty.
        """
        vault_count = await self.db.get_vault_activity_count()

        if vault_count > 0:
            # Local-first: read from vault
            raw_activities = await self.db.get_vault_activities(limit=min(count, 200))
            shaped = [_shape_activity(a) for a in raw_activities]
        else:
            # Vault empty: fall back to API + old cache path
            key = f"activities:list:{count}"
            category = "activities_list"

            cached = await self.db.get_cached(key)
            if cached is not None:
                return cached

            raw = await self.client.get_activities(per_page=min(count, 200))
            shaped = [_shape_activity(a) for a in raw[:count]]

            await self.db.set_cached(key, category, shaped, TTL[category])

        # Resolve gear names
        gear_ids = {a["gear_id"] for a in shaped if a.get("gear_id")}
        gear_map = {}
        for gid in gear_ids:
            name = await self._resolve_gear_name(gid)
            if name:
                gear_map[gid] = name
        for a in shaped:
            gid = a.get("gear_id")
            if gid and gid in gear_map:
                a["gear_name"] = gear_map[gid]

        return shaped

    async def _resolve_gear_name(self, gear_id: str) -> str | None:
        """Look up a gear name by ID, cached for 7 days."""
        key = f"gear:{gear_id}"
        cached = await self.db.get_cached(key)
        if cached is not None:
            return cached.get("name")

        try:
            gear = await self.client.get_gear(gear_id)
            await self.db.set_cached(key, "gear", gear, 604800)  # 7 days
            return gear.get("name")
        except Exception:
            return None

    async def get_activity(self, activity_id: int) -> dict:
        """Return full activity detail, cached for 24 hours."""
        key = f"activity:{activity_id}"
        category = "activity_detail"

        cached = await self.db.get_cached(key)
        if cached is not None:
            return cached

        result = await self.client.get_activity(activity_id)

        await self.db.set_cached(key, category, result, TTL[category])
        return result

    async def get_activity_streams(
        self,
        activity_id: int,
        stream_types: str = "heartrate,distance,altitude",
    ) -> dict:
        """Return activity streams, cached for 7 days."""
        types_list = [t.strip() for t in stream_types.split(",")]
        sorted_types = sorted(types_list)
        sorted_key = ",".join(sorted_types)

        key = f"streams:{activity_id}:{sorted_key}"
        category = "activity_streams"

        cached = await self.db.get_cached(key)
        if cached is not None:
            return cached

        result = await self.client.get_activity_streams(activity_id, types_list)

        await self.db.set_cached(key, category, result, TTL[category])
        return result

    async def get_athlete_profile(self) -> dict:
        """Return the authenticated athlete profile, cached for 24 hours."""
        key = "athlete:profile"
        category = "athlete_profile"

        cached = await self.db.get_cached(key)
        if cached is not None:
            return cached

        result = await self.client.get_athlete()

        await self.db.set_cached(key, category, result, TTL[category])
        return result

    async def get_athlete_stats(self) -> dict:
        """Return athlete stats, cached for 1 day."""
        key = "athlete:stats"
        category = "athlete_stats"

        cached = await self.db.get_cached(key)
        if cached is not None:
            return cached

        profile = await self.get_athlete_profile()
        athlete_id = profile["id"]
        result = await self.client.get_athlete_stats(athlete_id)

        await self.db.set_cached(key, category, result, TTL[category])
        return result

    async def get_cache_stats(self) -> dict:
        """Return cache + vault statistics combined with API rate-limit info."""
        stats = await self.db.get_stats()
        stats["rate_limit"] = self.client.rate_limit_remaining

        # Vault stats
        stats["vault"] = {
            "total_activities": await self.db.get_vault_activity_count(),
            "date_range": await self.db.get_vault_date_range(),
            "sync_log": await self.db.get_sync_log(),
        }

        return stats

    async def sync_activities(self, days_back: int = 0) -> dict:
        """Sync activities into the vault.

        Behavior:
        - days_back=0 (default): Incremental sync. If the vault has data,
          fetches only activities newer than the latest stored activity.
          If the vault is empty, does a full historical sync.
        - days_back>0: Fetches activities from the last N days, regardless
          of what's already in the vault. Useful for backfilling or refreshing
          a specific window.

        Each activity is stored permanently in the vault (activities table).
        Activity detail is also cached with TTL for the get_activity tool.

        Returns a summary with counts, mode, and API usage.
        """
        latest_epoch = await self.db.get_latest_activity_epoch()
        vault_count_before = await self.db.get_vault_activity_count()

        if days_back > 0:
            # Explicit time window
            after = int(time.time()) - (days_back * 86400)
            mode = f"window_{days_back}d"
        elif latest_epoch is not None:
            # Incremental: fetch only newer than latest stored
            after = latest_epoch
            mode = "incremental"
        else:
            # First sync: fetch everything (after=0 means all time)
            after = 0
            mode = "full"

        all_activities = []
        page = 1
        api_calls = 0

        logger.info("Sync starting: mode=%s, after=%d", mode, after)

        while True:
            batch = await self.client.get_activities(
                page=page, per_page=200, after=after
            )
            api_calls += 1

            if not batch:
                break

            all_activities.extend(batch)
            logger.info("Sync page %d: got %d activities", page, len(batch))
            page += 1

        # Store in vault (permanent)
        if all_activities:
            await self.db.upsert_activities_batch(all_activities)

        # Also cache each activity individually (for get_activity detail lookups)
        for activity in all_activities:
            activity_key = f"activity:{activity['id']}"
            await self.db.set_cached(
                activity_key,
                "activity_detail",
                activity,
                TTL["activity_detail"],
            )

        vault_count_after = await self.db.get_vault_activity_count()
        new_activities = vault_count_after - vault_count_before

        # Update sync log
        await self.db.update_sync_log(vault_count_after, mode)

        result = {
            "mode": mode,
            "activities_fetched": len(all_activities),
            "new_activities": new_activities,
            "total_in_vault": vault_count_after,
            "api_calls_used": api_calls,
            "date_range": await self.db.get_vault_date_range(),
        }

        logger.info(
            "Sync complete: mode=%s, fetched=%d, new=%d, total=%d, api_calls=%d",
            mode, len(all_activities), new_activities, vault_count_after, api_calls,
        )

        return result
