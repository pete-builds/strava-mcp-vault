import json
import math
import os
import time
from datetime import datetime

import aiosqlite

from strava_mcp_vault.cache.encryption import decrypt_token, encrypt_token


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Updates Strava's fields in place on a re-sync. Deliberately NOT
# "INSERT OR REPLACE": that deletes the row and inserts a fresh one, which
# silently erased location_override, the one column a user sets by hand.
_UPSERT_ACTIVITY_SQL = """
    INSERT INTO activities
        (id, data, start_date, start_date_local, sport_type, start_lat, start_lon, synced_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        data = excluded.data,
        start_date = excluded.start_date,
        start_date_local = excluded.start_date_local,
        sport_type = excluded.sport_type,
        start_lat = excluded.start_lat,
        start_lon = excluded.start_lon,
        synced_at = excluded.synced_at
"""


class CacheDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                cache_key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cache_category ON cache(category);
            CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);

            CREATE TABLE IF NOT EXISTS cache_stats (
                category TEXT PRIMARY KEY,
                hits INTEGER DEFAULT 0,
                misses INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY,
                data TEXT NOT NULL,
                start_date TEXT,
                start_date_local TEXT,
                sport_type TEXT,
                synced_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_date);
            CREATE INDEX IF NOT EXISTS idx_activities_sport ON activities(sport_type);

            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_sync_at REAL,
                total_synced INTEGER DEFAULT 0,
                mode TEXT
            );

            -- Curated, human-named riding locations used to build the public
            -- ride-spot export. Deliberately its OWN table rather than a column
            -- on activities: sync_activities used to upsert activities with
            -- INSERT OR REPLACE, which wiped location_override on every
            -- re-sync. Curation that lives here cannot be destroyed by a sync,
            -- because sync never touches this table.
            CREATE TABLE IF NOT EXISTS ride_spots (
                name TEXT PRIMARY KEY,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                radius_miles REAL NOT NULL DEFAULT 1.0,
                blurb TEXT,
                created_at REAL NOT NULL
            );

            -- One row per sync or repair, never updated or pruned, so a hole
            -- can be dated afterwards. sync_log above keeps only the last run.
            -- Additive: an older vault gains this table on its next start.
            CREATE TABLE IF NOT EXISTS sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                finished_at REAL,
                mode TEXT NOT NULL,
                window_after TEXT,
                window_before TEXT,
                added INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                removed INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0,
                note TEXT
            );

            -- Small facts about the vault itself, such as which Strava athlete
            -- it belongs to, recorded on first use. Additive, like sync_history.
            CREATE TABLE IF NOT EXISTS vault_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        await self._db.commit()

        # Migration: add lat/lon and location_override columns if not present
        for col in ("start_lat REAL", "start_lon REAL", "location_override TEXT"):
            try:
                await self._db.execute(f"ALTER TABLE activities ADD COLUMN {col}")
            except Exception:
                pass  # already exists
        await self._db.execute("""
            UPDATE activities
            SET start_lat = json_extract(data, '$.start_latlng[0]'),
                start_lon = json_extract(data, '$.start_latlng[1]')
            WHERE start_lat IS NULL
        """)
        await self._db.commit()

        await self.cleanup_expired()

    async def get_cached(self, key: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT data, category, expires_at FROM cache WHERE cache_key = ?",
            (key,),
        )
        row = await cursor.fetchone()

        if row is None:
            # Record a miss with unknown category
            await self._db.execute(
                "INSERT OR IGNORE INTO cache_stats (category, hits, misses) VALUES ('unknown', 0, 0)"
            )
            await self._db.execute(
                "UPDATE cache_stats SET misses = misses + 1 WHERE category = 'unknown'"
            )
            await self._db.commit()
            return None

        data, category, expires_at = row

        if expires_at < time.time():
            await self.invalidate(key)
            await self._db.execute(
                "INSERT OR IGNORE INTO cache_stats (category, hits, misses) VALUES (?, 0, 0)",
                (category,),
            )
            await self._db.execute(
                "UPDATE cache_stats SET misses = misses + 1 WHERE category = ?",
                (category,),
            )
            await self._db.commit()
            return None

        await self._db.execute(
            "INSERT OR IGNORE INTO cache_stats (category, hits, misses) VALUES (?, 0, 0)",
            (category,),
        )
        await self._db.execute(
            "UPDATE cache_stats SET hits = hits + 1 WHERE category = ?",
            (category,),
        )
        await self._db.commit()
        return json.loads(data)

    async def set_cached(self, key: str, category: str, data: dict, ttl_seconds: int):
        now = time.time()
        expires_at = now + ttl_seconds
        await self._db.execute(
            "INSERT OR REPLACE INTO cache (cache_key, category, data, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, category, json.dumps(data), now, expires_at),
        )
        await self._db.commit()

    async def invalidate(self, key: str):
        await self._db.execute("DELETE FROM cache WHERE cache_key = ?", (key,))
        await self._db.commit()

    async def invalidate_category(self, category: str):
        await self._db.execute("DELETE FROM cache WHERE category = ?", (category,))
        await self._db.commit()

    async def get_stats(self) -> dict:
        cursor = await self._db.execute("SELECT category, hits, misses FROM cache_stats")
        rows = await cursor.fetchall()
        stats = {row[0]: {"hits": row[1], "misses": row[2]} for row in rows}

        cursor = await self._db.execute("SELECT COUNT(*) FROM cache")
        row = await cursor.fetchone()
        total_items = row[0]

        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "categories": stats,
            "total_cached_items": total_items,
            "db_size_bytes": db_size,
        }

    async def get_tokens(self) -> dict | None:
        cursor = await self._db.execute(
            "SELECT access_token, refresh_token, expires_at FROM tokens WHERE id = 1"
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "access_token": decrypt_token(row[0]),
            "refresh_token": decrypt_token(row[1]),
            "expires_at": row[2],
        }

    async def set_tokens(self, access_token: str, refresh_token: str, expires_at: int):
        await self._db.execute(
            "INSERT OR REPLACE INTO tokens (id, access_token, refresh_token, expires_at) "
            "VALUES (1, ?, ?, ?)",
            (encrypt_token(access_token), encrypt_token(refresh_token), expires_at),
        )
        await self._db.commit()

    async def cleanup_expired(self):
        await self._db.execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))
        await self._db.commit()

    # ── Vault (permanent activity storage) ────────────────────────────

    @staticmethod
    def _extract_latlng(activity: dict) -> tuple[float | None, float | None]:
        coords = activity.get("start_latlng") or []
        if len(coords) == 2:
            return coords[0], coords[1]
        return None, None

    async def upsert_activity(self, activity: dict):
        """Store or update a single activity in the vault."""
        now = time.time()
        lat, lon = self._extract_latlng(activity)
        await self._db.execute(
            _UPSERT_ACTIVITY_SQL,
            (
                activity["id"],
                json.dumps(activity),
                activity.get("start_date"),
                activity.get("start_date_local"),
                activity.get("sport_type") or activity.get("type"),
                lat,
                lon,
                now,
            ),
        )

    async def upsert_activities_batch(self, activities: list[dict]):
        """Store multiple activities in a single transaction."""
        now = time.time()
        rows = [
            (
                a["id"],
                json.dumps(a),
                a.get("start_date"),
                a.get("start_date_local"),
                a.get("sport_type") or a.get("type"),
                *self._extract_latlng(a),
                now,
            )
            for a in activities
        ]
        await self._db.executemany(_UPSERT_ACTIVITY_SQL, rows)
        await self._db.commit()

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

    async def get_vault_totals(
        self,
        sport_type: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> dict:
        """Sum distance, moving time and elevation over every matching activity.

        Summed in SQL with the same filters as get_vault_activity_count, so the
        totals always cover exactly the activities that count reports.
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
        cursor = await self._db.execute(
            "SELECT "
            "COALESCE(SUM(json_extract(data, '$.distance')), 0), "
            "COALESCE(SUM(json_extract(data, '$.moving_time')), 0), "
            "COALESCE(SUM(json_extract(data, '$.total_elevation_gain')), 0) "
            f"FROM activities {where}",
            params,
        )
        distance_m, moving_time_s, elevation_m = await cursor.fetchone()
        return {
            "distance_meters": distance_m,
            "moving_time_seconds": moving_time_s,
            "elevation_meters": elevation_m,
        }

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

    async def get_activities_near_location(
        self,
        lat: float,
        lon: float,
        radius_miles: float = 20.0,
        sport_type: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> list[dict]:
        """Return activities that started within radius_miles of (lat, lon)."""
        # Bounding box pre-filter in SQL, then precise haversine in Python
        lat_delta = radius_miles / 69.0
        lon_delta = radius_miles / (69.0 * math.cos(math.radians(lat)))

        conditions = [
            "start_lat IS NOT NULL",
            "start_lat BETWEEN ? AND ?",
            "start_lon BETWEEN ? AND ?",
        ]
        params: list = [lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta]

        if sport_type:
            conditions.append("sport_type = ?")
            params.append(sport_type)
        if after:
            conditions.append("start_date_local >= ?")
            params.append(after)
        if before:
            conditions.append("start_date_local < ?")
            params.append(before)

        where = f"WHERE {' AND '.join(conditions)}"
        cursor = await self._db.execute(
            f"SELECT data, start_lat, start_lon, location_override FROM activities {where} ORDER BY start_date DESC",
            params,
        )
        rows = await cursor.fetchall()

        results = []
        for data, a_lat, a_lon, loc_override in rows:
            d = _haversine_miles(a_lat, a_lon, lat, lon)
            if d <= radius_miles:
                activity = json.loads(data)
                activity["_distance_from_query_miles"] = round(d, 1)
                if loc_override:
                    activity["_location_override"] = loc_override
                results.append(activity)
        return results

    async def set_location_override(self, activity_id: int, location: str | None) -> bool:
        """Set (or clear) a manual location string for an activity. Returns True if found."""
        cursor = await self._db.execute(
            "UPDATE activities SET location_override = ? WHERE id = ?",
            (location, activity_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # --- Ride spots (curated allowlist for the public export) ---

    async def upsert_ride_spot(
        self,
        name: str,
        lat: float,
        lon: float,
        radius_miles: float = 1.0,
        blurb: str | None = None,
    ) -> None:
        """Create or update one curated ride spot."""
        await self._db.execute(
            """
            INSERT INTO ride_spots (name, lat, lon, radius_miles, blurb, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                lat = excluded.lat,
                lon = excluded.lon,
                radius_miles = excluded.radius_miles,
                blurb = excluded.blurb
            """,
            (name, lat, lon, radius_miles, blurb, time.time()),
        )
        await self._db.commit()

    async def delete_ride_spot(self, name: str) -> bool:
        """Remove a curated ride spot. Returns True if it existed."""
        cursor = await self._db.execute("DELETE FROM ride_spots WHERE name = ?", (name,))
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_ride_spots(self) -> list[dict]:
        """Return every curated ride spot, alphabetically."""
        cursor = await self._db.execute(
            "SELECT name, lat, lon, radius_miles, blurb FROM ride_spots ORDER BY name"
        )
        rows = await cursor.fetchall()
        return [
            {
                "name": name,
                "lat": lat,
                "lon": lon,
                "radius_miles": radius_miles,
                "blurb": blurb,
            }
            for name, lat, lon, radius_miles, blurb in rows
        ]

    async def get_public_activities_with_geometry(
        self,
        sport_types: list[str],
        after: str | None = None,
        before: str | None = None,
    ) -> list[dict]:
        """Return publishable activities that carry route geometry.

        The visibility test is an explicit allowlist (`= 'everyone'`), so an
        activity with a missing or unrecognized visibility value is withheld
        rather than published. `spots.build_export` re-applies the same test on
        the dicts, so the guarantee does not rest on this WHERE clause alone.

        Reads only the `activities` table. It deliberately does not touch the
        cache table, whose `activity_detail` entries expire after a day and,
        before sync stopped writing them, held stripped-down list summaries.
        """
        if not sport_types:
            return []

        placeholders = ",".join("?" for _ in sport_types)
        conditions = [
            f"sport_type IN ({placeholders})",
            "start_lat IS NOT NULL",
            "json_extract(data, '$.map.summary_polyline') IS NOT NULL",
            "json_extract(data, '$.map.summary_polyline') != ''",
            "json_extract(data, '$.visibility') = ?",
            "COALESCE(json_extract(data, '$.private'), 0) = 0",
        ]
        params: list = [*sport_types, "everyone"]

        if after:
            conditions.append("start_date_local >= ?")
            params.append(after)
        if before:
            conditions.append("start_date_local < ?")
            params.append(before)

        cursor = await self._db.execute(
            f"SELECT data FROM activities WHERE {' AND '.join(conditions)} "
            "ORDER BY start_date DESC",
            params,
        )
        rows = await cursor.fetchall()
        return [json.loads(row[0]) for row in rows]

    async def get_activity_row(self, activity_id: int) -> dict | None:
        """Return one activity's stored Strava JSON, or None if not in the vault.

        Reads the `activities` table, never the cache: the `activity_detail`
        cache entries expire after a day, and sync used to overwrite them with
        stripped-down list summaries (audit finding 2). `visibility` and `map`
        are load-bearing here, so this reads the permanent row.
        """
        cursor = await self._db.execute("SELECT data FROM activities WHERE id = ?", (activity_id,))
        row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def get_vault_date_range(self) -> dict | None:
        """Return the earliest and latest activity dates in the vault."""
        cursor = await self._db.execute(
            "SELECT MIN(start_date_local), MAX(start_date_local) FROM activities"
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None
        return {"earliest": row[0], "latest": row[1]}

    async def get_latest_activity_epoch(self) -> int | None:
        """Return the epoch timestamp of the most recent activity in the vault.

        Used for incremental sync (the 'after' parameter).
        """
        cursor = await self._db.execute("SELECT MAX(start_date) FROM activities")
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None
        # start_date is ISO format like "2026-03-10T12:00:00Z"
        try:
            dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    async def delete_activities(self, activity_ids: list[int]) -> int:
        """Delete activities from the vault by ID. Returns number of rows deleted."""
        if not activity_ids:
            return 0

        placeholders = ",".join("?" * len(activity_ids))
        cursor = await self._db.execute(
            f"DELETE FROM activities WHERE id IN ({placeholders})",
            activity_ids,
        )
        await self._db.commit()
        return cursor.rowcount

    async def get_vault_index(
        self,
        after: str | None = None,
        before: str | None = None,
        sport_types: list[str] | None = None,
    ) -> list[dict]:
        """The light per-activity facts a vault check compares, for a window.

        Filters on start_date_local exactly as query_vault does, so a check
        and a query over the same dates count the same activities.
        """
        conditions = []
        params: list = []
        if after:
            conditions.append("start_date_local >= ?")
            params.append(after)
        if before:
            conditions.append("start_date_local < ?")
            params.append(before)
        if sport_types:
            # Case-insensitive, so "ride" means Ride rather than matching nothing.
            conditions.append(f"LOWER(sport_type) IN ({','.join('?' for _ in sport_types)})")
            params.extend(t.lower() for t in sport_types)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            "SELECT id, start_date_local, sport_type, json_extract(data, '$.distance'), "
            "json_extract(data, '$.name'), location_override, "
            "COALESCE(json_extract(data, '$.private'), 0) "
            "OR COALESCE(json_extract(data, '$.visibility'), '') = 'only_me' "
            f"FROM activities {where} ORDER BY start_date_local",
            params,
        )
        return [
            {
                "id": row[0],
                "start_date_local": row[1],
                "sport_type": row[2],
                "distance": row[3] or 0,
                "name": row[4],
                "location_override": row[5],
                "private": bool(row[6]),
            }
            for row in await cursor.fetchall()
        ]

    async def get_vault_athlete_ids(self) -> set[int]:
        """The Strava athlete ids carried on the vault's stored activities."""
        cursor = await self._db.execute(
            "SELECT DISTINCT CAST(json_extract(data, '$.athlete.id') AS INTEGER) "
            "FROM activities WHERE json_extract(data, '$.athlete.id') IS NOT NULL"
        )
        return {row[0] for row in await cursor.fetchall()}

    async def get_meta(self, key: str) -> str | None:
        cursor = await self._db.execute("SELECT value FROM vault_meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO vault_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._db.commit()

    async def get_existing_ids(self, activity_ids: list[int]) -> set[int]:
        """Which of these ids the vault already holds."""
        found: set[int] = set()
        ids = list(activity_ids)
        for start in range(0, len(ids), 500):  # stay under SQLite's variable limit
            chunk = ids[start : start + 500]
            cursor = await self._db.execute(
                f"SELECT id FROM activities WHERE id IN ({','.join('?' for _ in chunk)})",
                chunk,
            )
            found.update(row[0] for row in await cursor.fetchall())
        return found

    async def delete_activities_reporting(self, activity_ids: list[int]) -> list[dict]:
        """Delete activities in ONE transaction and return exactly what went.

        Each returned dict names the id, date, type, name and any hand-set
        location the row carried, read inside the same transaction as the
        delete, so the report cannot drift from what was removed.
        """
        if not activity_ids:
            return []
        placeholders = ",".join("?" for _ in activity_ids)
        try:
            await self._db.execute("BEGIN IMMEDIATE")
            cursor = await self._db.execute(
                "SELECT id, start_date_local, sport_type, json_extract(data, '$.name'), "
                f"location_override FROM activities WHERE id IN ({placeholders}) "
                "ORDER BY start_date_local",
                list(activity_ids),
            )
            rows = await cursor.fetchall()
            await self._db.execute(
                f"DELETE FROM activities WHERE id IN ({placeholders})", list(activity_ids)
            )
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise
        return [
            {
                "id": r[0],
                "start_date_local": r[1],
                "sport_type": r[2],
                "name": r[3],
                "location_override": r[4],
            }
            for r in rows
        ]

    async def add_sync_history(
        self,
        started_at: float,
        mode: str,
        window_after: str | None,
        window_before: str | None,
        added: int,
        updated: int,
        completed: bool,
        removed: int = 0,
        note: str | None = None,
    ) -> None:
        """Append one row to the sync history. Rows are never updated."""
        await self._db.execute(
            "INSERT INTO sync_history (started_at, finished_at, mode, window_after, "
            "window_before, added, updated, removed, completed, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                started_at,
                time.time(),
                mode,
                window_after,
                window_before,
                added,
                updated,
                removed,
                1 if completed else 0,
                note,
            ),
        )
        await self._db.commit()

    async def get_sync_history(self, limit: int = 10) -> list[dict]:
        """The most recent sync history rows, newest first."""
        cursor = await self._db.execute(
            "SELECT started_at, finished_at, mode, window_after, window_before, added, "
            "updated, removed, completed, note FROM sync_history ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        keys = (
            "started_at",
            "finished_at",
            "mode",
            "window_after",
            "window_before",
            "added",
            "updated",
            "removed",
            "completed",
            "note",
        )
        return [
            {**dict(zip(keys, row, strict=True)), "completed": bool(row[8])}
            for row in await cursor.fetchall()
        ]

    async def update_sync_log(self, total_synced: int, mode: str):
        """Record sync completion."""
        now = time.time()
        await self._db.execute(
            "INSERT OR REPLACE INTO sync_log (id, last_sync_at, total_synced, mode) "
            "VALUES (1, ?, ?, ?)",
            (now, total_synced, mode),
        )
        await self._db.commit()

    async def get_sync_log(self) -> dict | None:
        """Return the last sync info."""
        cursor = await self._db.execute(
            "SELECT last_sync_at, total_synced, mode FROM sync_log WHERE id = 1"
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "last_sync_at": row[0],
            "total_synced": row[1],
            "mode": row[2],
        }

    async def close(self):
        if self._db:
            await self._db.close()
