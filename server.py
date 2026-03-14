import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from cache.db import CacheDB
from cache.geocode import forward_geocode, reverse_geocode_many
from cache.manager import CacheManager
from clients.strava import StravaClient, RateLimitError
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
    format_activities_near,
    format_delete_activities,
)

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


@mcp.tool()
async def get_activity(activity_id: int) -> str:
    """Get full details for a specific Strava activity.

    Args:
        activity_id: The Strava activity ID.
    """
    try:
        result = await manager.get_activity(activity_id)
        return format_activity_detail(result)
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
        return format_activity_streams(result, activity_id)
    except RateLimitError as e:
        return str(e)


@mcp.tool()
async def get_athlete_profile() -> str:
    """Get the authenticated Strava athlete's profile."""
    try:
        result = await manager.get_athlete_profile()
        return format_athlete_profile(result)
    except RateLimitError as e:
        return str(e)


@mcp.tool()
async def get_athlete_stats() -> str:
    """Get year-to-date and all-time activity statistics."""
    try:
        result = await manager.get_athlete_stats()
        return format_athlete_stats(result)
    except RateLimitError as e:
        return str(e)




def _validate_radius_miles(radius_miles: float) -> str | None:
    if radius_miles <= 0:
        return "radius_miles must be greater than 0."
    if radius_miles > 250:
        return "radius_miles is too large. Use 250 miles or less."
    return None

@mcp.tool()
async def get_cache_stats() -> str:
    """Show cache hit/miss rates, stored items, and API rate limit status."""
    stats = await manager.get_cache_stats()
    return format_cache_stats(stats)


@mcp.tool()
async def get_activities_near(
    location: str,
    radius_miles: float = 20.0,
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
) -> str:
    """Find vault activities that started near a given location.

    Geocodes the location name, then searches the local vault for activities
    that started within the specified radius. No Strava API calls are made.

    Args:
        location: Place name to search near (e.g. "Syracuse, NY", "Central Park").
        radius_miles: Search radius in miles (default 20).
        sport_type: Filter by activity type (e.g. "Ride", "Run", "GravelRide").
        after: Only activities on or after this date (ISO format, e.g. "2025-01-01").
        before: Only activities before this date (ISO format, e.g. "2026-01-01").
    """
    location = (location or "").strip()
    if not location:
        return "Location is required. Example: 'Syracuse, NY'."

    radius_error = _validate_radius_miles(radius_miles)
    if radius_error:
        return radius_error

    coords = await forward_geocode(location)
    if coords is None:
        return f"Could not geocode '{location}'. Try a more specific place name."
    lat, lon = coords
    results = await manager.db.get_activities_near_location(
        lat, lon, radius_miles=radius_miles,
        sport_type=sport_type, after=after, before=before,
    )
    if results:
        activity_coords = [
            (a["start_latlng"][0], a["start_latlng"][1])
            for a in results
            if a.get("start_latlng") and len(a["start_latlng"]) == 2
        ]
        location_map = await reverse_geocode_many(activity_coords)
        for a in results:
            if a.get("_location_override"):
                a["_location"] = a["_location_override"]
            else:
                coords_key = tuple(a["start_latlng"][:2]) if a.get("start_latlng") else None
                a["_location"] = location_map.get(coords_key, "") if coords_key else ""
    return format_activities_near(results, location, radius_miles)


@mcp.tool()
async def set_activity_location(activity_id: int, location: str | None = None) -> str:
    """Manually set (or clear) the display location for a vault activity.

    Useful for activities recorded indoors or without GPS where no location
    can be reverse geocoded. Pass location=None to clear an override.

    Args:
        activity_id: The Strava activity ID to update.
        location: Location string to display (e.g. "Ithaca, NY"). Pass null to clear.
    """
    found = await manager.db.set_location_override(activity_id, location)
    if not found:
        return f"Activity {activity_id} not found in vault."
    if location:
        return f"✅ Location for activity {activity_id} set to \"{location}\"."
    return f"✅ Location override cleared for activity {activity_id}."


@mcp.tool()
async def delete_vault_activity(activity_ids: list[int]) -> str:
    """Delete one or more activities from the local vault by Strava activity ID.

    This only removes activities from the local database — it does not delete
    them from Strava. Useful for removing duplicates or unwanted entries.

    Args:
        activity_ids: List of Strava activity IDs to delete (e.g. [12345, 67890]).
    """
    if not activity_ids:
        return "No activity IDs provided. Pass one or more IDs, e.g. [12345]."

    deleted = await manager.db.delete_activities(activity_ids)
    return format_delete_activities(deleted, activity_ids)


@mcp.tool()
async def sync_activities(days_back: int = 0) -> str:
    """Sync Strava activities into the local vault.

    Smart sync behavior:
    - First run (empty vault): pulls ALL historical activities
    - Subsequent runs: only fetches activities newer than the latest stored
    - With days_back > 0: fetches a specific time window (useful for refreshing)

    Activities are stored permanently in the vault. No data expires.
    Typically takes 1-3 API calls for a full sync (~200 activities).

    Args:
        days_back: 0 = auto (incremental or full). >0 = fetch last N days.
    """
    try:
        result = await manager.sync_activities(days_back)
        return format_sync_result(result)
    except RateLimitError as e:
        return str(e)


if __name__ == "__main__":
    import uvicorn
    from auth import maybe_add_auth

    app = mcp.sse_app()
    maybe_add_auth(app)
    uvicorn.run(app, host="0.0.0.0", port=port)
