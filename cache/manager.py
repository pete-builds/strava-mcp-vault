"""Cache-aside orchestration layer.

Sits between MCP tools (server.py) and the Strava API client.
Tools call the manager, which checks SQLite first and falls back
to the API on a cache miss.
"""

import time

METERS_PER_MILE = 1609.344

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

    return shaped


class CacheManager:
    """Cache-aside manager for Strava API data.

    Each public method follows the same pattern: check the cache first,
    call the API on miss, store the result, and return it.
    """

    def __init__(self, cache_db, strava_client):
        self.db = cache_db
        self.client = strava_client

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def get_recent_activities(self, count: int = 10) -> list:
        """Return a shaped list of recent activities, cached for 1 hour."""
        key = f"activities:list:{count}"
        category = "activities_list"

        cached = await self.db.get_cached(key)
        if cached is not None:
            return cached

        raw = await self.client.get_activities(per_page=min(count, 200))
        shaped = [_shape_activity(a) for a in raw[:count]]

        await self.db.set_cached(key, category, shaped, TTL[category])
        return shaped

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
        """Return athlete stats, cached for 1 day.

        Fetches the athlete profile first (itself cached) to get the
        athlete_id required by the stats endpoint.
        """
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
        """Return cache statistics combined with API rate-limit info."""
        stats = await self.db.get_stats()
        stats["rate_limit"] = self.client.rate_limit_remaining
        return stats

    async def sync_activities(self, days_back: int = 30) -> dict:
        """Bulk-sync activities from the last N days into the cache.

        Paginates through the API until no more results are returned.
        Each activity is cached individually. A shaped list is also
        cached for get_recent_activities.

        Returns a summary with counts and API usage.
        """
        after = int(time.time()) - (days_back * 86400)
        all_activities = []
        page = 1
        api_calls = 0

        while True:
            batch = await self.client.get_activities(
                page=page, per_page=200, after=after
            )
            api_calls += 1

            if not batch:
                break

            all_activities.extend(batch)
            page += 1

        # Cache each activity individually
        for activity in all_activities:
            activity_key = f"activity:{activity['id']}"
            await self.db.set_cached(
                activity_key,
                "activity_detail",
                activity,
                TTL["activity_detail"],
            )

        # Also cache a shaped list so get_recent_activities hits the cache
        shaped = [_shape_activity(a) for a in all_activities]
        list_key = f"activities:list:{len(all_activities)}"
        await self.db.set_cached(
            list_key, "activities_list", shaped, TTL["activities_list"]
        )

        return {
            "activities_synced": len(all_activities),
            "api_calls_used": api_calls,
            "days_back": days_back,
        }
