import json
import math
import os
import sqlite3
import time
from datetime import datetime

import aiosqlite

from strava_mcp_vault.cache.encryption import decrypt_token, encrypt_token
from strava_mcp_vault.sport_types import expand_sport_type


def _sport_type_clause(value: str | None) -> tuple[str, list]:
    """Build a SQL fragment + params for a sport_type filter.

    Returns ("", []) when no filter applies. Otherwise returns a
    'sport_type IN (?, ?, ...)' clause with one placeholder per expanded
    sport type. Aliases like "rides" expand to all ride members; a single
    type like "Ride" yields a one-element IN clause.
    """
    expanded = expand_sport_type(value)
    if not expanded:
        return "", []
    placeholders = ",".join("?" for _ in expanded)
    return f"sport_type IN ({placeholders})", list(expanded)


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class CacheDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self.db_path)
        # WAL lets concurrent readers proceed while a writer is active.
        # synchronous=NORMAL is safe with WAL and avoids fsync per commit.
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA synchronous = NORMAL")
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
        """)
        await self._db.commit()

        # Migration: add lat/lon, location_override, and power columns if not
        # present. Tolerate "duplicate column name" — anything else is a real
        # error. Power columns are denormalized from the JSON blob to make the
        # has_power filter and aggregate queries efficient.
        for col in (
            "start_lat REAL",
            "start_lon REAL",
            "location_override TEXT",
            "average_watts REAL",
            "weighted_average_watts REAL",
            "max_watts REAL",
            "kilojoules REAL",
            "device_watts INTEGER",
        ):
            try:
                await self._db.execute(f"ALTER TABLE activities ADD COLUMN {col}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        await self._db.execute("""
            UPDATE activities
            SET start_lat = json_extract(data, '$.start_latlng[0]'),
                start_lon = json_extract(data, '$.start_latlng[1]')
            WHERE start_lat IS NULL
        """)
        # Backfill power columns from JSON blobs for activities synced before
        # the schema migration. Only touches rows where the column is NULL so
        # repeated runs are no-ops.
        await self._db.execute("""
            UPDATE activities
            SET average_watts = json_extract(data, '$.average_watts'),
                weighted_average_watts = json_extract(data, '$.weighted_average_watts'),
                max_watts = json_extract(data, '$.max_watts'),
                kilojoules = json_extract(data, '$.kilojoules'),
                device_watts = json_extract(data, '$.device_watts')
            WHERE average_watts IS NULL
              AND json_extract(data, '$.average_watts') IS NOT NULL
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_activities_power ON activities(average_watts)"
        )
        await self._db.commit()

        await self.cleanup_expired()

    async def _bump_stat(self, category: str, *, hit: bool):
        """Atomically increment a category's hit or miss counter."""
        if hit:
            sql = (
                "INSERT INTO cache_stats (category, hits, misses) VALUES (?, 1, 0) "
                "ON CONFLICT(category) DO UPDATE SET hits = hits + 1"
            )
        else:
            sql = (
                "INSERT INTO cache_stats (category, hits, misses) VALUES (?, 0, 1) "
                "ON CONFLICT(category) DO UPDATE SET misses = misses + 1"
            )
        await self._db.execute(sql, (category,))
        await self._db.commit()

    async def get_cached(self, key: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT data, category, expires_at FROM cache WHERE cache_key = ?",
            (key,),
        )
        row = await cursor.fetchone()

        if row is None:
            await self._bump_stat("unknown", hit=False)
            return None

        data, category, expires_at = row

        if expires_at < time.time():
            await self.invalidate(key)
            await self._bump_stat(category, hit=False)
            return None

        await self._bump_stat(category, hit=True)
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
        # Opportunistic cleanup: stats is called by get_cache_stats, which is
        # the most natural moment to evict expired rows without a background task.
        await self.cleanup_expired()
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

    @staticmethod
    def _extract_power(
        activity: dict,
    ) -> tuple[float | None, float | None, float | None, float | None, int | None]:
        """Pull power fields out of an activity dict for column denormalization.

        Returns (average_watts, weighted_average_watts, max_watts, kilojoules,
        device_watts). device_watts is stored as 1/0/NULL since SQLite has no
        native bool.
        """
        device = activity.get("device_watts")
        device_int = None if device is None else (1 if device else 0)
        return (
            activity.get("average_watts"),
            activity.get("weighted_average_watts"),
            activity.get("max_watts"),
            activity.get("kilojoules"),
            device_int,
        )

    async def upsert_activity(self, activity: dict):
        """Store or update a single activity in the vault."""
        now = time.time()
        lat, lon = self._extract_latlng(activity)
        avg_w, wt_avg_w, max_w, kj, dev_w = self._extract_power(activity)
        await self._db.execute(
            "INSERT OR REPLACE INTO activities "
            "(id, data, start_date, start_date_local, sport_type, start_lat, start_lon, "
            "average_watts, weighted_average_watts, max_watts, kilojoules, device_watts, "
            "synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                activity["id"],
                json.dumps(activity),
                activity.get("start_date"),
                activity.get("start_date_local"),
                activity.get("sport_type") or activity.get("type"),
                lat,
                lon,
                avg_w,
                wt_avg_w,
                max_w,
                kj,
                dev_w,
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
                *self._extract_power(a),
                now,
            )
            for a in activities
        ]
        await self._db.executemany(
            "INSERT OR REPLACE INTO activities "
            "(id, data, start_date, start_date_local, sport_type, start_lat, start_lon, "
            "average_watts, weighted_average_watts, max_watts, kilojoules, device_watts, "
            "synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self._db.commit()

    async def get_vault_activities(
        self,
        limit: int = 10,
        offset: int = 0,
        sport_type: str | None = None,
        after: str | None = None,
        before: str | None = None,
        has_power: bool | None = None,
    ) -> list[dict]:
        """Query activities from the vault with optional filters.

        Args:
            limit: Max activities to return.
            offset: Skip this many results.
            sport_type: Filter by Strava sport_type or category alias. Accepts
                a single type ("Ride"), a comma-separated list ("Ride,Run"),
                or a category alias ("rides", "running"). See
                strava_mcp_vault.sport_types for known aliases.
            after: Only activities on or after this ISO date (e.g. "2026-01-01").
            before: Only activities before this ISO date (e.g. "2026-04-01").
            has_power: If True, only activities that recorded power data.
                If False, only activities without power data. If None (default),
                no power filter.
        """
        conditions = []
        params: list = []

        sport_clause, sport_params = _sport_type_clause(sport_type)
        if sport_clause:
            conditions.append(sport_clause)
            params.extend(sport_params)
        if after:
            conditions.append("start_date_local >= ?")
            params.append(after)
        if before:
            conditions.append("start_date_local < ?")
            params.append(before)
        if has_power is True:
            conditions.append("average_watts IS NOT NULL")
        elif has_power is False:
            conditions.append("average_watts IS NULL")

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
        has_power: bool | None = None,
    ) -> int:
        """Return count of activities in the vault, with optional filters.

        See ``get_vault_activities`` for argument semantics.
        """
        conditions = []
        params: list = []

        sport_clause, sport_params = _sport_type_clause(sport_type)
        if sport_clause:
            conditions.append(sport_clause)
            params.extend(sport_params)
        if after:
            conditions.append("start_date_local >= ?")
            params.append(after)
        if before:
            conditions.append("start_date_local < ?")
            params.append(before)
        if has_power is True:
            conditions.append("average_watts IS NOT NULL")
        elif has_power is False:
            conditions.append("average_watts IS NULL")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(f"SELECT COUNT(*) FROM activities {where}", params)
        row = await cursor.fetchone()
        return row[0]

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

        sport_clause, sport_params = _sport_type_clause(sport_type)
        if sport_clause:
            conditions.append(sport_clause)
            params.extend(sport_params)
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
        """Set (or clear) a manual location string for an activity. Returns True if found.

        An empty string is treated as a clear (stored as NULL).
        """
        normalized = location if location else None
        cursor = await self._db.execute(
            "UPDATE activities SET location_override = ? WHERE id = ?",
            (normalized, activity_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

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
