import json
import os
import time

import aiosqlite


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
            "access_token": row[0],
            "refresh_token": row[1],
            "expires_at": row[2],
        }

    async def set_tokens(self, access_token: str, refresh_token: str, expires_at: int):
        await self._db.execute(
            "INSERT OR REPLACE INTO tokens (id, access_token, refresh_token, expires_at) "
            "VALUES (1, ?, ?, ?)",
            (access_token, refresh_token, expires_at),
        )
        await self._db.commit()

    async def cleanup_expired(self):
        await self._db.execute(
            "DELETE FROM cache WHERE expires_at < ?", (time.time(),)
        )
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
