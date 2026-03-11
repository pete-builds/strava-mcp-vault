import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from cache.db import CacheDB
from cache.manager import CacheManager
from clients.strava import StravaClient, RateLimitError

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Globals initialized in lifespan
manager: CacheManager | None = None


async def _startup():
    """Initialize DB, client, and cache manager on server start."""
    global manager

    # Validate required env vars
    required = ["STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)

    # Init database
    db_path = os.getenv("VAULT_DB_PATH", "/app/data/vault.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = CacheDB(db_path)
    await db.init()

    # Init Strava client
    client = StravaClient(
        client_id=os.getenv("STRAVA_CLIENT_ID"),
        client_secret=os.getenv("STRAVA_CLIENT_SECRET"),
        cache_db=db,
    )
    await client.init_tokens()

    # If no tokens in DB, seed from env vars (first boot)
    if client._access_token is None:
        access_token = os.getenv("STRAVA_ACCESS_TOKEN")
        refresh_token = os.getenv("STRAVA_REFRESH_TOKEN")
        if not access_token or not refresh_token:
            logger.error(
                "First boot: STRAVA_ACCESS_TOKEN and STRAVA_REFRESH_TOKEN required"
            )
            sys.exit(1)
        # Set expires_at to 0 to force immediate refresh
        await db.set_tokens(access_token, refresh_token, 0)
        client._access_token = access_token
        client._refresh_token = refresh_token
        client._expires_at = 0
        logger.info("Seeded tokens from env vars (will refresh on first request)")

    manager = CacheManager(db, client)
    logger.info("strava-mcp-vault initialized")


@asynccontextmanager
async def lifespan(server):
    await _startup()
    yield


port = int(os.getenv("STRAVA_MCP_PORT", "18201"))
mcp = FastMCP("strava-vault", host="0.0.0.0", port=port, lifespan=lifespan)


@mcp.tool()
async def get_recent_activities(count: int = 10) -> str:
    """List recent Strava activities with distance, time, and stats.

    Args:
        count: Number of activities to return (default 10, max 200).
    """
    try:
        results = await manager.get_recent_activities(count)
        return json.dumps(results, indent=2)
    except RateLimitError as e:
        return str(e)


@mcp.tool()
async def get_activity(activity_id: int) -> str:
    """Get full details for a specific Strava activity.

    Args:
        activity_id: The Strava activity ID.
    """
    try:
        result = await manager.get_activity(activity_id)
        return json.dumps(result, indent=2)
    except RateLimitError as e:
        return str(e)


@mcp.tool()
async def get_activity_streams(
    activity_id: int, stream_types: str = "heartrate,distance,altitude"
) -> str:
    """Get time-series data for an activity (heart rate, elevation, etc).

    Args:
        activity_id: The Strava activity ID.
        stream_types: Comma-separated stream types (e.g. heartrate,distance,altitude).
    """
    try:
        result = await manager.get_activity_streams(activity_id, stream_types)
        return json.dumps(result, indent=2)
    except RateLimitError as e:
        return str(e)


@mcp.tool()
async def get_athlete_profile() -> str:
    """Get the authenticated Strava athlete's profile."""
    try:
        result = await manager.get_athlete_profile()
        return json.dumps(result, indent=2)
    except RateLimitError as e:
        return str(e)


@mcp.tool()
async def get_athlete_stats() -> str:
    """Get year-to-date and all-time activity statistics."""
    try:
        result = await manager.get_athlete_stats()
        return json.dumps(result, indent=2)
    except RateLimitError as e:
        return str(e)


@mcp.tool()
async def get_cache_stats() -> str:
    """Show cache hit/miss rates, stored items, and API rate limit status."""
    stats = await manager.get_cache_stats()
    return json.dumps(stats, indent=2)


@mcp.tool()
async def sync_activities(days_back: int = 30) -> str:
    """Bulk-sync recent activities into the local cache.

    Fetches all activities from the last N days and caches them locally.
    This reduces future API calls and enables offline access to cached data.

    Args:
        days_back: Number of days to sync (default 30).
    """
    try:
        result = await manager.sync_activities(days_back)
        return json.dumps(result, indent=2)
    except RateLimitError as e:
        return str(e)


if __name__ == "__main__":
    mcp.run(transport="sse")
