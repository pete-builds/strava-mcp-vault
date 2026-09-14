import json
import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from mcp.server import MCPServer

from strava_mcp_vault.cache.db import CacheDB
from strava_mcp_vault.cache.geocode import forward_geocode, reverse_geocode_many
from strava_mcp_vault.cache.manager import CacheManager
from strava_mcp_vault.clients.strava import StravaClient
from strava_mcp_vault.exceptions import VaultError
from strava_mcp_vault.formatters import (
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
from strava_mcp_vault.photos import pick_for_spot, strip_photo
from strava_mcp_vault.spots import (
    DEFAULT_SPORT_TYPES,
    assign_to_spots,
    build_export,
    format_export,
    format_spot_list,
    is_public,
)
from strava_mcp_vault.tracks import DEFAULT_TOLERANCE_M, ClipError, build_track

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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

# Bind address for the HTTP transport. This value is load-bearing twice over:
# once for uvicorn, and once for MCP transport security (see build_app).
HTTP_HOST = "0.0.0.0"

mcp = MCPServer("strava-vault", lifespan=lifespan)

# --- Tool annotations ---
# Nothing in an MCP manifest distinguishes delete_vault_activity from
# get_activity unless the tool says so, so a client has no basis on which to
# prompt before a destructive call.
#
# openWorldHint is set per tool rather than uniformly, because it genuinely
# varies here: the vault is a LOCAL store, so most reads never leave the host,
# while the Strava-backed tools do. Marking everything open-world would be the
# easy uniform answer and would misdescribe most of the surface.

#: Reads the local vault. Never leaves this host.
READ_LOCAL = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

#: Reads via the Strava API, so an answer can change between identical calls.
READ_REMOTE = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

#: Sets a value in the local vault. Applying the same location twice lands in
#: the same place, so a retry is safe.
WRITE_IDEMPOTENT = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

#: Pulls from Strava into the vault. Idempotent by design -- it upserts, so
#: running it twice converges rather than duplicating -- but it reaches out.
SYNC = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

#: Removes activities from the vault. The call worth confirming.
#:
#: Worth being precise about the blast radius rather than overstating it: this
#: deletes from the LOCAL vault only and never touches Strava, and
#: sync_activities can re-pull anything still within the sync window. What it
#: cannot restore is an activity older than that window, or a manual location
#: set through set_activity_location. So it is recoverable in the common case
#: and not in the interesting one, which is exactly when a confirmation is
#: worth having.
DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}


@mcp.tool(annotations=READ_REMOTE)
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
            count,
            sport_type=sport_type,
            after=after,
            before=before,
        )
        if compact:
            return format_recent_activities_compact(results)
        return format_recent_activities(results)
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in get_recent_activities")
        return f"Unexpected error: {type(e).__name__}: {e}"


@mcp.tool(annotations=READ_LOCAL)
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
            sport_type=sport_type,
            after=after,
            before=before,
        )
        return format_vault_query(result)
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in query_vault")
        return f"Unexpected error: {type(e).__name__}: {e}"


@mcp.tool(annotations=READ_REMOTE)
async def get_activity(activity_id: int) -> str:
    """Get full details for a specific Strava activity.

    Args:
        activity_id: The Strava activity ID.
    """
    try:
        result = await manager.get_activity(activity_id)
        return format_activity_detail(result)
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in get_activity")
        return f"Unexpected error: {type(e).__name__}: {e}"


@mcp.tool(annotations=READ_REMOTE)
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
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in get_activity_streams")
        return f"Unexpected error: {type(e).__name__}: {e}"


@mcp.tool(annotations=READ_REMOTE)
async def get_athlete_profile() -> str:
    """Get the authenticated Strava athlete's profile."""
    try:
        result = await manager.get_athlete_profile()
        return format_athlete_profile(result)
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in get_athlete_profile")
        return f"Unexpected error: {type(e).__name__}: {e}"


@mcp.tool(annotations=READ_REMOTE)
async def get_athlete_stats() -> str:
    """Get year-to-date and all-time activity statistics."""
    try:
        result = await manager.get_athlete_stats()
        return format_athlete_stats(result)
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in get_athlete_stats")
        return f"Unexpected error: {type(e).__name__}: {e}"


def _validate_radius_miles(radius_miles: float) -> str | None:
    if radius_miles <= 0:
        return "radius_miles must be greater than 0."
    if radius_miles > 250:
        return "radius_miles is too large. Use 250 miles or less."
    return None


@mcp.tool(annotations=READ_LOCAL)
async def get_cache_stats() -> str:
    """Show cache hit/miss rates, stored items, and API rate limit status."""
    stats = await manager.get_cache_stats()
    return format_cache_stats(stats)


@mcp.tool(annotations=READ_LOCAL)
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
        lat,
        lon,
        radius_miles=radius_miles,
        sport_type=sport_type,
        after=after,
        before=before,
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


@mcp.tool(annotations=WRITE_IDEMPOTENT)
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
        return f'✅ Location for activity {activity_id} set to "{location}".'
    return f"✅ Location override cleared for activity {activity_id}."


@mcp.tool(annotations=DESTRUCTIVE)
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


@mcp.tool(annotations=WRITE_IDEMPOTENT)
async def set_ride_spot(
    name: str,
    lat: float,
    lon: float,
    radius_miles: float = 1.0,
    blurb: str | None = None,
    remove: bool = False,
) -> str:
    """Name a riding location so it becomes eligible for the public export.

    export_ride_spots publishes ONLY the spots curated here. Nothing is named
    automatically, because the largest cluster of ride start points in this vault
    is a neighborhood rather than a trailhead, and an auto-naming export would
    publish a home address first. Naming a spot is therefore a deliberate act.

    Pick the coordinate of the trailhead or parking area you want a map pin on,
    not the coordinate of a ride start. The pin published for a spot is this
    coordinate, and no recorded GPS start point is ever exported.

    Stored in its own table, so sync_activities cannot wipe it the way it wipes
    set_activity_location overrides. Returns a confirmation line.

    Args:
        name: Display name for the spot (e.g. "Shindagin Hollow").
        lat: Latitude of the map pin, in decimal degrees.
        lon: Longitude of the map pin, in decimal degrees.
        radius_miles: Rides starting within this distance belong to the spot (default 1.0).
        blurb: Optional one-line description for the page.
        remove: If true, delete this spot instead of creating or updating it.
    """
    name = (name or "").strip()
    if not name:
        return "A spot name is required, e.g. 'Shindagin Hollow'."

    if remove:
        deleted = await manager.db.delete_ride_spot(name)
        if not deleted:
            return f'No ride spot named "{name}".'
        return f'✅ Removed ride spot "{name}". It will no longer be exported.'

    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return f"Coordinates out of range: lat={lat}, lon={lon}."
    if radius_miles <= 0 or radius_miles > 25:
        return "radius_miles must be greater than 0 and 25 or less."

    await manager.db.upsert_ride_spot(name, lat, lon, radius_miles, blurb)
    return f'✅ Ride spot "{name}" set at ({lat:.4f}, {lon:.4f}), radius {radius_miles} mi.'


@mcp.tool(annotations=READ_LOCAL)
async def list_ride_spots() -> str:
    """Show the curated ride spots that the public export is allowed to publish."""
    spots = await manager.db.get_ride_spots()
    return format_spot_list(spots)


@mcp.tool(annotations=READ_LOCAL)
async def export_ride_spots(
    sport_types: str = "Ride,MountainBikeRide,GravelRide",
    after: str | None = None,
    before: str | None = None,
) -> str:
    """Export curated ride spots as JSON for a public web page to render.

    Returns a JSON string: `generated_at`, the `filters` applied, a `privacy`
    block, `totals`, an `unassigned_rides` count, and a `spots` array. Each spot
    carries name, slug, pin lat/lon, ride count, total miles, total elevation,
    first and last ride dates, a sport-type breakdown, and an `activities` array
    whose entries hold id, name, date, distance, elevation and the encoded
    `polyline` for drawing the trace.

    Three guarantees this export makes, because its output is meant to be public:
    only activities Strava marks visible to everyone are included, and anything
    with a missing or unrecognized visibility is withheld; only spots named via
    set_ride_spot appear, with no automatic fallback naming; and no recorded start
    coordinate is ever emitted, only route polylines plus the curated pin. Rides
    matching no curated spot are reported as a bare count.

    Reads the local vault only. Makes no Strava API calls.

    Args:
        sport_types: Comma-separated activity types to include.
        after: Only rides on or after this date (ISO format, e.g. "2025-01-01").
        before: Only rides before this date (ISO format, e.g. "2026-01-01").
    """
    types = [t.strip() for t in (sport_types or "").split(",") if t.strip()]
    if not types:
        return "At least one sport_type is required, e.g. 'Ride,MountainBikeRide,GravelRide'."

    spots = await manager.db.get_ride_spots()
    if not spots:
        return (
            "No ride spots curated yet, so there is nothing publishable to export.\n"
            "Name at least one with set_ride_spot first."
        )

    activities = await manager.db.get_public_activities_with_geometry(
        types, after=after, before=before
    )
    payload = build_export(activities, spots, types, after=after, before=before)
    return format_export(payload)


@mcp.tool(annotations=READ_REMOTE)
async def get_route_track(
    activity_id: int,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    precision: int = 5,
    include_time: bool = False,
) -> str:
    """Full-resolution route geometry for one activity, safe to publish as GPX.

    Returns a JSON string: `points` as [[lat, lon, elevation_m], ...] (a fourth
    element carries seconds-from-start when include_time is set), plus
    `raw_points`, `published_points`, `trimmed_head`, `trimmed_tail` and
    `tolerance_m` so a caller can see exactly how much was cut and why.

    The track is CLIPPED to the activity's summary_polyline before anything is
    returned. Strava privacy-zone trims the polyline it publishes but does not
    trim the underlying latlng stream, so an unclipped stream would republish the
    exact start point Strava was hiding. Where the recording cannot be matched to
    its polyline this errors rather than guessing, because a track nobody can
    bound is one that may expose a trimmed start.

    Only activities marked visible to everyone are served. Reaches Strava the
    first time and is then cached for 7 days.

    Args:
        activity_id: The Strava activity ID.
        tolerance_m: Douglas-Peucker simplification in metres (default 5, which is inside GPS noise).
        precision: Decimal places for coordinates (default 5, about 1.1 m).
        include_time: Include seconds-from-start on each point.
    """
    try:
        row = await manager.db.get_activity_row(activity_id)
        if row is None:
            return f"Activity {activity_id} is not in the vault."
        if not is_public(row):
            return f"Activity {activity_id} is not visible to everyone, so it is not publishable."

        polyline = (row.get("map") or {}).get("summary_polyline") or ""
        if not polyline:
            return f"Activity {activity_id} has no route geometry."

        streams = await manager.get_activity_streams(activity_id, "latlng,altitude,time")
        track = build_track(
            streams,
            polyline,
            tolerance_m=tolerance_m,
            precision=precision,
            include_time=include_time,
        )
        track["activity_id"] = activity_id
        track["name"] = row.get("name") or ""
        track["sport_type"] = row.get("sport_type") or row.get("type") or ""
        track["start_date_local"] = row.get("start_date_local") or ""
        return json.dumps(track)
    except ClipError as e:
        return f"Refusing to emit a track for {activity_id}: {e}"
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in get_route_track")
        return f"Unexpected error: {type(e).__name__}: {e}"


@mcp.tool(annotations=READ_REMOTE)
async def export_spot_photos(size: int = 2048, per_spot: int = 1) -> str:
    """Pick a photo for each curated ride spot, for a public page to self-host.

    Returns a JSON string: one entry per spot that has any, each carrying the
    spot name and slug plus `photos` with `url`, `caption`, `activity_id`,
    `activity_name`, `created_at_local` and pixel `width`/`height`.

    The URLs are Strava's own CDN renditions and rotate, so download them and
    serve your own copies rather than hotlinking. A photo's EXIF `location` is
    never returned: Strava sends a full-precision coordinate with every photo,
    which is the shutter position rather than the trimmed route, and on a photo
    taken before setting off that is the athlete's front door.

    Only activities visible to everyone are considered. Reaches Strava once per
    activity inspected and caches for 7 days, so the first call on a cold cache
    is slow and later ones are not.

    Args:
        size: Pixel rendition to request from Strava (default 2048).
        per_spot: How many photos to return for each spot (default 1).
    """
    try:
        spots = await manager.db.get_ride_spots()
        if not spots:
            return "No ride spots curated yet. Name one with set_ride_spot first."

        types = list(DEFAULT_SPORT_TYPES)
        activities = await manager.db.get_public_activities_with_geometry(types)
        buckets, _ = assign_to_spots(activities, spots)

        out = []
        for spot in spots:
            members = [
                a for a in buckets.get(spot["name"], []) if (a.get("total_photo_count") or 0) > 0
            ]
            members.sort(key=lambda a: a.get("start_date_local") or "", reverse=True)

            collected: list[dict] = []
            for activity in members:
                if len(collected) >= per_spot:
                    break
                raw = await manager.get_activity_photos(activity["id"], size=size)
                for photo in raw:
                    shaped = strip_photo(photo, size)
                    if shaped:
                        collected.append(shaped)
            if collected:
                out.append(
                    {
                        "name": spot["name"],
                        "slug": spot["name"].lower().replace(" ", "-"),
                        "photos": pick_for_spot(collected, per_spot),
                    }
                )

        return json.dumps(
            {"size": size, "per_spot": per_spot, "spots": out, "spots_with_photos": len(out)},
            indent=2,
        )
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in export_spot_photos")
        return f"Unexpected error: {type(e).__name__}: {e}"


@mcp.tool(annotations=SYNC)
async def sync_route_tracks(limit: int = 80) -> str:
    """Warm the stream cache for publishable rides, one rate-limited batch.

    Returns a plain report of how many were fetched, how many were already
    cached, how many were refused by the clipping guard, and the Strava rate
    limit headroom left afterwards.

    Strava allows 100 requests per 15 minutes, so the default stops at 80 and
    leaves room for everything else this server does. Run it repeatedly until it
    reports nothing left. Streams cache for 7 days, so a completed backfill stays
    warm for a week.

    Args:
        limit: Maximum activities to fetch this run (default 80).
    """
    try:
        spots = await manager.db.get_ride_spots()
        activities = await manager.db.get_public_activities_with_geometry(list(DEFAULT_SPORT_TYPES))
        if spots:
            buckets, _ = assign_to_spots(activities, spots)
            activities = [a for members in buckets.values() for a in members]

        fetched = cached = refused = 0
        remaining = 0
        for activity in activities:
            key = manager.stream_cache_key(activity["id"], "latlng,altitude,time")
            if await manager.db.get_cached(key) is not None:
                cached += 1
                continue
            if fetched >= limit:
                remaining += 1
                continue
            try:
                streams = await manager.get_activity_streams(activity["id"], "latlng,altitude,time")
                fetched += 1
                build_track(streams, (activity.get("map") or {}).get("summary_polyline") or "")
            except ClipError:
                refused += 1
            except Exception:
                logger.exception("stream fetch failed for %s", activity["id"])

        stats = await manager.get_cache_stats()
        rate = stats.get("rate_limit", {})
        return (
            f"Fetched {fetched}, already cached {cached}, refused by the clipping guard {refused}, "
            f"still to do {remaining}.\nRate limit: {rate}"
        )
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in sync_route_tracks")
        return f"Unexpected error: {type(e).__name__}: {e}"


@mcp.tool(annotations=SYNC)
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
    except VaultError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.exception("Unexpected error in sync_activities")
        return f"Unexpected error: {type(e).__name__}: {e}"


def build_app():
    """Build the Streamable HTTP ASGI app, with bearer auth if configured.

    Streamable HTTP transport (MCP spec 2025-06-18). Replaces the deprecated
    HTTP+SSE transport from 2024-11-05. Single /mcp endpoint that serves POST
    (client -> server) and GET (server -> client SSE stream) on the same path.

    Passing host=HTTP_HOST is REQUIRED, not redundant with the uvicorn bind
    below. Since mcp 2.0, streamable_http_app() auto-enables DNS-rebinding
    protection whenever it is given a loopback host (its default is
    "127.0.0.1"), which makes the server answer HTTP 421 "Invalid Host header"
    to every request that does not arrive as localhost. This server is reached
    over the LAN and Tailscale, so that would break every real client while
    still looking healthy from the box itself.

    Do not "simplify" this to streamable_http_app().
    See tests/test_transport_host.py, which fails if this argument is dropped.
    """
    from strava_mcp_vault.auth import maybe_add_auth

    return maybe_add_auth(mcp.streamable_http_app(host=HTTP_HOST))


def main() -> None:
    """Serve the MCP app over streamable HTTP. Console-script entry point."""
    import uvicorn

    uvicorn.run(build_app(), host=HTTP_HOST, port=port)


if __name__ == "__main__":
    main()
