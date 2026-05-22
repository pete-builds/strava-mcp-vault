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

import asyncio
import logging
import time
from datetime import datetime

METERS_PER_MILE = 1609.344

logger = logging.getLogger(__name__)

TTL = {
    "activities_list": 3600,  # 1 hour
    "activity_detail": 86400,  # 24 hours
    "activity_streams": 604800,  # 7 days
    "athlete_profile": 86400,  # 24 hours
    "athlete_stats": 86400,  # 1 day
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
    "has_heartrate",
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
    # Power (only set when activity recorded power data)
    "average_watts",
    "weighted_average_watts",
    "max_watts",
    "kilojoules",
    "device_watts",
]


def _format_duration(seconds: int) -> str:
    """Convert seconds to H:MM:SS format."""
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _iso_to_epoch(value: str | None) -> int | None:
    """Convert an ISO date/datetime string to a Unix epoch int for Strava.

    Strava's ``/athlete/activities`` endpoint accepts ``before`` and
    ``after`` as epoch seconds. We accept the same ISO format we use
    everywhere else (``"2026-01-01"`` or ``"2026-01-01T00:00:00Z"``) and
    return ``None`` for unparseable input — the API call simply omits the
    bound, which matches how the vault path treats missing filters.
    """
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


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

    async def get_recent_activities(
        self,
        count: int = 10,
        offset: int = 0,
        sport_type: str | None = None,
        after: str | None = None,
        before: str | None = None,
        has_power: bool | None = None,
    ) -> list:
        """Return a shaped list of recent activities with optional filters.

        Local-first: reads from the vault if it has data.
        Falls back to the API if the vault is empty — filters are still
        applied on the fallback path (``before``/``after`` are passed to
        Strava natively; ``sport_type`` and ``has_power`` are applied
        client-side after fetch).
        """
        from strava_mcp_vault.sport_types import expand_sport_type

        vault_count = await self.db.get_vault_activity_count()

        if vault_count > 0:
            raw_activities = await self.db.get_vault_activities(
                limit=min(count, 200),
                offset=max(offset, 0),
                sport_type=sport_type,
                after=after,
                before=before,
                has_power=has_power,
            )
            shaped = [_shape_activity(a) for a in raw_activities]
        else:
            # Vault empty: fall back to the Strava API. The list endpoint
            # accepts before/after as epoch seconds; sport_type and
            # has_power have no API equivalent and are applied client-side.
            after_epoch = _iso_to_epoch(after)
            before_epoch = _iso_to_epoch(before)
            has_filters = bool(sport_type or after_epoch or before_epoch or has_power is not None)

            # Cache key includes the full filter signature so unfiltered and
            # filtered calls don't collide. Old "activities:list:N" entries
            # from earlier versions become orphans (harmless; they TTL out).
            key = (
                f"activities:list:{count}:{offset}:{sport_type or ''}:"
                f"{after or ''}:{before or ''}:{has_power}"
            )
            category = "activities_list"

            cached = await self.db.get_cached(key)
            if cached is not None:
                return cached

            # When filters are active, fetch a wider page so client-side
            # filtering can still return up to `count` results. One API call,
            # capped at Strava's per_page max of 200.
            fetch_size = 200 if has_filters else min(count, 200)
            raw = await self.client.get_activities(
                per_page=fetch_size,
                after=after_epoch,
                before=before_epoch,
            )

            # Client-side sport_type filter (supports aliases + comma lists).
            allowed_sports = expand_sport_type(sport_type)
            if allowed_sports is not None:
                allowed = set(allowed_sports)
                raw = [a for a in raw if (a.get("sport_type") or a.get("type")) in allowed]

            # Client-side has_power filter — Strava embeds power fields in
            # the list response when present, so this is just a presence
            # check on the JSON blob.
            if has_power is True:
                raw = [a for a in raw if a.get("average_watts") is not None]
            elif has_power is False:
                raw = [a for a in raw if a.get("average_watts") is None]

            # Apply offset + count after filtering.
            sliced = raw[max(offset, 0) : max(offset, 0) + count]
            shaped = [_shape_activity(a) for a in sliced]

            await self.db.set_cached(key, category, shaped, TTL[category])

        # Resolve gear names in parallel; cache hits return instantly, only
        # cache-misses fan out to the API.
        gear_ids = list({a["gear_id"] for a in shaped if a.get("gear_id")})
        if gear_ids:
            names = await asyncio.gather(*(self._resolve_gear_name(gid) for gid in gear_ids))
            gear_map = {gid: name for gid, name in zip(gear_ids, names) if name}
            for a in shaped:
                gid = a.get("gear_id")
                if gid in gear_map:
                    a["gear_name"] = gear_map[gid]

        return shaped

    async def query_vault(
        self,
        sport_type: str | None = None,
        after: str | None = None,
        before: str | None = None,
        has_power: bool | None = None,
    ) -> dict:
        """Return a summary of vault activities matching the given filters.

        Returns counts by sport_type, totals for distance/time/elevation, and
        power aggregates (work + weighted-avg power + count of power-meter
        rides) when any activities have power data.
        """
        from strava_mcp_vault.sport_types import expand_sport_type

        expanded_sport = expand_sport_type(sport_type)

        # Get count and breakdown
        total = await self.db.get_vault_activity_count(
            sport_type=sport_type,
            after=after,
            before=before,
            has_power=has_power,
        )
        breakdown = await self.db.get_vault_sport_type_summary(
            after=after,
            before=before,
        )

        # If a sport_type filter is active, only include matching types in breakdown
        if expanded_sport is not None:
            allowed = set(expanded_sport)
            breakdown = [b for b in breakdown if b["sport_type"] in allowed]

        # Pull matching activities for aggregate stats
        activities = await self.db.get_vault_activities(
            limit=1000,
            sport_type=sport_type,
            after=after,
            before=before,
            has_power=has_power,
        )

        total_distance_m = 0
        total_moving_time_s = 0
        total_elevation_m = 0
        total_kilojoules = 0.0
        weighted_power_samples: list[float] = []
        power_rides_count = 0
        for a in activities:
            total_distance_m += a.get("distance") or 0
            total_moving_time_s += a.get("moving_time") or 0
            total_elevation_m += a.get("total_elevation_gain") or 0
            kj = a.get("kilojoules")
            if kj is not None:
                total_kilojoules += kj
            wt = a.get("weighted_average_watts")
            if wt is not None:
                weighted_power_samples.append(wt)
            if a.get("average_watts") is not None:
                power_rides_count += 1

        avg_weighted_power = (
            sum(weighted_power_samples) / len(weighted_power_samples)
            if weighted_power_samples
            else None
        )

        return {
            "total_activities": total,
            "breakdown_by_type": breakdown,
            "total_distance_meters": total_distance_m,
            "total_moving_time_seconds": total_moving_time_s,
            "total_elevation_meters": total_elevation_m,
            "total_kilojoules": total_kilojoules if total_kilojoules > 0 else None,
            "avg_weighted_power": avg_weighted_power,
            "power_rides_count": power_rides_count,
            "filters": {
                "sport_type": sport_type,
                "after": after,
                "before": before,
                "has_power": has_power,
            },
        }

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
            logger.warning("Gear lookup failed for %s", gear_id, exc_info=True)
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

    async def sync_activities(self, days_back: int = 0, ctx=None) -> dict:
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
            batch = await self.client.get_activities(page=page, per_page=200, after=after)
            api_calls += 1

            if not batch:
                break

            all_activities.extend(batch)
            logger.info("Sync page %d: got %d activities", page, len(batch))
            if ctx is not None:
                try:
                    await ctx.report_progress(
                        progress=page,
                        message=f"page {page}: fetched {len(all_activities)} activities so far",
                    )
                except Exception:
                    pass  # progress reporting is best-effort
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
            mode,
            len(all_activities),
            new_activities,
            vault_count_after,
            api_calls,
        )

        return result
