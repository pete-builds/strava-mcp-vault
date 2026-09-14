"""Ride-spot curation and public geometry export.

This module exists to feed a PUBLIC web page, which makes it the one place in
this codebase where the default has to be "publish nothing" rather than
"publish what we have."

Two decisions here are load-bearing and deliberately not configurable:

1. **Allowlist only.** A spot is exported only if someone named it and stored it
   in `ride_spots`. There is no geocoded fallback name, because a fallback is
   what turns "I forgot to curate this cluster" into "my home address is on the
   internet." Measured on the real vault on 2026-09-13, the single largest
   cluster of bike activities (59 rides, 49 of them road rides) starts in Pete's
   neighborhood, not at a trailhead: it outranks Shindagin Hollow, which is
   third. Any design that auto-names clusters publishes that one first.

2. **Unknown visibility is not public.** The filter is an explicit allowlist of
   Strava's `visibility == "everyone"`. An activity whose visibility is missing,
   null, or any other value is withheld. Strava has changed this field's shape
   before, and a future value we have never seen must fail closed.

`start_latlng` is NOT exported. Strava's own privacy-zone trimming is applied
inconsistently: across the 256 activities in the vault that carry geometry, the
gap between `start_latlng` and the first point of `summary_polyline` has a
median of 344 ft, but 95 of them sit within 50 ft. So the polyline is sometimes
trimmed and sometimes not, while `start_latlng` is always the raw recorded
start. Only the polyline goes out, and the pin a page draws comes from the
curated spot coordinate rather than from any recorded GPS point.
"""

import json
import math
import re
from datetime import UTC, datetime

#: Strava visibility value that means "anyone can see this activity."
#: Anything else, including a missing field, is withheld from the export.
PUBLIC_VISIBILITY = "everyone"

#: Activity types that count as riding for this export.
DEFAULT_SPORT_TYPES = ("Ride", "MountainBikeRide", "GravelRide")

_METERS_PER_MILE = 1609.344
_FEET_PER_METER = 3.280839895


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two decimal-degree points."""
    r = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def slugify(name: str) -> str:
    """Turn a spot name into a URL-safe slug ("Shindagin Hollow" -> "shindagin-hollow")."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower())
    return slug.strip("-")


def is_public(activity: dict) -> bool:
    """True only when Strava explicitly marks this activity visible to everyone.

    Fails closed on purpose. A missing or unrecognized `visibility`, or a truthy
    `private` flag, means "do not publish."
    """
    if activity.get("private"):
        return False
    return activity.get("visibility") == PUBLIC_VISIBILITY


def _polyline(activity: dict) -> str:
    return (activity.get("map") or {}).get("summary_polyline") or ""


def _start_point(activity: dict) -> tuple[float, float] | None:
    latlng = activity.get("start_latlng")
    if isinstance(latlng, list) and len(latlng) >= 2:
        lat, lon = latlng[0], latlng[1]
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return float(lat), float(lon)
    return None


def assign_to_spots(activities: list[dict], spots: list[dict]) -> tuple[dict, int]:
    """Bucket activities into curated spots by start-point proximity.

    Returns (buckets, unassigned_count). `buckets` maps spot name to the list of
    activities that started within that spot's radius. An activity is assigned to
    the NEAREST matching spot, so overlapping radii do not double-count it.
    Activities matching no spot are counted, never returned: their coordinates
    are exactly what must not leak.
    """
    buckets: dict[str, list[dict]] = {s["name"]: [] for s in spots}
    unassigned = 0

    for activity in activities:
        point = _start_point(activity)
        if point is None:
            unassigned += 1
            continue
        lat, lon = point

        best_name = None
        best_distance = None
        for spot in spots:
            distance = haversine_miles(lat, lon, spot["lat"], spot["lon"])
            if distance <= spot["radius_miles"] and (
                best_distance is None or distance < best_distance
            ):
                best_name, best_distance = spot["name"], distance

        if best_name is None:
            unassigned += 1
        else:
            buckets[best_name].append(activity)

    return buckets, unassigned


def _activity_row(activity: dict) -> dict:
    """Shape one activity for the public export. Polyline only, never start_latlng."""
    distance_m = activity.get("distance") or 0
    elevation_m = activity.get("total_elevation_gain") or 0
    start_local = activity.get("start_date_local") or ""
    return {
        "id": activity.get("id"),
        "name": activity.get("name") or "",
        "date": start_local[:10],
        "sport_type": activity.get("sport_type") or activity.get("type") or "",
        "distance_miles": round(distance_m / _METERS_PER_MILE, 2),
        "elevation_ft": round(elevation_m * _FEET_PER_METER),
        "moving_time_s": activity.get("moving_time") or 0,
        "polyline": _polyline(activity),
    }


def _spot_summary(spot: dict, activities: list[dict]) -> dict:
    rows = [_activity_row(a) for a in activities]
    rows.sort(key=lambda r: r["date"], reverse=True)

    sport_counts: dict[str, int] = {}
    for row in rows:
        sport_counts[row["sport_type"]] = sport_counts.get(row["sport_type"], 0) + 1

    dates = [r["date"] for r in rows if r["date"]]
    return {
        "name": spot["name"],
        "slug": slugify(spot["name"]),
        # The pin comes from the curated coordinate, never from a recorded GPS
        # point, so the map never publishes where a ride actually started.
        "lat": spot["lat"],
        "lon": spot["lon"],
        "radius_miles": spot["radius_miles"],
        "blurb": spot.get("blurb") or "",
        "ride_count": len(rows),
        "total_miles": round(sum(r["distance_miles"] for r in rows), 1),
        "total_elevation_ft": sum(r["elevation_ft"] for r in rows),
        "first_ride": min(dates) if dates else "",
        "last_ride": max(dates) if dates else "",
        "sport_types": dict(sorted(sport_counts.items(), key=lambda kv: -kv[1])),
        "activities": rows,
    }


def build_export(
    activities: list[dict],
    spots: list[dict],
    sport_types: list[str],
    after: str | None = None,
    before: str | None = None,
) -> dict:
    """Build the public ride-spot export payload.

    Applies the public-visibility filter here rather than trusting the caller to
    have done it in SQL, so the guarantee holds no matter which path reaches this
    function.
    """
    public, withheld = [], 0
    for activity in activities:
        if is_public(activity):
            public.append(activity)
        else:
            withheld += 1

    with_geometry = [a for a in public if _polyline(a)]
    buckets, unassigned = assign_to_spots(with_geometry, spots)

    spot_payloads = [
        _spot_summary(spot, buckets[spot["name"]]) for spot in spots if buckets[spot["name"]]
    ]
    spot_payloads.sort(key=lambda s: -s["ride_count"])

    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "filters": {
            "sport_types": list(sport_types),
            "after": after,
            "before": before,
        },
        "privacy": {
            "allowlist_only": True,
            "visibility_filter": PUBLIC_VISIBILITY,
            "withheld_not_public": withheld,
            "start_coordinates_exported": False,
        },
        "totals": {
            "spots": len(spot_payloads),
            "rides": sum(s["ride_count"] for s in spot_payloads),
            "miles": round(sum(s["total_miles"] for s in spot_payloads), 1),
            "elevation_ft": sum(s["total_elevation_ft"] for s in spot_payloads),
        },
        # Rides that matched no curated spot. A count only: their start points are
        # precisely the data this export exists to keep off a public page.
        "unassigned_rides": unassigned,
        "spots": spot_payloads,
    }


def format_export(payload: dict) -> str:
    """Serialize the export payload as indented JSON for a site build to consume."""
    return json.dumps(payload, indent=2)


def format_spot_list(spots: list[dict]) -> str:
    """Human-readable summary of the curated allowlist."""
    if not spots:
        return (
            "No ride spots curated yet, so export_ride_spots would return nothing.\n"
            "Add one with set_ride_spot, e.g. "
            'set_ride_spot(name="Shindagin Hollow", lat=42.3451, lon=-76.3505).'
        )
    lines = [
        f"## Curated Ride Spots ({len(spots)})",
        "",
        "| Spot | Lat | Lon | Radius |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {s['name']} | {s['lat']:.4f} | {s['lon']:.4f} | {s['radius_miles']} mi |" for s in spots
    ]
    return "\n".join(lines)
