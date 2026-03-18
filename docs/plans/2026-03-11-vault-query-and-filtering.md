# Vault Query, Filtering & Compact Output Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add server-side filtering (sport_type, date range) to `get_recent_activities`, a new `query_vault` summary tool, a compact output mode, and emojis across all tool outputs.

**Architecture:** Extend the existing three-layer pattern (server.py -> manager -> db). New filtering params flow from MCP tool args through the manager into SQLite WHERE clauses. A new `query_vault` tool provides counts/summaries without returning full activity lists. A `compact` flag on `get_recent_activities` switches to a one-line-per-activity table format. Emojis are added to section headers and stat labels across all formatters.

**Tech Stack:** Python 3.13, FastMCP, aiosqlite, existing formatter pattern

---

## Task 1: Extend DB layer with date-range filtering

**Files:**
- Modify: `cache/db.py:208-221` (`get_vault_activities`)
- Modify: `cache/db.py:223-227` (`get_vault_activity_count`)

**Step 1: Add `after` and `before` params to `get_vault_activities`**

Edit `cache/db.py` method `get_vault_activities` to accept optional `after: str | None = None` and `before: str | None = None` parameters (ISO date strings like "2026-01-01"). Build the WHERE clause dynamically:

```python
async def get_vault_activities(
    self,
    limit: int = 10,
    offset: int = 0,
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Query activities from the vault with optional filters.

    Args:
        limit: Max activities to return.
        offset: Skip this many results.
        sport_type: Filter by Strava sport_type (e.g. "Ride", "Run").
        after: Only activities on or after this ISO date (e.g. "2026-01-01").
        before: Only activities before this ISO date (e.g. "2026-04-01").
    """
    conditions = []
    params = []

    if sport_type:
        conditions.append("sport_type = ?")
        params.append(sport_type)
    if after:
        conditions.append("start_date_local >= ?")
        params.append(after)
    if before:
        conditions.append("start_date_local < ?")
        params.append(before)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"SELECT data FROM activities {where} ORDER BY start_date DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    cursor = await self._db.execute(query, params)
    rows = await cursor.fetchall()
    return [json.loads(row[0]) for row in rows]
```

**Step 2: Add matching filters to `get_vault_activity_count`**

```python
async def get_vault_activity_count(
    self,
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> int:
    """Return count of activities in the vault, with optional filters."""
    conditions = []
    params = []

    if sport_type:
        conditions.append("sport_type = ?")
        params.append(sport_type)
    if after:
        conditions.append("start_date_local >= ?")
        params.append(after)
    if before:
        conditions.append("start_date_local < ?")
        params.append(before)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cursor = await self._db.execute(f"SELECT COUNT(*) FROM activities {where}", params)
    row = await cursor.fetchone()
    return row[0]
```

**Step 3: Add a `get_vault_sport_type_summary` method for aggregation**

Add this new method to `CacheDB`:

```python
async def get_vault_sport_type_summary(
    self,
    after: str | None = None,
    before: str | None = None,
) -> list[dict]:
    """Return activity counts grouped by sport_type, with optional date filters."""
    conditions = []
    params = []

    if after:
        conditions.append("start_date_local >= ?")
        params.append(after)
    if before:
        conditions.append("start_date_local < ?")
        params.append(before)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cursor = await self._db.execute(
        f"SELECT sport_type, COUNT(*) as cnt FROM activities {where} GROUP BY sport_type ORDER BY cnt DESC",
        params,
    )
    rows = await cursor.fetchall()
    return [{"sport_type": row[0], "count": row[1]} for row in rows]
```

**Step 4: Verify no syntax errors**

Run: `cd /mnt/c/Users/pster/strava-mcp-vault && python -c "import cache.db; print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git add cache/db.py
git commit -m "feat: add date-range and sport_type filtering to vault queries"
```

---

## Task 2: Extend CacheManager with filtering pass-through

**Files:**
- Modify: `cache/manager.py:113-151` (`get_recent_activities`)

**Step 1: Add filter params to `get_recent_activities`**

Update the method signature and pass filters through to the DB:

```python
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
```

**Step 2: Add a `query_vault` method**

Add this new method to `CacheManager`:

```python
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
        sport_type=sport_type, after=after, before=before,
    )
    breakdown = await self.db.get_vault_sport_type_summary(
        after=after, before=before,
    )

    # If a sport_type filter is active, only include that type in breakdown
    if sport_type:
        breakdown = [b for b in breakdown if b["sport_type"] == sport_type]

    # Pull matching activities for aggregate stats
    activities = await self.db.get_vault_activities(
        limit=1000, sport_type=sport_type, after=after, before=before,
    )

    total_distance_m = 0
    total_moving_time_s = 0
    total_elevation_m = 0
    for a in activities:
        total_distance_m += a.get("distance") or 0
        total_moving_time_s += a.get("moving_time") or 0
        total_elevation_m += a.get("total_elevation_gain") or 0

    return {
        "total_activities": total,
        "breakdown_by_type": breakdown,
        "total_distance_meters": total_distance_m,
        "total_moving_time_seconds": total_moving_time_s,
        "total_elevation_meters": total_elevation_m,
        "filters": {
            "sport_type": sport_type,
            "after": after,
            "before": before,
        },
    }
```

**Step 3: Verify no syntax errors**

Run: `cd /mnt/c/Users/pster/strava-mcp-vault && python -c "import cache.manager; print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add cache/manager.py
git commit -m "feat: pass filters through manager, add query_vault aggregation"
```

---

## Task 3: Add compact formatter and `format_vault_query`

**Files:**
- Modify: `formatters.py`

**Step 1: Add `format_recent_activities_compact` function**

Add this after the existing `format_recent_activities` function (after line 331):

```python
def format_recent_activities_compact(activities: list) -> str:
    """Format activities as a compact one-line-per-activity table."""
    if not activities:
        return "📭 No activities found."

    lines = [f"## 📋 Activities ({len(activities)})\n"]
    lines.append("| # | Date | Type | Name | Distance | Time | Elevation | HR |")
    lines.append("|---|------|------|------|----------|------|-----------|----|")

    for i, a in enumerate(activities, 1):
        sport_type = a.get("sport_type") or a.get("type") or "?"
        icon = _sport_icon(sport_type)
        name = a.get("name", "Untitled")
        # Truncate long names
        if len(name) > 25:
            name = name[:22] + "..."

        date_str = a.get("start_date_local", "")
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            short_date = dt.strftime("%b %-d")
        except (ValueError, TypeError, AttributeError):
            short_date = "?"

        dist = a.get("distance")
        dist_str = f"{dist:.1f}mi" if dist is not None else ""

        time_val = a.get("moving_time", "")

        elev = a.get("total_elevation_gain")
        elev_str = _format_elevation(elev) if elev else ""

        avg_hr = a.get("average_heartrate")
        hr_str = f"{int(avg_hr)}" if avg_hr else ""

        lines.append(f"| {i} | {short_date} | {icon} | {name} | {dist_str} | {time_val} | {elev_str} | {hr_str} |")

    return "\n".join(lines)
```

**Step 2: Add `format_vault_query` function**

Add this new formatter:

```python
def format_vault_query(result: dict) -> str:
    """Format vault query summary with counts and totals."""
    total = result.get("total_activities", 0)
    breakdown = result.get("breakdown_by_type", [])
    dist_m = result.get("total_distance_meters", 0)
    time_s = result.get("total_moving_time_seconds", 0)
    elev_m = result.get("total_elevation_meters", 0)
    filters = result.get("filters", {})

    # Build filter description
    filter_parts = []
    if filters.get("sport_type"):
        filter_parts.append(f"type={filters['sport_type']}")
    if filters.get("after"):
        filter_parts.append(f"after {filters['after']}")
    if filters.get("before"):
        filter_parts.append(f"before {filters['before']}")
    filter_desc = ", ".join(filter_parts) if filter_parts else "all activities"

    lines = [f"## 🔍 Vault Query Results\n"]
    lines.append(f"**Filter:** {filter_desc}")
    lines.append(f"**Total Activities:** {total}\n")

    if total == 0:
        lines.append("📭 No activities match these filters.")
        return "\n".join(lines)

    # Totals
    miles = dist_m / METERS_PER_MILE
    hours = time_s / 3600

    lines.append("### 📊 Totals\n")
    lines.append(f"- 📏 **Distance:** {miles:.1f} mi")
    lines.append(f"- ⏱️ **Moving Time:** {hours:.1f} hours")
    lines.append(f"- ⛰️ **Elevation:** {_format_elevation(elev_m)}")

    # Breakdown by type
    if breakdown:
        lines.append("\n### 🏷️ By Activity Type\n")
        lines.append("| Type | Count | Icon |")
        lines.append("|------|-------|------|")
        for entry in breakdown:
            st = entry["sport_type"] or "Unknown"
            icon = _sport_icon(st)
            lines.append(f"| {st} | {entry['count']} | {icon} |")

    return "\n".join(lines)
```

**Step 3: Verify no syntax errors**

Run: `cd /mnt/c/Users/pster/strava-mcp-vault && python -c "import formatters; print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add formatters.py
git commit -m "feat: add compact activity table and vault query formatters"
```

---

## Task 4: Add emojis to existing formatters

**Files:**
- Modify: `formatters.py`

**Step 1: Add emojis to `format_recent_activities`**

Change line 272 header from:
```python
lines = [f"## Recent Activities ({len(activities)})\n"]
```
to:
```python
lines = [f"## 🏃 Recent Activities ({len(activities)})\n"]
```

Add emojis to stat labels throughout the activity card:
- `**Distance:**` -> `**📏 Distance:**`
- `**Speed:**` -> `**🚀 Speed:**`
- `**Time:**` -> `**⏱️ Time:**`
- `**Elevation:**` -> `**⛰️ Elevation:**`
- `**Pace:**` -> `**👟 Pace:**`
- `**Time on Mountain:**` -> `**⛷️ Time on Mountain:**`
- `**Avg HR:**` -> `**❤️ Avg HR:**`
- `**Max HR:**` -> `**💓 Max HR:**`
- `**Calories:**` -> `**🔥 Calories:**`
- `**Effort:**` -> `**💪 Effort:**`
- `**Kudos:**` -> `**👍 Kudos:**`
- `**PRs:**` -> `**🏆 PRs:**`
- `**Gear:**` -> `**⚙️ Gear:**`

**Step 2: Add emojis to `format_activity_detail`**

- `### Performance` -> `### ⚡ Performance`
- `### Runs Breakdown` -> `### 🎿 Runs Breakdown`
- `### Heart Rate` -> `### ❤️ Heart Rate`
- Same stat label emojis as list view (Distance, Speed, Pace, etc.)
- `**Gear:**` -> `**⚙️ Gear:**`
- `**Kudos:**` -> `**👍 Kudos:**`
- `**Comments:**` -> `**💬 Comments:**`

**Step 3: Add emojis to `format_athlete_stats`**

- `## Athlete Statistics` -> `## 📈 Athlete Statistics`
- Section headers: add sport icons
  - "Recent Rides" -> "🚴 Recent Rides"
  - "Year-to-Date Rides" -> "🚴 Year-to-Date Rides"
  - "All-Time Rides" -> "🚴 All-Time Rides"
  - "Recent Runs" -> "🏃 Recent Runs" (and YTD/All-Time)
  - "Recent Swims" -> "🏊 Recent Swims" (and YTD/All-Time)
- `**Activities:**` -> `**🔢 Activities:**`
- `**Distance:**` -> `**📏 Distance:**`
- `**Time:**` -> `**⏱️ Time:**`
- `**Elevation:**` -> `**⛰️ Elevation:**`
- `**Longest Ride:**` -> `**🏆 Longest Ride:**`
- `**Biggest Climb:**` -> `**⛰️ Biggest Climb:**`

**Step 4: Add emojis to `format_cache_stats`**

- `## Vault & Cache Statistics` -> `## 📦 Vault & Cache Statistics`
- `### Vault (Permanent Storage)` -> `### 🏛️ Vault (Permanent Storage)`
- `### Cache (TTL-based)` -> `### ⏳ Cache (TTL-based)`
- `### Hit/Miss by Category` -> `### 🎯 Hit/Miss by Category`
- `### API Rate Limits` -> `### 🚦 API Rate Limits`

**Step 5: Add emojis to `format_sync_result`**

- `## Sync Complete` -> `## ✅ Sync Complete`
- `**Mode:**` -> `**🔄 Mode:**`
- `**Activities fetched:**` -> `**📥 Activities fetched:**`
- `**New activities added:**` -> `**🆕 New activities added:**`
- `**Total in vault:**` -> `**🏛️ Total in vault:**`
- `**API calls used:**` -> `**📡 API calls used:**`
- `**Vault date range:**` -> `**📅 Vault date range:**`

**Step 6: Verify no syntax errors**

Run: `cd /mnt/c/Users/pster/strava-mcp-vault && python -c "import formatters; print('OK')"`
Expected: `OK`

**Step 7: Commit**

```bash
git add formatters.py
git commit -m "feat: add emojis to all formatter outputs"
```

---

## Task 5: Wire up new tools and params in server.py

**Files:**
- Modify: `server.py`

**Step 1: Update imports**

Add the new formatters to the import block:

```python
from formatters import (
    format_recent_activities,
    format_recent_activities_compact,
    format_activity_detail,
    format_activity_streams,
    format_athlete_profile,
    format_athlete_stats,
    format_cache_stats,
    format_sync_result,
    format_vault_query,
)
```

**Step 2: Update `get_recent_activities` tool**

Replace the existing tool with filtering and compact support:

```python
@mcp.tool()
async def get_recent_activities(
    count: int = 10,
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
    compact: bool = False,
) -> str:
    """List recent Strava activities with distance, time, and stats.

    Args:
        count: Number of activities to return (default 10, max 200).
        sport_type: Filter by activity type (e.g. "Ride", "Run", "GravelRide", "Snowboard").
        after: Only activities on or after this date (ISO format, e.g. "2026-01-01").
        before: Only activities before this date (ISO format, e.g. "2026-04-01").
        compact: If true, return a compact one-line-per-activity table instead of full cards.
    """
    try:
        results = await manager.get_recent_activities(
            count, sport_type=sport_type, after=after, before=before,
        )
        if compact:
            return format_recent_activities_compact(results)
        return format_recent_activities(results)
    except RateLimitError as e:
        return str(e)
```

**Step 3: Add the `query_vault` tool**

Add this new tool after `get_recent_activities`:

```python
@mcp.tool()
async def query_vault(
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> str:
    """Query the activity vault for counts and totals with optional filters.

    Returns a summary with total count, distance, time, elevation,
    and breakdown by activity type. Much lighter than fetching full
    activity lists. Great for questions like "how many rides this year?"

    Args:
        sport_type: Filter by activity type (e.g. "Ride", "Run", "GravelRide").
        after: Only activities on or after this date (ISO format, e.g. "2026-01-01").
        before: Only activities before this date (ISO format, e.g. "2026-04-01").
    """
    try:
        result = await manager.query_vault(
            sport_type=sport_type, after=after, before=before,
        )
        return format_vault_query(result)
    except RateLimitError as e:
        return str(e)
```

**Step 4: Verify no syntax errors**

Run: `cd /mnt/c/Users/pster/strava-mcp-vault && python -c "import server; print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git add server.py
git commit -m "feat: expose filtering params and new query_vault tool"
```

---

## Task 6: Integration test on nix1

**Step 1: Build and deploy**

```bash
cd /mnt/c/Users/pster/strava-mcp-vault
# Push changes to GitHub (nix1 pulls from there)
git push origin main
```

Then on nix1:
```bash
cd /home/pete/docker/strava-mcp-vault
git pull
docker compose up -d --build
docker logs -f strava-mcp-vault --tail 20
```

Wait for `strava-mcp-vault initialized` log line.

**Step 2: Verify the new tools are registered**

From Claude Code, call `get_cache_stats` to confirm the server is up. Then test:

1. `query_vault(after="2026-01-01")` - should return 2026 summary with breakdown
2. `get_recent_activities(count=5, sport_type="Ride", compact=True)` - should return a compact table of rides only
3. `get_recent_activities(count=50, after="2026-01-01", compact=True)` - compact table of all 2026 activities

**Step 3: Verify emoji output**

Call `get_recent_activities(count=3)` and confirm emojis appear in section headers and stat labels.

**Step 4: Final commit (if any fixups needed)**

```bash
git add -A
git commit -m "fix: integration test fixups"
```

---

## Notes

### Sport types in Strava

The `sport_type` field uses Strava's exact casing. Common values:
- Rides: `Ride`, `GravelRide`, `MountainBikeRide`, `VirtualRide`, `EBikeRide`
- Runs: `Run`, `TrailRun`, `VirtualRun`
- Other: `Walk`, `Hike`, `Swim`, `Snowboard`, `AlpineSki`, `WeightTraining`, `Yoga`

### Query vault vs get_recent_activities

- `query_vault`: Returns counts and aggregate stats. Lightweight, won't overflow context. Use for "how many" and "how much" questions.
- `get_recent_activities(compact=True)`: Returns a table with one line per activity. Good for browsing/scanning.
- `get_recent_activities(compact=False)`: Full rich cards. Best for small result sets (5-10 activities).

### Filtering combines with sport_type groups

The `sport_type` filter uses exact match on Strava's type. If Claude wants "all rides" it should issue multiple queries or use `query_vault` which shows the breakdown. A future enhancement could accept comma-separated types or a category like "rides" that maps to the `_RIDE_TYPES` set, but that's out of scope for this plan.
