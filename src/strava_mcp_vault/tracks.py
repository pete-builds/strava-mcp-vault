"""Full-resolution route geometry, clipped to what Strava already published.

THE CLIPPING IS THE POINT OF THIS MODULE. Read this before changing anything in it.

Strava applies privacy-zone trimming to `map.summary_polyline`: the encoded line
it hands out starts some distance along the real route, hiding where the athlete
actually set off. It does NOT apply that trimming to the `latlng` stream, which
is the raw recording from the first GPS fix.

Measured across the 256 activities in this vault that carry geometry, the gap
between `start_latlng` and the first point of `summary_polyline` has a median of
344 ft and a p90 of 954 ft, while 95 of 256 sit within 50 ft. So the trimming is
real, it is substantial, and it is applied inconsistently enough that you cannot
tell from one activity whether it is in effect.

The consequence: emitting a raw stream on a site that currently publishes
polylines would silently republish the exact start points Strava was hiding, on
routes that look perfectly safe today, with no visible change to anything. A
correctly clipped track and an unclipped one are indistinguishable without a
check, which is why `clip_to_polyline` fails closed rather than returning its
best effort.

The polyline is therefore the AUTHORITY for where a route may begin and end. The
stream only supplies resolution and elevation in between.
"""

import math

#: How close a clipped endpoint must land to the polyline's own endpoint, in
#: metres, before the result is trusted. The polyline is a decimated subset of
#: the same recording, so a correct clip lands within a few metres; this is
#: loose enough for decimation error and far tighter than any privacy zone.
ENDPOINT_TOLERANCE_M = 40.0

#: Douglas-Peucker tolerance. Phone GPS noise is 3 to 5 m, so simplifying at 5 m
#: removes jitter rather than shape, and takes 175 rides from roughly 87 MB of
#: GPX to roughly 10 MB.
DEFAULT_TOLERANCE_M = 5.0

_EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return _EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decode a Google-encoded polyline into [(lat, lon), ...]."""
    points: list[tuple[float, float]] = []
    index = 0
    lat = 0
    lon = 0
    while index < len(encoded):
        for axis in range(2):
            shift = 0
            result = 0
            while True:
                if index >= len(encoded):
                    return points
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        points.append((lat / 1e5, lon / 1e5))
    return points


def _first_index_within(
    points: list[tuple[float, float]], target: tuple[float, float], tolerance_m: float
) -> int | None:
    """Index of the point that best matches `target`, searching from the start.

    Two stages on purpose. The tolerance gate finds the right PART of the route,
    which is what stops an out-and-back matching its own return leg. Then it
    walks forward to the local closest point, because stopping at the first
    merely-within-tolerance sample keeps up to a tolerance-width of the stretch
    Strava was hiding: at 22 m sampling and a 40 m gate that was a whole point of
    the trimmed lead-in, published.
    """
    for i, (lat, lon) in enumerate(points):
        if haversine_m(lat, lon, target[0], target[1]) <= tolerance_m:
            best = i
            best_d = haversine_m(lat, lon, target[0], target[1])
            j = i + 1
            while j < len(points):
                d = haversine_m(points[j][0], points[j][1], target[0], target[1])
                if d > best_d:
                    break
                best, best_d = j, d
                j += 1
            return best
    return None


def _last_index_within(
    points: list[tuple[float, float]], target: tuple[float, float], tolerance_m: float
) -> int | None:
    """Index best matching `target`, searching from the end. See above."""
    for i in range(len(points) - 1, -1, -1):
        lat, lon = points[i]
        if haversine_m(lat, lon, target[0], target[1]) <= tolerance_m:
            best = i
            best_d = haversine_m(lat, lon, target[0], target[1])
            j = i - 1
            while j >= 0:
                d = haversine_m(points[j][0], points[j][1], target[0], target[1])
                if d > best_d:
                    break
                best, best_d = j, d
                j -= 1
            return best
    return None


def normalize_streams(streams) -> dict[str, list]:
    """Flatten a Strava streams response to {type: [values]}.

    The endpoint answers in TWO shapes and which one you get is not obvious from
    the docs: a list of {"type": ..., "data": [...]} objects, which is what the
    live API actually returned for `key_type=time`, or a dict keyed by type
    whose values are either the {"data": [...]} envelope or the bare list.

    This is the single normaliser. A second copy of it lived inline in
    `format_activity_streams`, and `build_track` was written against the dict
    shape alone from a fixture: every unit test passed and the first live call
    died with "'list' object has no attribute 'get'". Fixtures cannot tell you
    the shape of an upstream you have not probed.
    """
    if isinstance(streams, list):
        out: dict[str, list] = {}
        for entry in streams:
            if isinstance(entry, dict) and "type" in entry:
                out[entry["type"]] = entry.get("data") or []
        return out

    if isinstance(streams, dict):
        out = {}
        for key, value in streams.items():
            if isinstance(value, dict) and "data" in value:
                out[key] = value["data"] or []
            elif isinstance(value, list):
                out[key] = value
        return out

    return {}


class ClipError(ValueError):
    """Raised when a stream cannot be clipped to its polyline with confidence."""


def clip_to_polyline(
    stream_points: list[tuple[float, float]],
    polyline_points: list[tuple[float, float]],
    tolerance_m: float = ENDPOINT_TOLERANCE_M,
) -> tuple[int, int]:
    """Return the [start, end] stream slice that matches the polyline's extent.

    Walks IN from each end rather than searching for the nearest point, because
    an out-and-back or a loop puts the finish near the start, and a nearest-point
    search on such a route can select an index from the wrong lap and silently
    return a fragment.

    Fails closed. If neither end can be matched to the polyline within tolerance,
    the honest answer is that we do not know how much of this recording Strava
    was hiding, and the caller must not publish it.
    """
    if not polyline_points:
        raise ClipError("activity has no summary_polyline to clip against")
    if not stream_points:
        raise ClipError("activity has no latlng stream")

    start = _first_index_within(stream_points, polyline_points[0], tolerance_m)
    if start is None:
        raise ClipError(
            "no stream point lies within "
            f"{tolerance_m:.0f} m of the polyline start; refusing to guess how "
            "much of the recording Strava trimmed"
        )

    end = _last_index_within(stream_points, polyline_points[-1], tolerance_m)
    if end is None:
        raise ClipError(f"no stream point lies within {tolerance_m:.0f} m of the polyline end")

    if end <= start:
        raise ClipError("clipped range is empty or inverted")
    return start, end


def _perpendicular_distance_m(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    """Distance from `point` to the segment start-end, in metres.

    Works in a local flat projection: over the length of one simplification
    segment the error is far below the tolerances in use here.
    """
    lat_scale = 111320.0
    lon_scale = 111320.0 * math.cos(math.radians(start[0]))

    px = (point[1] - start[1]) * lon_scale
    py = (point[0] - start[0]) * lat_scale
    ex = (end[1] - start[1]) * lon_scale
    ey = (end[0] - start[0]) * lat_scale

    seg_len_sq = ex * ex + ey * ey
    if seg_len_sq == 0:
        return math.hypot(px, py)

    t = max(0.0, min(1.0, (px * ex + py * ey) / seg_len_sq))
    return math.hypot(px - t * ex, py - t * ey)


def simplify(points: list[tuple], tolerance_m: float) -> list[tuple]:
    """Douglas-Peucker on (lat, lon, ...) tuples, preserving both endpoints.

    Iterative rather than recursive: a 20,000 point stream would otherwise be
    able to exceed Python's recursion limit on a pathological route.
    """
    if tolerance_m <= 0 or len(points) < 3:
        return list(points)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        worst = 0.0
        worst_i = first
        for i in range(first + 1, last):
            d = _perpendicular_distance_m(points[i][:2], points[first][:2], points[last][:2])
            if d > worst:
                worst = d
                worst_i = i
        if worst > tolerance_m:
            keep[worst_i] = True
            stack.append((first, worst_i))
            stack.append((worst_i, last))

    return [p for p, k in zip(points, keep) if k]


def build_track(
    streams: dict,
    summary_polyline: str,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    precision: int = 5,
    include_time: bool = False,
) -> dict:
    """Clip, simplify and round a stream into a publishable track.

    Returns a dict with `points` ([[lat, lon, ele] or [lat, lon, ele, t]]),
    the counts before and after, and the clipping that was applied. Raises
    ClipError when the recording cannot be clipped to its polyline, because a
    track we cannot bound is a track that may expose a trimmed start.
    """
    normalized = normalize_streams(streams)
    latlng = normalized.get("latlng") or []
    altitude = normalized.get("altitude") or []
    times = normalized.get("time") or []

    stream_points = [(float(p[0]), float(p[1])) for p in latlng if isinstance(p, (list, tuple))]
    poly = decode_polyline(summary_polyline or "")

    start, end = clip_to_polyline(stream_points, poly)

    combined: list[tuple] = []
    for i in range(start, end + 1):
        lat, lon = stream_points[i]
        ele = float(altitude[i]) if i < len(altitude) else None
        if include_time and i < len(times):
            combined.append((lat, lon, ele, int(times[i])))
        else:
            combined.append((lat, lon, ele))

    simplified = simplify(combined, tolerance_m)

    points = []
    for p in simplified:
        row = [round(p[0], precision), round(p[1], precision)]
        row.append(round(p[2], 1) if p[2] is not None else None)
        if include_time and len(p) > 3:
            row.append(p[3])
        points.append(row)

    return {
        "points": points,
        "raw_points": len(stream_points),
        "clipped_points": len(combined),
        "published_points": len(points),
        "trimmed_head": start,
        "trimmed_tail": len(stream_points) - 1 - end,
        "tolerance_m": tolerance_m,
        "has_elevation": any(p[2] is not None for p in points),
    }
