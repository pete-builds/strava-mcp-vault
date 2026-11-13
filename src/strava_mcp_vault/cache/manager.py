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
import math
import time
from datetime import UTC, datetime, timedelta

from strava_mcp_vault.exceptions import (
    RateLimitError,
    ReadBudgetExhausted,
    StravaAPIError,
    VaultError,
)

METERS_PER_MILE = 1609.344

#: Without max_delete, one repair deletes at most this many activities, or
#: DELETE_CAP_SHARE of the window's vault activities if that is larger.
DELETE_CAP_MIN = 10
DELETE_CAP_SHARE = 0.05

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


def _normalise_date(label: str, value: str | None) -> str | None:
    """Turn any ISO date into YYYY-MM-DD, or YYYY-MM-DDTHH:MM:SS if a time was given.

    The window is compared as text against start_date_local, so "20260101"
    left as typed would sort after every real date and match nothing.
    """
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f'{label}="{value}" is not an ISO date. Use a form like "2026-01-01".'
        ) from None
    has_time = any(c in value for c in "Tt ")
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if has_time else dt.strftime("%Y-%m-%d")


def _shift_day(local: str, days: int) -> str:
    """A local timestamp moved by whole days, as YYYY-MM-DDTHH:MM:SS."""
    dt = datetime.fromisoformat(local).replace(tzinfo=None) + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def parse_window(after: str | None, before: str | None) -> tuple[str | None, str | None]:
    """Validate and normalise a check window. Raises ValueError for the user."""
    after = _normalise_date("after", after)
    before = _normalise_date("before", before)
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
            await self._record_failure(started_at, mode, window_after, None, e)
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

    async def _record_failure(
        self,
        started_at: float,
        mode: str,
        window_after: str | None,
        window_before: str | None,
        error: Exception,
    ) -> None:
        """Date a failed run in the history. Never raises: the caller is
        already handling the real error, and a failed write must not hide it."""
        try:
            await self.db.add_sync_history(
                started_at,
                mode,
                window_after,
                window_before,
                added=0,
                updated=0,
                completed=False,
                note=f"{type(error).__name__}: {error}"[:300],
            )
        except Exception:
            logger.exception("Could not record the failed %s in sync_history", mode)

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
        scan with whatever it had. Any other error (a 500, a network failure)
        is raised: an error page is never mistaken for the end of the list.
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
                if not isinstance(e, ReadBudgetExhausted):
                    calls += 1  # a 429 reached Strava; a local refusal did not
                return fetched, calls, str(e)
            calls += 1
            if not batch:
                return fetched, calls, None
            fetched.extend(batch)
            page += 1

    async def _check(
        self, after: str | None, before: str | None, sport_types: list[str]
    ) -> tuple[dict, list[dict], list[dict]]:
        """Compare Strava with the vault.

        Returns (report, activities a repair should store, everything fetched).
        """
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
        # the data rather than assumed. The resume point overlaps a day, since
        # a rider who crossed time zones can have a later ride with an earlier
        # local time; the upsert makes re-reading that day harmless.
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
                    resume = {"after": _shift_day(checked_through, -1), "before": before}
                else:
                    cutoff_low = checked_through
                    resume = {"after": after, "before": _shift_day(checked_through, 1)}
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
        # A vault row Strava did send, but which now falls outside the window
        # or the sport filter: re-dated or re-typed on Strava. It is not gone.
        fetched_by_id = {a["id"]: a for a in fetched}
        changed = [
            {
                **_brief(fetched_by_id[r["id"]]),
                "was_start_date_local": r["start_date_local"],
                "was_sport_type": r["sport_type"],
            }
            for r in vault_rows
            if r["id"] in fetched_by_id and r["id"] not in strava
        ]
        # "Gone" needs the whole window seen: on a stopped scan an activity
        # not fetched yet would look deleted. Compared against EVERY id
        # fetched, so an activity re-dated or re-typed on Strava is not flagged.
        gone = None
        if complete:
            gone = [r for r in vault_rows if r["id"] not in fetched_by_id]

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

        strava_side = {
            "count": len(strava),
            "distance_meters": sum((a.get("distance") or 0) for a in strava.values()),
        }
        vault_side = {
            "count": len(vault_rows),
            "distance_meters": sum(r["distance"] for r in vault_rows),
        }
        matches = (
            complete
            and not missing
            and not gone
            and not changed
            and strava_side["count"] == vault_side["count"]
            and abs(strava_side["distance_meters"] - vault_side["distance_meters"]) <= 1.0
        )

        report = {
            "window": {"after": after, "before": before, "sport_types": sport_types},
            "complete": complete,
            "matches": matches,
            "stop_reason": stop_reason,
            "checked_through": checked_through,
            "resume": resume,
            "api_calls": calls,
            "athlete_warning": await self._athlete_mismatch(fetched, may_ask=False),
            "strava": strava_side,
            "vault": vault_side,
            "months": [{"month": k, **v} for k, v in sorted(months.items())],
            "missing": [_brief(a) for a in missing],
            "changed": changed,
            "gone": gone,
        }
        # A repair refreshes every vault row Strava sent, not only the ones
        # still inside the filter, so a re-typed or re-dated ride is corrected.
        vault_ids = {r["id"] for r in vault_rows}
        to_store = list(strava.values()) + [
            a for i, a in fetched_by_id.items() if i in vault_ids and i not in strava
        ]
        return report, to_store, fetched

    async def _athlete_mismatch(self, fetched: list[dict], *, may_ask: bool) -> str | None:
        """Say why these activities may belong to someone else, or None.

        The vault's athlete is the one on its stored rows, or, for a vault
        whose rows do not carry one, the athlete recorded on first use. When
        neither side names an athlete and may_ask is set, this asks Strava who
        the token belongs to (one read call) and records it.
        """
        if not fetched:
            return None
        expected = await self.db.get_vault_athlete_ids()
        if len(expected) > 1:
            return (
                "the vault already holds activities from more than one Strava athlete "
                f"({', '.join(str(i) for i in sorted(expected))})"
            )
        if not expected:
            stored = await self.db.get_meta("athlete_id")
            if stored:
                expected = {int(stored)}
        seen = {i for i in (_athlete_id(a) for a in fetched) if i is not None}
        if not seen or not expected:
            if not may_ask:
                return None
            try:
                me = _int_or_none((await self.client.get_athlete()).get("id"))
            except VaultError as e:
                return f"Strava could not confirm which athlete the token belongs to ({e})"
            if me is None:
                return "Strava did not say which athlete the token belongs to"
            seen.add(me)
            if not expected:
                await self.db.set_meta("athlete_id", str(me))
                expected = {me}
        strangers = seen - expected
        if strangers:
            return (
                f"Strava answered for athlete {', '.join(str(i) for i in sorted(strangers))}, "
                f"but this vault belongs to athlete {', '.join(str(i) for i in sorted(expected))}"
            )
        return None

    async def check_vault(
        self,
        after: str | None = None,
        before: str | None = None,
        sport_types: str | list[str] | None = None,
    ) -> dict:
        """Compare the vault with Strava over a local-date window. Writes nothing.

        Reads Strava's activity list (about one call per 200 activities) and
        reports what is on Strava but missing from the vault, what changed on
        Strava, what is in the vault but not in Strava's list, per-month counts
        for both sides, and each side's count and distance. If the read budget
        runs low it stops, reports how far it got, and says how to resume.

        Callable from Python as well as through the MCP tool, e.g. from a
        nightly script: ``report = await manager.check_vault(after="2026-01-01")``.
        """
        after, before = parse_window(after, before)
        types = sport_types if isinstance(sport_types, list) else parse_sport_types(sport_types)
        report, _, _ = await self._check(after, before, types)
        return report

    async def repair_vault(
        self,
        after: str | None = None,
        before: str | None = None,
        sport_types: str | list[str] | None = None,
        delete_gone: bool = False,
        max_delete: int | None = None,
    ) -> dict:
        """Run a check, then put back what the vault is missing.

        Every Strava activity the check saw in the window, and every vault row
        in the window Strava sent, is stored through the same upsert sync uses:
        missing ones are added, the rest refreshed, hand-set locations kept.
        Nothing is stored or deleted if Strava answers for a different athlete.

        With delete_gone, a vault activity missing from Strava's list is
        deleted only if the whole window was read, Strava's side of it is not
        empty, the count is within the cap (max_delete, else the larger of 10
        and 5 percent of the window), it was not stored as private, and Strava
        answers 404 for that id. Anything else keeps the row and says why.

        A run cut short by the read budget stores what it fetched, which the
        upsert makes safe to repeat, and the report says where to resume.
        """
        after, before = parse_window(after, before)
        types = sport_types if isinstance(sport_types, list) else parse_sport_types(sport_types)
        if max_delete is not None and max_delete < 0:
            raise ValueError("max_delete must be 0 or more.")
        started_at = time.time()
        try:
            return await self._repair(started_at, after, before, types, delete_gone, max_delete)
        except Exception as e:
            await self._record_failure(started_at, "repair", after, before, e)
            raise

    async def _repair(
        self,
        started_at: float,
        after: str | None,
        before: str | None,
        types: list[str],
        delete_gone: bool,
        max_delete: int | None,
    ) -> dict:
        report, to_store, fetched = await self._check(after, before, types)
        repair = {
            "added": [],
            "updated": 0,
            "removed": [],
            "kept": [],
            "would_delete": [],
            "refused": None,
            "delete_requested": delete_gone,
            "delete_note": None,
        }
        report["repair"] = repair

        refused = await self._athlete_mismatch(fetched, may_ask=True)
        if refused:
            repair["refused"] = refused
        else:
            existed = await self.db.get_existing_ids([a["id"] for a in to_store])
            if to_store:
                await self.db.upsert_activities_batch(to_store)
            repair["added"] = [a["id"] for a in to_store if a["id"] not in existed]
            repair["updated"] = len(existed)
            if delete_gone:
                await self._delete_gone(report, max_delete)

        after_rows = await self.db.get_vault_index(after, before, types or None)
        repair["vault_after"] = {
            "count": len(after_rows),
            "distance_meters": sum(r["distance"] for r in after_rows),
        }
        await self.db.add_sync_history(
            started_at,
            "repair",
            after,
            before,
            added=len(repair["added"]),
            updated=repair["updated"],
            removed=len(repair["removed"]),
            completed=report["complete"] and not refused,
            note=refused or report["stop_reason"] or repair["delete_note"],
        )
        return report

    async def _delete_gone(self, report: dict, max_delete: int | None) -> None:
        """Delete the vault rows Strava confirms, one by one, no longer exist."""
        repair = report["repair"]
        gone = report["gone"]
        if not report["complete"]:
            repair["delete_note"] = (
                "Nothing was deleted: the check stopped early, so an activity not "
                "fetched yet could look deleted."
            )
            return
        if not gone:
            return
        ids = [r["id"] for r in gone]
        if report["strava"]["count"] == 0:
            repair["would_delete"] = ids
            repair["delete_note"] = (
                "Nothing was deleted: Strava returned no activities for this window, "
                "which is more likely an incomplete answer than a deleted history."
            )
            return
        cap = (
            max_delete
            if max_delete is not None
            else max(DELETE_CAP_MIN, math.ceil(DELETE_CAP_SHARE * report["vault"]["count"]))
        )
        if len(ids) > cap:
            repair["would_delete"] = ids
            repair["delete_note"] = (
                f"Nothing was deleted: {len(ids)} activities would go, more than the limit "
                f"of {cap}. The max_delete argument raises the limit for one run."
            )
            return

        confirmed: list[int] = []
        stopped = None
        for row in gone:
            if stopped:
                repair["kept"].append({"id": row["id"], "reason": "not confirmed: " + stopped})
                continue
            if row["private"]:
                repair["kept"].append(
                    {
                        "id": row["id"],
                        "reason": "stored as private, which a token without "
                        "activity:read_all cannot see, so its absence proves nothing",
                    }
                )
                continue
            remaining = self.client.read_budget_remaining()
            if remaining is not None and remaining <= CHECK_READ_RESERVE:
                stopped = (
                    f"the Strava read budget is down to {remaining}; "
                    f"{self.client.read_budget_reset_text()}"
                )
                repair["kept"].append({"id": row["id"], "reason": "not confirmed: " + stopped})
                continue
            try:
                await self.client.get_activity(row["id"])
            except StravaAPIError as e:
                report["api_calls"] += 1
                if e.status_code == 404:
                    confirmed.append(row["id"])
                else:
                    repair["kept"].append(
                        {"id": row["id"], "reason": f"Strava answered {e.status_code} for it"}
                    )
                continue
            except RateLimitError as e:
                if not isinstance(e, ReadBudgetExhausted):
                    report["api_calls"] += 1
                stopped = f"the Strava read limit was reached ({e})"
                repair["kept"].append({"id": row["id"], "reason": "not confirmed: " + stopped})
                continue
            except Exception as e:  # network trouble: keep the row
                repair["kept"].append(
                    {"id": row["id"], "reason": f"Strava could not be reached ({type(e).__name__})"}
                )
                continue
            report["api_calls"] += 1
            repair["kept"].append(
                {
                    "id": row["id"],
                    "reason": "still on Strava: it answered for this id, so the list "
                    "probably changed while it was being read",
                }
            )

        if stopped:
            repair["delete_note"] = (
                f"Stopped confirming because {stopped}. The rest were kept; nothing "
                "further was deleted."
            )
        if confirmed:
            repair["removed"] = await self.db.delete_activities_reporting(confirmed)


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _athlete_id(activity: dict) -> int | None:
    athlete = activity.get("athlete")
    return _int_or_none(athlete.get("id")) if isinstance(athlete, dict) else None


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
