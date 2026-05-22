import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import Context, FastMCP

from cache.db import CacheDB
from cache.geocode import forward_geocode, reverse_geocode_many
from cache.manager import CacheManager
from clients.strava import StravaClient
from exceptions import RateLimitError, StravaAPIError, VaultError
from formatters import (
    format_activities_near,
    format_activity_detail,
    format_activity_streams,
    format_athlete_profile,
    format_athlete_stats,
    format_cache_stats,
    format_delete_activities,
    format_recent_activities,
    format_recent_activities_compact,
    format_sync_result,
    format_vault_query,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _jsonify(obj) -> str:
    """Serialize tool output as pretty JSON. Non-serializable values stringify."""
    return json.dumps(obj, indent=2, default=str)


def _tool_error(tool_name: str, e: Exception) -> str:
    """Map an exception to an actionable error string for MCP clients.

    Specific Strava errors get tailored guidance (rate limit, auth, not found);
    other VaultErrors stringify directly; anything else logs a traceback and
    returns the type+message.
    """
    if isinstance(e, RateLimitError):
        return f"Strava rate limit hit. Wait a few minutes before retrying. ({e})"
    if isinstance(e, StravaAPIError):
        if e.status_code == 404:
            return f"Strava API: resource not found. Check the ID. ({e.path})"
        if e.status_code in (401, 403):
            return (
                f"Strava API: unauthorized ({e.path}). The access token may be "
                "expired or revoked; reseed STRAVA_ACCESS_TOKEN / STRAVA_REFRESH_TOKEN."
            )
        if e.status_code == 429:
            return f"Strava API: rate limited. Wait before retrying. ({e.path})"
        return f"Strava API error: {e}"
    if isinstance(e, VaultError):
        return f"Error: {e}"
    logger.exception("Unexpected error in %s", tool_name)
    return f"Unexpected error: {type(e).__name__}: {e}"


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

    # Refuse to start unauthenticated unless explicitly opted in. The MCP
    # endpoint exposes private Strava data, so accidentally running without
    # MCP_AUTH_TOKEN on a routable interface is a foot-gun.
    if not os.getenv("MCP_AUTH_TOKEN") and not os.getenv("MCP_ALLOW_UNAUTHENTICATED"):
        logger.error(
            "Refusing to start without authentication. "
            "Set MCP_AUTH_TOKEN=<bearer-token> to enable auth, or "
            "MCP_ALLOW_UNAUTHENTICATED=1 to opt out (only safe behind a "
            "trusted network like Tailscale or with localhost-only port binding)."
        )
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
            logger.error("First boot: STRAVA_ACCESS_TOKEN and STRAVA_REFRESH_TOKEN required")
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
mcp = FastMCP("strava_mcp", host="0.0.0.0", port=port, lifespan=lifespan)


@mcp.tool(
    name="strava_get_recent_activities",
    annotations={
        "title": "List recent Strava activities",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_recent_activities(
    count: int = 10,
    offset: int = 0,
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
    compact: bool = False,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """List recent Strava activities with distance, time, and stats.

    Args:
        count: Number of activities to return (default 10, max 200).
        offset: Skip the first N activities for pagination (default 0).
        sport_type: Filter by activity type (e.g. "Ride", "Run", "GravelRide", "Snowboard").
        after: Only activities on or after this date (ISO format, e.g. "2026-01-01").
        before: Only activities before this date (ISO format, e.g. "2026-04-01").
        compact: If true, return a compact one-line-per-activity table instead of full cards.
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        results = await manager.get_recent_activities(
            count,
            offset=offset,
            sport_type=sport_type,
            after=after,
            before=before,
        )
        if response_format == "json":
            total = await manager.db.get_vault_activity_count(
                sport_type=sport_type, after=after, before=before
            )
            return _jsonify({
                "total": total,
                "count": len(results),
                "offset": offset,
                "items": results,
                "has_more": offset + len(results) < total,
                "next_offset": offset + len(results) if offset + len(results) < total else None,
            })
        if compact:
            return format_recent_activities_compact(results)
        return format_recent_activities(results)
    except Exception as e:
        return _tool_error("get_recent_activities", e)


@mcp.tool(
    name="strava_query_vault",
    annotations={
        "title": "Summarize vault activities",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def query_vault(
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Query the activity vault for counts and totals with optional filters.

    Returns a summary with total count, distance, time, elevation,
    and breakdown by activity type. Much lighter than fetching full
    activity lists. Great for questions like "how many rides this year?"

    Args:
        sport_type: Filter by activity type (e.g. "Ride", "Run", "GravelRide").
        after: Only activities on or after this date (ISO format, e.g. "2026-01-01").
        before: Only activities before this date (ISO format, e.g. "2026-04-01").
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        result = await manager.query_vault(
            sport_type=sport_type,
            after=after,
            before=before,
        )
        if response_format == "json":
            return _jsonify(result)
        return format_vault_query(result)
    except Exception as e:
        return _tool_error("query_vault", e)


@mcp.tool(
    name="strava_get_activity",
    annotations={
        "title": "Get full Strava activity detail",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_activity(activity_id: int, response_format: Literal["json", "markdown"] = "markdown") -> str:
    """Get full details for a specific Strava activity.

    Args:
        activity_id: The Strava activity ID.
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        result = await manager.get_activity(activity_id)
        if response_format == "json":
            return _jsonify(result)
        return format_activity_detail(result)
    except Exception as e:
        return _tool_error("get_activity", e)


@mcp.tool(
    name="strava_get_activity_streams",
    annotations={
        "title": "Get activity time-series streams",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_activity_streams(
    activity_id: int,
    stream_types: str = "heartrate,distance,altitude",
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Get time-series data for an activity (heart rate, elevation, etc).

    Args:
        activity_id: The Strava activity ID.
        stream_types: Comma-separated stream types (e.g. heartrate,distance,altitude).
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        result = await manager.get_activity_streams(activity_id, stream_types)
        if response_format == "json":
            return _jsonify(result)
        return format_activity_streams(result, activity_id)
    except Exception as e:
        return _tool_error("get_activity_streams", e)


@mcp.tool(
    name="strava_get_athlete_profile",
    annotations={
        "title": "Get authenticated athlete profile",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_athlete_profile(response_format: Literal["json", "markdown"] = "markdown") -> str:
    """Get the authenticated Strava athlete's profile.

    Args:
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        result = await manager.get_athlete_profile()
        if response_format == "json":
            return _jsonify(result)
        return format_athlete_profile(result)
    except Exception as e:
        return _tool_error("get_athlete_profile", e)


@mcp.tool(
    name="strava_get_athlete_stats",
    annotations={
        "title": "Get athlete YTD and all-time stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_athlete_stats(response_format: Literal["json", "markdown"] = "markdown") -> str:
    """Get year-to-date and all-time activity statistics.

    Args:
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        result = await manager.get_athlete_stats()
        if response_format == "json":
            return _jsonify(result)
        return format_athlete_stats(result)
    except Exception as e:
        return _tool_error("get_athlete_stats", e)


def _validate_radius_miles(radius_miles: float) -> str | None:
    if radius_miles <= 0:
        return "radius_miles must be greater than 0."
    if radius_miles > 250:
        return "radius_miles is too large. Use 250 miles or less."
    return None


@mcp.tool(
    name="strava_get_cache_stats",
    annotations={
        "title": "Show cache and rate-limit stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def get_cache_stats(response_format: Literal["json", "markdown"] = "markdown") -> str:
    """Show cache hit/miss rates, stored items, and API rate limit status.

    Args:
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    try:
        stats = await manager.get_cache_stats()
        if response_format == "json":
            return _jsonify(stats)
        return format_cache_stats(stats)
    except Exception as e:
        return _tool_error("get_cache_stats", e)


@mcp.tool(
    name="strava_get_activities_near",
    annotations={
        "title": "Find vault activities near a location",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def get_activities_near(
    location: str,
    radius_miles: float = 20.0,
    sport_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 50,
    offset: int = 0,
    response_format: Literal["json", "markdown"] = "markdown",
) -> str:
    """Find vault activities that started near a given location.

    Geocodes the location name, then searches the local vault for activities
    that started within the specified radius. No Strava API calls are made.

    Args:
        location: Place name to search near (e.g. "Syracuse, NY", "Central Park").
        radius_miles: Search radius in miles (default 20, max 250).
        sport_type: Filter by activity type (e.g. "Ride", "Run", "GravelRide").
        after: Only activities on or after this date (ISO format, e.g. "2025-01-01").
        before: Only activities before this date (ISO format, e.g. "2026-01-01").
        limit: Maximum activities to return (default 50, max 500).
        offset: Skip the first N results for pagination (default 0).
        response_format: "markdown" (default, human-readable) or "json" (machine-readable).
    """
    location = (location or "").strip()
    if not location:
        return "Location is required. Example: 'Syracuse, NY'."

    radius_error = _validate_radius_miles(radius_miles)
    if radius_error:
        return radius_error

    if limit < 1 or limit > 500:
        return "limit must be between 1 and 500."
    if offset < 0:
        return "offset must be >= 0."

    coords = await forward_geocode(location)
    if coords is None:
        return f"Could not geocode '{location}'. Try a more specific place name."
    lat, lon = coords
    all_results = await manager.db.get_activities_near_location(
        lat,
        lon,
        radius_miles=radius_miles,
        sport_type=sport_type,
        after=after,
        before=before,
    )
    total = len(all_results)
    results = all_results[offset : offset + limit]
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

    if response_format == "json":
        return _jsonify({
            "total": total,
            "count": len(results),
            "offset": offset,
            "items": results,
            "has_more": offset + len(results) < total,
            "next_offset": offset + len(results) if offset + len(results) < total else None,
            "location": location,
            "radius_miles": radius_miles,
        })
    return format_activities_near(results, location, radius_miles)


@mcp.tool(
    name="strava_set_activity_location",
    annotations={
        "title": "Set or clear display location for a vault activity",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def set_activity_location(activity_id: int, location: str | None = None) -> str:
    """Manually set (or clear) the display location for a vault activity.

    Useful for activities recorded indoors or without GPS where no location
    can be reverse geocoded. Pass location=None to clear an override.

    Args:
        activity_id: The Strava activity ID to update.
        location: Location string to display (e.g. "Ithaca, NY"). Pass null to clear.
    """
    try:
        found = await manager.db.set_location_override(activity_id, location)
        if not found:
            return f"Activity {activity_id} not found in vault."
        if location:
            return f'✅ Location for activity {activity_id} set to "{location}".'
        return f"✅ Location override cleared for activity {activity_id}."
    except Exception as e:
        return _tool_error("set_activity_location", e)


@mcp.tool(
    name="strava_delete_vault_activity",
    annotations={
        "title": "Delete activities from local vault",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def delete_vault_activity(activity_ids: list[int]) -> str:
    """Delete one or more activities from the local vault by Strava activity ID.

    This only removes activities from the local database — it does not delete
    them from Strava. Useful for removing duplicates or unwanted entries.

    Args:
        activity_ids: List of Strava activity IDs to delete (e.g. [12345, 67890]).
    """
    if not activity_ids:
        return "No activity IDs provided. Pass one or more IDs, e.g. [12345]."

    try:
        deleted = await manager.db.delete_activities(activity_ids)
        return format_delete_activities(deleted, activity_ids)
    except Exception as e:
        return _tool_error("delete_vault_activity", e)


@mcp.tool(
    name="strava_sync_activities",
    annotations={
        "title": "Sync Strava activities into local vault",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def sync_activities(days_back: int = 0, ctx: Context | None = None) -> str:
    """Sync Strava activities into the local vault.

    Smart sync behavior:
    - First run (empty vault): pulls ALL historical activities
    - Subsequent runs: only fetches activities newer than the latest stored
    - With days_back > 0: fetches a specific time window (useful for refreshing)

    Activities are stored permanently in the vault. No data expires.
    Typically takes 1-3 API calls for a full sync (~200 activities).

    Args:
        days_back: 0 = auto (incremental or full). >0 = fetch last N days (max 3650).
    """
    if days_back < 0 or days_back > 3650:
        return "days_back must be between 0 and 3650 (10 years)."
    try:
        result = await manager.sync_activities(days_back, ctx=ctx)
        return format_sync_result(result)
    except Exception as e:
        return _tool_error("sync_activities", e)


if __name__ == "__main__":
    import uvicorn

    from auth import maybe_add_auth, maybe_add_origin_check

    # Streamable HTTP transport (MCP spec 2025-06-18). Replaces the
    # deprecated HTTP+SSE transport from 2024-11-05. Single /mcp endpoint
    # that serves POST (client -> server) and GET (server -> client SSE
    # stream) on the same path.
    app = mcp.streamable_http_app()
    app = maybe_add_auth(app)
    app = maybe_add_origin_check(app)
    uvicorn.run(app, host="0.0.0.0", port=port)
