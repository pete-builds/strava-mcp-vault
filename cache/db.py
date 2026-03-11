import json
import os
import time
from datetime import datetime

import aiosqlite

from cache.encryption import encrypt_token, decrypt_token


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
        await self._db.execute(
            "DELETE FROM cache WHERE expires_at < ?", (time.time(),)
        )
        await self._db.commit()

    # ── Vault (permanent activity storage) ────────────────────────────

    async def upsert_activity(self, activity: dict):
        """Store or update a single activity in the vault."""
        now = time.time()
        await self._db.execute(
            "INSERT OR REPLACE INTO activities (id, data, start_date, start_date_local, sport_type, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                activity["id"],
                json.dumps(activity),
                activity.get("start_date"),
                activity.get("start_date_local"),
                activity.get("sport_type") or activity.get("type"),
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
                now,
            )
            for a in activities
        ]
        await self._db.executemany(
            "INSERT OR REPLACE INTO activities (id, data, start_date, start_date_local, sport_type, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
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
        cursor = await self._db.execute(
            "SELECT MAX(start_date) FROM activities"
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return None
        # start_date is ISO format like "2026-03-10T12:00:00Z"
        try:
            dt = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

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
