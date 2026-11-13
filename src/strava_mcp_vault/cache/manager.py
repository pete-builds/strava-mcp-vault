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
from datetime import UTC, datetime

from strava_mcp_vault.exceptions import RateLimitError

METERS_PER_MILE = 1609.344

#: Strava's list endpoint returns at most 200 activities a call.
LIST_PAGE_SIZE = 200

#: A vault check stops while this many read calls are still left, so the rest
#: of the server (and the athlete's other apps) are not starved by a check.
CHECK_READ_RESERVE = 5

#: Padding on the window sent to Strava. The window is in LOCAL dates, Strava
#: filters on UTC epochs, and no time zone is more than 14 hours from UTC; a day
#: either side covers that, and the padding is filtered back out locally.
_WINDOW_PAD_SECONDS = 86400

logger = logging.getLogger(__name__)


def _to_epoch(iso: str) -> int:
    """Parse an ISO date or date-time (treated as UTC) to an epoch second."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def _epoch_to_iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_window(after: str | None, before: str | None) -> tuple[str | None, str | None]:
    """Validate a check window. Raises ValueError with a message for the user."""
    after = (after or "").strip() or None
    before = (before or "").strip() or None
    for label, value in (("after", after), ("before", before)):
        if value is None:
            continue
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(
                f'{label}="{value}" is not an ISO date. Use a form like "2026-01-01".'
            ) from None
    if after and before and _to_epoch(after) >= _to_epoch(before):
        raise ValueError(f'after ("{after}") must be earlier than before ("{before}").')
    return after, before


def parse_sport_types(sport_types: str | None) -> list[str]:
    """Split a comma-separated sport type list. Empty means every type."""
    return [t.strip() for t in (sport_types or "").split(",") if t.strip()]


def _sport(activity: dict) -> str | None:
    return activity.get("sport_type") or activity.get("type")


TTL = {
    "activities_list": 3600,  # 1 hour
    "activity_detail": 86400,  # 24 hours
    "activity_streams": 604800,  # 7 days
    "athlete_profile": 86400,  # 24 hours
    "athlete_stats": 86400,  # 1 day
    "activity_photos": 604800,  # 7 days; the URLs are signed and rotate
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

    async def get_recent_activities(
        self,
        count: int = 10,
        sport_type: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> list:
        """Return a shaped list of recent activities with optional filters.

        Local-first: reads from the vault if it has data.
        Falls back to the API if the vault is empty.
        """
        vault_count = await self.db.get_vault_activity_count()

        if vault_count > 0:
            raw_activities = await self.db.get_vault_activities(
                limit=min(count, 200),
                sport_type=sport_type,
                after=after,
                before=before,
            )
            shaped = [_shape_activity(a) for a in raw_activities]
        else:
            # Vault empty: fall back to API (no filtering available)
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

    async def query_vault(
        self,
        sport_type: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> dict:
        """Return a summary of vault activities matching the given filters.

        Returns counts by sport_type, total distance/time, and date range.
        """
        # Get count and breakdown
        total = await self.db.get_vault_activity_count(
            sport_type=sport_type,
            after=after,
            before=before,
        )
        breakdown = await self.db.get_vault_sport_type_summary(
            after=after,
            before=before,
        )

        # If a sport_type filter is active, only include that type in breakdown
        if sport_type:
            breakdown = [b for b in breakdown if b["sport_type"] == sport_type]

        # Summed in SQL over every match. This used to sum the first 1,000 rows
        # in Python, so a long history reported the right count beside a total
        # distance that silently stopped growing.
        totals = await self.db.get_vault_totals(
            sport_type=sport_type,
            after=after,
            before=before,
        )

        return {
            "total_activities": total,
            "breakdown_by_type": breakdown,
            "total_distance_meters": totals["distance_meters"],
            "total_moving_time_seconds": totals["moving_time_seconds"],
            "total_elevation_meters": totals["elevation_meters"],
            "filters": {
                "sport_type": sport_type,
                "after": after,
                "before": before,
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

    async def get_activity_photos(self, activity_id: int, size: int = 2048) -> list:
        """Return an activity's photos, cached for 7 days.

        Cached under its own key rather than the activity_detail key, so photos
        never depend on what that entry holds. Sync used to overwrite it with
        stripped-down list summaries (audit finding 2).
        """
        key = f"activity_photos:{activity_id}:{size}"
        cached = await self.db.get_cached(key)
        if cached is not None:
            return cached

        result = await self.client.get_activity_photos(activity_id, size=size)
        await self.db.set_cached(key, "activity_photos", result, TTL["activity_photos"])
        return result

    @staticmethod
    def stream_cache_key(activity_id: int, stream_types: str) -> str:
        """The cache key for one activity's streams.

        The type list is SORTED into the key, so "latlng,altitude,time" and
        "altitude,latlng,time" are one entry rather than two. Anything checking
        whether a stream is already cached must build the key through here: a
        hand-written key in the requested order silently never matches, reports
        every activity as uncached, and spends the whole Strava rate limit
        refetching what the vault already had.
        """
        types_list = [t.strip() for t in stream_types.split(",")]
        return f"streams:{activity_id}:{','.join(sorted(types_list))}"

    async def get_activity_streams(
        self,
        activity_id: int,
        stream_types: str = "heartrate,distance,altitude",
    ) -> dict:
        """Return activity streams, cached for 7 days."""
        types_list = [t.strip() for t in stream_types.split(",")]

        key = self.stream_cache_key(activity_id, stream_types)
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
            "sync_history": await self.db.get_sync_history(limit=10),
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
        A re-sync refreshes Strava's fields and keeps any location set with
        set_activity_location. Full detail is left to get_activity, which
        fetches and caches it on first request.

        Returns a summary with counts, mode, and API usage.
        """
        started_at = time.time()
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

        window_after = _epoch_to_iso(after) if after else None
        try:
            while True:
                batch = await self.client.get_activities(
                    page=page, per_page=LIST_PAGE_SIZE, after=after
                )
                api_calls += 1

                if not batch:
                    break

                all_activities.extend(batch)
                logger.info("Sync page %d: got %d activities", page, len(batch))
                page += 1
        except Exception as e:
            # Nothing was stored, but the attempt is dated, so a later hole
            # can be traced to the night the sync failed.
            await self.db.add_sync_history(
                started_at,
                mode,
                window_after,
                None,
                added=0,
                updated=0,
                completed=False,
                note=f"{type(e).__name__}: {e}"[:300],
            )
            raise

        # Store in vault (permanent). Nothing goes into the activity_detail
        # cache: these are list summaries with no description, gear or laps,
        # and caching them under get_activity's key made "full details" serve
        # the summary for a day after every sync.
        if all_activities:
            await self.db.upsert_activities_batch(all_activities)

        vault_count_after = await self.db.get_vault_activity_count()
        new_activities = vault_count_after - vault_count_before

        # Update sync log, and append to the history that outlives it.
        await self.db.update_sync_log(vault_count_after, mode)
        fetched_ids = {a["id"] for a in all_activities}
        await self.db.add_sync_history(
            started_at,
            mode,
            window_after,
            None,
            added=new_activities,
            updated=max(0, len(fetched_ids) - new_activities),
            completed=True,
        )

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

    # ------------------------------------------------------------------
    # Vault check and repair
    # ------------------------------------------------------------------

    async def _scan_strava(
        self, after: str | None, before: str | None
    ) -> tuple[list[dict], int, str | None]:
        """Page Strava's activity list over a local-date window.

        Returns (activities in the order Strava sent them, calls made, the
        reason it stopped early or None). Never raises a rate limit: it stops
        before the read budget runs out, and a 429 or a local refusal ends the
        scan with whatever it had.
        """
        after_epoch = max(0, _to_epoch(after) - _WINDOW_PAD_SECONDS) if after else 0
        before_epoch = _to_epoch(before) + _WINDOW_PAD_SECONDS if before else None

        fetched: list[dict] = []
        calls = 0
        page = 1
        while True:
            remaining = self.client.read_budget_remaining()
            if remaining is not None and remaining <= CHECK_READ_RESERVE:
                reset = self.client.read_budget_reset_text()
                return (
                    fetched,
                    calls,
                    f"stopped with {remaining} Strava read calls left, keeping "
                    f"{CHECK_READ_RESERVE} in reserve; {reset}",
                )
            try:
                batch = await self.client.get_activities(
                    page=page, per_page=LIST_PAGE_SIZE, after=after_epoch, before=before_epoch
                )
            except RateLimitError as e:
                return fetched, calls, str(e)
            calls += 1
            if not batch:
                return fetched, calls, None
            fetched.extend(batch)
            page += 1

    async def _check(
        self, after: str | None, before: str | None, sport_types: list[str]
    ) -> tuple[dict, list[dict]]:
        """Compare Strava with the vault. Returns (report, Strava activities checked)."""
        fetched, calls, stop_reason = await self._scan_strava(after, before)
        complete = stop_reason is None

        def in_window(local: str | None, sport: str | None) -> bool:
            local = local or ""
            return (
                (not after or local >= after)
                and (not before or local < before)
                and (not sport_types or sport in sport_types)
            )

        # On a stopped scan only the stretch Strava actually sent is compared.
        # Strava pages oldest first when given `after`; the order is read from
        # the data rather than assumed, so a reversed order would make the
        # resume point `before` instead of skipping what was never fetched.
        checked_through = None
        resume = None
        cutoff_low = cutoff_high = None
        if not complete:
            if fetched:
                first, last = fetched[0], fetched[-1]
                ascending = (first.get("start_date") or "") <= (last.get("start_date") or "")
                checked_through = last.get("start_date_local")
                if ascending:
                    cutoff_high = checked_through
                    resume = {"after": checked_through, "before": before}
                else:
                    cutoff_low = checked_through
                    resume = {"after": after, "before": checked_through}
            else:
                resume = {"after": after, "before": before}

        def covered(local: str | None) -> bool:
            local = local or ""
            if cutoff_high is not None and local > cutoff_high:
                return False
            if cutoff_low is not None and local < cutoff_low:
                return False
            return True

        strava = {}
        for a in fetched:
            local = a.get("start_date_local")
            if in_window(local, _sport(a)) and covered(local):
                strava[a["id"]] = a

        vault_rows = [
            r
            for r in await self.db.get_vault_index(after, before, sport_types or None)
            if covered(r["start_date_local"])
        ]
        if not complete and not fetched:
            vault_rows = []  # nothing on Strava was seen, so nothing is compared

        in_vault = await self.db.get_existing_ids(list(strava))
        missing = sorted(
            (a for i, a in strava.items() if i not in in_vault),
            key=lambda a: a.get("start_date_local") or "",
        )
        # "Gone" needs the whole window seen: on a stopped scan an activity
        # not fetched yet would look deleted. Compared against EVERY id
        # fetched, so an activity whose date moved on Strava is not flagged.
        gone = None
        if complete:
            seen_ids = {a["id"] for a in fetched}
            gone = [r for r in vault_rows if r["id"] not in seen_ids]

        months: dict[str, dict] = {}
        for a in strava.values():
            m = months.setdefault((a.get("start_date_local") or "")[:7], _month_row())
            m["strava"] += 1
        for r in vault_rows:
            m = months.setdefault((r["start_date_local"] or "")[:7], _month_row())
            m["vault"] += 1
        for a in missing:
            months[(a.get("start_date_local") or "")[:7]]["missing"] += 1
        for r in gone or []:
            months[(r["start_date_local"] or "")[:7]]["gone"] += 1

        report = {
            "window": {"after": after, "before": before, "sport_types": sport_types},
            "complete": complete,
            "stop_reason": stop_reason,
            "checked_through": checked_through,
            "resume": resume,
            "api_calls": calls,
            "strava": {
                "count": len(strava),
                "distance_meters": sum((a.get("distance") or 0) for a in strava.values()),
            },
            "vault": {
                "count": len(vault_rows),
                "distance_meters": sum(r["distance"] for r in vault_rows),
            },
            "months": [{"month": k, **v} for k, v in sorted(months.items())],
            "missing": [_brief(a) for a in missing],
            "gone": gone,
        }
        return report, list(strava.values())

    async def check_vault(
        self,
        after: str | None = None,
        before: str | None = None,
        sport_types: str | list[str] | None = None,
    ) -> dict:
        """Compare the vault with Strava over a local-date window. Writes nothing.

        Reads Strava's activity list (about one call per 200 activities) and
        reports what is on Strava but missing from the vault, what is in the
        vault but gone from Strava, per-month counts for both sides, and each
        side's count and distance. If the read budget runs low it stops,
        reports how far it got, and says how to resume.

        Callable from Python as well as through the MCP tool, e.g. from a
        nightly script: ``report = await manager.check_vault(after="2026-01-01")``.
        """
        after, before = parse_window(after, before)
        types = sport_types if isinstance(sport_types, list) else parse_sport_types(sport_types)
        report, _ = await self._check(after, before, types)
        return report

    async def repair_vault(
        self,
        after: str | None = None,
        before: str | None = None,
        sport_types: str | list[str] | None = None,
        delete_gone: bool = False,
    ) -> dict:
        """Run a check, then put back what the vault is missing.

        Every Strava activity the check saw in the window is stored through
        the same upsert sync uses, so missing ones are added and present ones
        refreshed with any hand-set location kept. Activities gone from Strava
        are only listed, unless delete_gone is set AND the check covered the
        whole window, in which case they are removed in one transaction.

        A run cut short by the read budget stores what it fetched, which the
        upsert makes safe to repeat, and the report says where to resume.
        """
        started_at = time.time()
        after, before = parse_window(after, before)
        types = sport_types if isinstance(sport_types, list) else parse_sport_types(sport_types)
        report, strava = await self._check(after, before, types)

        ids = [a["id"] for a in strava]
        existed = await self.db.get_existing_ids(ids)
        if strava:
            await self.db.upsert_activities_batch(strava)
        added = [a["id"] for a in strava if a["id"] not in existed]

        removed: list[dict] = []
        delete_note = None
        if delete_gone:
            if not report["complete"]:
                delete_note = (
                    "Nothing was deleted: the check stopped early, so an activity not "
                    "fetched yet could look deleted. Run the full window again."
                )
            elif report["gone"]:
                removed = await self.db.delete_activities_reporting(
                    [r["id"] for r in report["gone"]]
                )

        after_rows = await self.db.get_vault_index(after, before, types or None)
        report["repair"] = {
            "added": added,
            "updated": len(existed),
            "removed": removed,
            "delete_requested": delete_gone,
            "delete_note": delete_note,
            "vault_after": {
                "count": len(after_rows),
                "distance_meters": sum(r["distance"] for r in after_rows),
            },
        }
        await self.db.add_sync_history(
            started_at,
            "repair",
            after,
            before,
            added=len(added),
            updated=len(existed),
            removed=len(removed),
            completed=report["complete"],
            note=report["stop_reason"],
        )
        return report


def _month_row() -> dict:
    return {"strava": 0, "vault": 0, "missing": 0, "gone": 0}


def _brief(activity: dict) -> dict:
    return {
        "id": activity["id"],
        "start_date_local": activity.get("start_date_local"),
        "sport_type": _sport(activity),
        "name": activity.get("name"),
        "distance": activity.get("distance") or 0,
    }
