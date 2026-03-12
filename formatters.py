"""Markdown formatters for MCP tool responses.

Each function takes a data dict/list (from the cache manager) and returns
a markdown string that renders cleanly in Claude Code's terminal.

Activity-type-specific formatting:
- Rides/Gravel: Speed (mph), distance, elevation, HR
- Runs: Pace (min/mi), distance, elevation, HR
- Snowboard/Ski: Number of runs (laps), elapsed time, elevation, HR
- Walks/Hikes: Distance, elapsed time, elevation
- Swims: Distance, time, pace
"""

from datetime import datetime

METERS_PER_MILE = 1609.344
METERS_PER_YARD = 0.9144

# ── Activity type classification ──────────────────────────────────────

_RIDE_TYPES = {"Ride", "VirtualRide", "MountainBikeRide", "GravelRide", "EBikeRide"}
_RUN_TYPES = {"Run", "VirtualRun", "TrailRun"}
_SNOW_TYPES = {"Snowboard", "AlpineSki", "BackcountrySki", "NordicSki", "Snowshoe"}
_WALK_TYPES = {"Walk", "Hike"}
_SWIM_TYPES = {"Swim"}


def _activity_category(sport_type: str | None) -> str:
    """Classify an activity into a formatting category."""
    st = sport_type or ""
    if st in _RIDE_TYPES:
        return "ride"
    if st in _RUN_TYPES:
        return "run"
    if st in _SNOW_TYPES:
        return "snow"
    if st in _WALK_TYPES:
        return "walk"
    if st in _SWIM_TYPES:
        return "swim"
    return "other"


# ── Unit conversion helpers ───────────────────────────────────────────


def _format_pace(speed_mps: float | None) -> str:
    """Convert m/s to min:sec per mile pace."""
    if not speed_mps or speed_mps <= 0:
        return "N/A"
    seconds_per_mile = METERS_PER_MILE / speed_mps
    minutes = int(seconds_per_mile // 60)
    secs = int(seconds_per_mile % 60)
    return f"{minutes}:{secs:02d}/mi"


def _format_swim_pace(speed_mps: float | None) -> str:
    """Convert m/s to min:sec per 100 yards."""
    if not speed_mps or speed_mps <= 0:
        return "N/A"
    seconds_per_100yd = (100 * METERS_PER_YARD) / speed_mps
    minutes = int(seconds_per_100yd // 60)
    secs = int(seconds_per_100yd % 60)
    return f"{minutes}:{secs:02d}/100yd"


def _format_speed_mph(speed_mps: float | None) -> str:
    """Convert m/s to mph."""
    if not speed_mps or speed_mps <= 0:
        return "N/A"
    mph = speed_mps * 3600 / METERS_PER_MILE
    return f"{mph:.1f} mph"


def _format_date(iso_str: str | None) -> str:
    """Convert ISO date to a readable format like 'Mar 10, 2026 7:30 AM'."""
    if not iso_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%b %-d, %Y %-I:%M %p")
    except (ValueError, TypeError):
        return iso_str


def _format_distance(miles: float | None) -> str:
    """Format distance in miles with 2 decimal places."""
    if miles is None:
        return "N/A"
    return f"{miles:.2f} mi"


def _format_distance_yards(meters: float | None) -> str:
    """Format distance in yards for swimming."""
    if meters is None:
        return "N/A"
    yards = meters / METERS_PER_YARD
    return f"{yards:.0f} yd"


def _format_elevation(meters: float | None) -> str:
    """Convert meters to feet."""
    if meters is None:
        return "N/A"
    feet = meters * 3.28084
    return f"{feet:.0f} ft"


def _format_duration(seconds) -> str:
    """Convert seconds (int or float) to H:MM:SS format."""
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}"


def _hr(value: float | None) -> str:
    """Format heart rate."""
    if value is None:
        return "N/A"
    return f"{int(value)} bpm"


def _sport_icon(sport_type: str | None) -> str:
    """Return an icon for the sport type."""
    icons = {
        "Run": "🏃",
        "Ride": "🚴",
        "Swim": "🏊",
        "Walk": "🚶",
        "Hike": "🥾",
        "WeightTraining": "🏋️",
        "Yoga": "🧘",
        "Workout": "💪",
        "VirtualRide": "🚴",
        "VirtualRun": "🏃",
        "TrailRun": "🏃",
        "MountainBikeRide": "🚵",
        "GravelRide": "🚴",
        "EBikeRide": "🚴",
        "Elliptical": "🏋️",
        "Rowing": "🚣",
        "Snowboard": "🏂",
        "AlpineSki": "⛷️",
        "BackcountrySki": "⛷️",
        "NordicSki": "⛷️",
        "Snowshoe": "🥾",
    }
    return icons.get(sport_type or "", "🏅")


# ── List view: activity-type-specific stats line ──────────────────────


def _list_stats_ride(a: dict) -> list[str]:
    """Stats line for ride activities in list view."""
    stats = []
    dist = a.get("distance")
    if dist is not None:
        stats.append(f"**📏 Distance:** {_format_distance(dist)}")
    avg_speed = a.get("average_speed")
    if avg_speed:
        stats.append(f"**🚀 Speed:** {_format_speed_mph(avg_speed)}")
    time_val = a.get("moving_time")
    if time_val:
        stats.append(f"**⏱️ Time:** {time_val}")
    elev = a.get("total_elevation_gain")
    if elev:
        stats.append(f"**⛰️ Elevation:** {_format_elevation(elev)}")
    return stats


def _list_stats_run(a: dict) -> list[str]:
    """Stats line for run activities in list view."""
    stats = []
    dist = a.get("distance")
    if dist is not None:
        stats.append(f"**📏 Distance:** {_format_distance(dist)}")
    avg_speed = a.get("average_speed")
    if avg_speed:
        stats.append(f"**👟 Pace:** {_format_pace(avg_speed)}")
    time_val = a.get("moving_time")
    if time_val:
        stats.append(f"**⏱️ Time:** {time_val}")
    elev = a.get("total_elevation_gain")
    if elev:
        stats.append(f"**⛰️ Elevation:** {_format_elevation(elev)}")
    return stats


def _list_stats_snow(a: dict) -> list[str]:
    """Stats line for snow sports in list view. Shows elapsed time (total
    time on mountain including lifts), elevation, no distance/pace."""
    stats = []
    elapsed = a.get("elapsed_time")
    if elapsed:
        stats.append(f"**⛷️ Time on Mountain:** {elapsed}")
    elev = a.get("total_elevation_gain")
    if elev:
        stats.append(f"**⛰️ Elevation:** {_format_elevation(elev)}")
    return stats


def _list_stats_walk(a: dict) -> list[str]:
    """Stats line for walks/hikes in list view."""
    stats = []
    dist = a.get("distance")
    if dist is not None:
        stats.append(f"**📏 Distance:** {_format_distance(dist)}")
    elapsed = a.get("elapsed_time")
    if elapsed:
        stats.append(f"**⏱️ Time:** {elapsed}")
    elev = a.get("total_elevation_gain")
    if elev:
        stats.append(f"**⛰️ Elevation:** {_format_elevation(elev)}")
    return stats


def _list_stats_swim(a: dict) -> list[str]:
    """Stats line for swim activities in list view."""
    stats = []
    dist = a.get("distance")
    if dist is not None:
        # Distance is already in miles from _shape_activity; convert back to meters for yards
        meters = dist * METERS_PER_MILE
        stats.append(f"**📏 Distance:** {_format_distance_yards(meters)}")
    time_val = a.get("moving_time")
    if time_val:
        stats.append(f"**⏱️ Time:** {time_val}")
    avg_speed = a.get("average_speed")
    if avg_speed:
        stats.append(f"**👟 Pace:** {_format_swim_pace(avg_speed)}")
    return stats


def _list_stats_default(a: dict) -> list[str]:
    """Stats line for unrecognized activity types."""
    stats = []
    dist = a.get("distance")
    if dist is not None:
        stats.append(f"**📏 Distance:** {_format_distance(dist)}")
    time_val = a.get("moving_time")
    if time_val:
        stats.append(f"**⏱️ Time:** {time_val}")
    elev = a.get("total_elevation_gain")
    if elev:
        stats.append(f"**⛰️ Elevation:** {_format_elevation(elev)}")
    return stats


_LIST_STATS_DISPATCH = {
    "ride": _list_stats_ride,
    "run": _list_stats_run,
    "snow": _list_stats_snow,
    "walk": _list_stats_walk,
    "swim": _list_stats_swim,
    "other": _list_stats_default,
}


# ── Tool formatters ───────────────────────────────────────────────────


def format_recent_activities(activities: list) -> str:
    """Format a list of shaped activities as a markdown activity log."""
    if not activities:
        return "No recent activities found."

    lines = [f"## 🏃 Recent Activities ({len(activities)})\n"]

    for i, a in enumerate(activities, 1):
        sport_type = a.get("sport_type") or a.get("type") or "Activity"
        icon = _sport_icon(sport_type)
        name = a.get("name", "Untitled")
        date = _format_date(a.get("start_date_local"))
        category = _activity_category(sport_type)

        lines.append(f"### {icon} {name}")

        # Header line: sport type, date, and location
        header = f"**{sport_type}** | {date}"
        location = a.get("location")
        if location:
            header += f" | {location}"
        lines.append(header + "\n")

        # Activity-type-specific stats
        stats_fn = _LIST_STATS_DISPATCH.get(category, _list_stats_default)
        stats = stats_fn(a)
        if stats:
            lines.append(" | ".join(stats))

        # Heart rate row (universal)
        hr_parts = []
        avg_hr = a.get("average_heartrate")
        max_hr = a.get("max_heartrate")
        cals = a.get("calories")
        if avg_hr:
            hr_parts.append(f"**❤️ Avg HR:** {_hr(avg_hr)}")
        if max_hr:
            hr_parts.append(f"**💓 Max HR:** {_hr(max_hr)}")
        if cals:
            hr_parts.append(f"**🔥 Calories:** {int(cals)}")
        if hr_parts:
            lines.append(" | ".join(hr_parts))

        # Effort and social metadata row
        meta_parts = []
        suffer = a.get("suffer_score")
        if suffer:
            meta_parts.append(f"**💪 Effort:** {int(suffer)}")
        kudos = a.get("kudos_count")
        if kudos:
            meta_parts.append(f"**👍 Kudos:** {kudos}")
        achievements = a.get("achievement_count")
        if achievements:
            meta_parts.append(f"**🏆 PRs:** {achievements}")
        if meta_parts:
            lines.append(" | ".join(meta_parts))

        # Gear name (if resolved)
        gear_name = a.get("gear_name")
        if gear_name:
            lines.append(f"**⚙️ Gear:** {gear_name}")

        lines.append(f"*ID: {a.get('id')}*\n")

    return "\n".join(lines)


def format_recent_activities_compact(activities: list) -> str:
    """Format activities as a compact one-line-per-activity table."""
    if not activities:
        return "📭 No activities found."

    lines = [f"## 📋 Activities ({len(activities)})\n"]
    lines.append("| # | Date | Type | Name | Distance | Time | Elevation | HR |")
    lines.append("|---|------|------|------|----------|------|-----------|----|")

    for i, a in enumerate(activities, 1):
        sport_type = a.get("sport_type") or a.get("type") or "?"
        icon = _sport_icon(sport_type)
        name = a.get("name", "Untitled")
        # Truncate long names
        if len(name) > 25:
            name = name[:22] + "..."

        date_str = a.get("start_date_local", "")
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            short_date = dt.strftime("%b %-d")
        except (ValueError, TypeError, AttributeError):
            short_date = "?"

        dist = a.get("distance")
        dist_str = f"{dist:.1f}mi" if dist is not None else ""

        time_val = a.get("moving_time", "")

        elev = a.get("total_elevation_gain")
        elev_str = _format_elevation(elev) if elev else ""

        avg_hr = a.get("average_heartrate")
        hr_str = f"{int(avg_hr)}" if avg_hr else ""

        lines.append(f"| {i} | {short_date} | {icon} | {name} | {dist_str} | {time_val} | {elev_str} | {hr_str} |")

    return "\n".join(lines)


# ── Detail view: activity-type-specific formatting ────────────────────


def _detail_performance_ride(activity: dict, lines: list) -> None:
    """Add ride-specific performance stats to detail view."""
    distance_m = activity.get("distance")
    if distance_m is not None:
        miles = distance_m / METERS_PER_MILE
        lines.append(f"- **📏 Distance:** {miles:.2f} mi")

    moving_time = activity.get("moving_time")
    if moving_time is not None:
        lines.append(f"- **⏱️ Moving Time:** {_format_duration(moving_time)}")

    elapsed = activity.get("elapsed_time")
    if elapsed is not None and moving_time is not None and abs(elapsed - moving_time) > 60:
        lines.append(f"- **⏱️ Elapsed Time:** {_format_duration(elapsed)}")

    avg_speed = activity.get("average_speed")
    if avg_speed:
        lines.append(f"- **🚀 Avg Speed:** {_format_speed_mph(avg_speed)}")
    max_speed = activity.get("max_speed")
    if max_speed:
        lines.append(f"- **🚀 Max Speed:** {_format_speed_mph(max_speed)}")

    _detail_elevation(activity, lines)


def _detail_performance_run(activity: dict, lines: list) -> None:
    """Add run-specific performance stats to detail view."""
    distance_m = activity.get("distance")
    if distance_m is not None:
        miles = distance_m / METERS_PER_MILE
        lines.append(f"- **📏 Distance:** {miles:.2f} mi")

    moving_time = activity.get("moving_time")
    if moving_time is not None:
        lines.append(f"- **⏱️ Moving Time:** {_format_duration(moving_time)}")

    elapsed = activity.get("elapsed_time")
    if elapsed is not None and moving_time is not None and abs(elapsed - moving_time) > 60:
        lines.append(f"- **⏱️ Elapsed Time:** {_format_duration(elapsed)}")

    avg_speed = activity.get("average_speed")
    if avg_speed:
        lines.append(f"- **👟 Avg Pace:** {_format_pace(avg_speed)}")
    max_speed = activity.get("max_speed")
    if max_speed:
        lines.append(f"- **👟 Best Pace:** {_format_pace(max_speed)}")

    _detail_elevation(activity, lines)


def _detail_performance_snow(activity: dict, lines: list) -> None:
    """Add snow sport performance stats to detail view.
    Emphasizes runs (laps), total time on mountain, elevation."""
    # Runs (laps)
    laps = activity.get("laps")
    if laps and isinstance(laps, list):
        lines.append(f"- **🎿 Runs:** {len(laps)}")

    # Elapsed time is the key metric (includes lift time)
    elapsed = activity.get("elapsed_time")
    if elapsed is not None:
        lines.append(f"- **⛷️ Time on Mountain:** {_format_duration(elapsed)}")

    # Moving time is less relevant but still interesting
    moving_time = activity.get("moving_time")
    if moving_time is not None:
        lines.append(f"- **⏱️ Time Moving:** {_format_duration(moving_time)}")

    _detail_elevation(activity, lines)

    # Max speed is fun for snow sports
    max_speed = activity.get("max_speed")
    if max_speed:
        lines.append(f"- **🚀 Max Speed:** {_format_speed_mph(max_speed)}")


def _detail_performance_walk(activity: dict, lines: list) -> None:
    """Add walk/hike performance stats to detail view."""
    distance_m = activity.get("distance")
    if distance_m is not None:
        miles = distance_m / METERS_PER_MILE
        lines.append(f"- **📏 Distance:** {miles:.2f} mi")

    elapsed = activity.get("elapsed_time")
    if elapsed is not None:
        lines.append(f"- **⏱️ Time:** {_format_duration(elapsed)}")

    moving_time = activity.get("moving_time")
    if moving_time is not None and elapsed is not None and abs(elapsed - moving_time) > 60:
        lines.append(f"- **⏱️ Moving Time:** {_format_duration(moving_time)}")

    _detail_elevation(activity, lines)


def _detail_performance_swim(activity: dict, lines: list) -> None:
    """Add swim performance stats to detail view."""
    distance_m = activity.get("distance")
    if distance_m is not None:
        lines.append(f"- **📏 Distance:** {_format_distance_yards(distance_m)}")

    moving_time = activity.get("moving_time")
    if moving_time is not None:
        lines.append(f"- **⏱️ Time:** {_format_duration(moving_time)}")

    avg_speed = activity.get("average_speed")
    if avg_speed:
        lines.append(f"- **👟 Pace:** {_format_swim_pace(avg_speed)}")

    laps = activity.get("laps")
    if laps and isinstance(laps, list):
        lines.append(f"- **🔄 Laps:** {len(laps)}")


def _detail_performance_default(activity: dict, lines: list) -> None:
    """Add generic performance stats to detail view."""
    distance_m = activity.get("distance")
    if distance_m is not None:
        miles = distance_m / METERS_PER_MILE
        lines.append(f"- **📏 Distance:** {miles:.2f} mi")

    moving_time = activity.get("moving_time")
    if moving_time is not None:
        lines.append(f"- **⏱️ Moving Time:** {_format_duration(moving_time)}")

    elapsed = activity.get("elapsed_time")
    if elapsed is not None and moving_time is not None and abs(elapsed - moving_time) > 60:
        lines.append(f"- **⏱️ Elapsed Time:** {_format_duration(elapsed)}")

    avg_speed = activity.get("average_speed")
    max_speed = activity.get("max_speed")
    if avg_speed:
        lines.append(f"- **🚀 Avg Speed:** {_format_speed_mph(avg_speed)}")
    if max_speed:
        lines.append(f"- **🚀 Max Speed:** {_format_speed_mph(max_speed)}")

    _detail_elevation(activity, lines)


def _detail_elevation(activity: dict, lines: list) -> None:
    """Add elevation stats (shared across activity types)."""
    elev = activity.get("total_elevation_gain")
    if elev:
        lines.append(f"- **⛰️ Elevation Gain:** {_format_elevation(elev)}")

    elev_high = activity.get("elev_high")
    elev_low = activity.get("elev_low")
    if elev_high is not None and elev_low is not None:
        lines.append(
            f"- **⛰️ Elevation Range:** {_format_elevation(elev_low)} to {_format_elevation(elev_high)}"
        )


_DETAIL_DISPATCH = {
    "ride": _detail_performance_ride,
    "run": _detail_performance_run,
    "snow": _detail_performance_snow,
    "walk": _detail_performance_walk,
    "swim": _detail_performance_swim,
    "other": _detail_performance_default,
}


def format_activity_detail(activity: dict) -> str:
    """Format a single activity's full details as markdown."""
    sport_type = activity.get("sport_type") or activity.get("type") or "Activity"
    icon = _sport_icon(sport_type)
    name = activity.get("name", "Untitled")
    date = _format_date(activity.get("start_date_local"))
    category = _activity_category(sport_type)

    lines = [f"## {icon} {name}"]
    lines.append(f"**{sport_type}** | {date}\n")

    # Performance section (activity-type-specific)
    lines.append("### ⚡ Performance")
    detail_fn = _DETAIL_DISPATCH.get(category, _detail_performance_default)
    detail_fn(activity, lines)

    # Lap details for snow sports
    if category == "snow":
        laps = activity.get("laps")
        if laps and isinstance(laps, list) and len(laps) > 1:
            lines.append("\n### 🎿 Runs Breakdown")
            for i, lap in enumerate(laps, 1):
                lap_elapsed = lap.get("elapsed_time")
                lap_elev = lap.get("total_elevation_gain")
                lap_max_speed = lap.get("max_speed")
                parts = [f"**Run {i}:**"]
                if lap_elapsed is not None:
                    parts.append(_format_duration(lap_elapsed))
                if lap_elev:
                    parts.append(f"elev {_format_elevation(lap_elev)}")
                if lap_max_speed:
                    parts.append(f"max {_format_speed_mph(lap_max_speed)}")
                lines.append(" | ".join(parts))

    # Heart rate (universal)
    avg_hr = activity.get("average_heartrate")
    max_hr = activity.get("max_heartrate")
    if avg_hr or max_hr:
        lines.append("\n### ❤️ Heart Rate")
        if avg_hr:
            lines.append(f"- **❤️ Average:** {_hr(avg_hr)}")
        if max_hr:
            lines.append(f"- **💓 Max:** {_hr(max_hr)}")

    # Calories
    cals = activity.get("calories")
    if cals:
        lines.append(f"- **🔥 Calories:** {int(cals)}")

    # Description
    desc = activity.get("description")
    if desc:
        lines.append(f"\n### Description\n{desc}")

    # Gear
    gear = activity.get("gear")
    if gear and isinstance(gear, dict):
        gear_name = gear.get("name")
        if gear_name:
            lines.append(f"\n**⚙️ Gear:** {gear_name}")

    # Kudos / comments
    kudos = activity.get("kudos_count", 0)
    comments = activity.get("comment_count", 0)
    if kudos or comments:
        lines.append(f"\n**👍 Kudos:** {kudos} | **💬 Comments:** {comments}")

    lines.append(f"\n*Activity ID: {activity.get('id')}*")

    return "\n".join(lines)


def format_activity_streams(streams: dict | list, activity_id: int) -> str:
    """Format activity streams as a compact markdown summary.

    Streams can be a dict of {type: {data: [...], ...}} or a list of
    stream objects with 'type' and 'data' keys.
    """
    lines = [f"## Activity Streams (ID: {activity_id})\n"]

    # Normalize to dict form
    if isinstance(streams, list):
        stream_dict = {}
        for s in streams:
            if isinstance(s, dict) and "type" in s:
                stream_dict[s["type"]] = s.get("data", [])
        streams = stream_dict
    elif isinstance(streams, dict):
        normalized = {}
        for k, v in streams.items():
            if isinstance(v, dict) and "data" in v:
                normalized[k] = v["data"]
            elif isinstance(v, list):
                normalized[k] = v
            else:
                normalized[k] = v
        streams = normalized

    if not streams:
        lines.append("No stream data available.")
        return "\n".join(lines)

    for stream_type, data in streams.items():
        if not isinstance(data, list) or not data:
            lines.append(f"**{stream_type}:** No data")
            continue

        numeric_data = [x for x in data if isinstance(x, (int, float))]

        if numeric_data:
            min_val = min(numeric_data)
            max_val = max(numeric_data)
            avg_val = sum(numeric_data) / len(numeric_data)

            label = stream_type.replace("_", " ").title()
            unit = _stream_unit(stream_type)

            lines.append(f"### {label}")
            lines.append(f"- **Points:** {len(numeric_data)}")
            lines.append(f"- **Min:** {min_val:.1f}{unit}")
            lines.append(f"- **Max:** {max_val:.1f}{unit}")
            lines.append(f"- **Avg:** {avg_val:.1f}{unit}")
            lines.append("")
        else:
            lines.append(f"**{stream_type}:** {len(data)} data points\n")

    return "\n".join(lines)


def _stream_unit(stream_type: str) -> str:
    """Return the unit suffix for a stream type."""
    units = {
        "heartrate": " bpm",
        "altitude": " m",
        "distance": " m",
        "velocity_smooth": " m/s",
        "watts": " W",
        "cadence": " rpm",
        "temp": " °C",
        "grade_smooth": "%",
    }
    return units.get(stream_type, "")


def format_athlete_profile(profile: dict) -> str:
    """Format athlete profile as markdown."""
    first = profile.get("firstname", "")
    last = profile.get("lastname", "")
    city = profile.get("city", "")
    state = profile.get("state", "")
    country = profile.get("country", "")

    lines = [f"## 🏃 {first} {last}"]

    location_parts = [p for p in [city, state, country] if p]
    if location_parts:
        lines.append(f"**Location:** {', '.join(location_parts)}\n")

    weight = profile.get("weight")
    if weight:
        lbs = weight * 2.20462
        lines.append(f"- **Weight:** {lbs:.0f} lbs ({weight:.1f} kg)")

    ftp = profile.get("ftp")
    if ftp:
        lines.append(f"- **FTP:** {ftp} W")

    follower_count = profile.get("follower_count")
    friend_count = profile.get("friend_count")
    if follower_count is not None or friend_count is not None:
        lines.append(
            f"- **Followers:** {follower_count or 0} | **Following:** {friend_count or 0}"
        )

    premium = profile.get("premium") or profile.get("summit")
    if premium:
        lines.append("- **Strava Summit:** Active")

    lines.append(f"\n*Athlete ID: {profile.get('id')}*")

    return "\n".join(lines)


def format_athlete_stats(stats: dict) -> str:
    """Format athlete stats (YTD + all-time) as markdown."""
    lines = ["## 📈 Athlete Statistics\n"]

    sections = [
        ("recent_run_totals", "🏃 Recent Runs"),
        ("ytd_run_totals", "🏃 Year-to-Date Runs"),
        ("all_run_totals", "🏃 All-Time Runs"),
        ("recent_ride_totals", "🚴 Recent Rides"),
        ("ytd_ride_totals", "🚴 Year-to-Date Rides"),
        ("all_ride_totals", "🚴 All-Time Rides"),
        ("recent_swim_totals", "🏊 Recent Swims"),
        ("ytd_swim_totals", "🏊 Year-to-Date Swims"),
        ("all_swim_totals", "🏊 All-Time Swims"),
    ]

    for key, label in sections:
        data = stats.get(key)
        if not data:
            continue
        count = data.get("count", 0)
        if count == 0:
            continue

        distance_m = data.get("distance", 0)
        miles = distance_m / METERS_PER_MILE if distance_m else 0
        moving_time = data.get("moving_time", 0)
        elev = data.get("elevation_gain", 0)

        h = int(moving_time) // 3600
        m = (int(moving_time) % 3600) // 60

        lines.append(f"### {label}")
        lines.append(f"- **🔢 Activities:** {count}")
        lines.append(f"- **📏 Distance:** {miles:.1f} mi")
        lines.append(f"- **⏱️ Time:** {h}h {m}m")
        if elev:
            lines.append(f"- **⛰️ Elevation:** {_format_elevation(elev)}")
        lines.append("")

    biggest_ride = stats.get("biggest_ride_distance")
    if biggest_ride:
        lines.append(
            f"**🏆 Longest Ride:** {biggest_ride / METERS_PER_MILE:.1f} mi"
        )
    biggest_climb = stats.get("biggest_climb_elevation_gain")
    if biggest_climb:
        lines.append(
            f"**⛰️ Biggest Climb:** {_format_elevation(biggest_climb)}"
        )

    return "\n".join(lines)


def format_cache_stats(stats: dict) -> str:
    """Format cache + vault statistics as markdown."""
    lines = ["## 📦 Vault & Cache Statistics\n"]

    # Vault section
    vault = stats.get("vault", {})
    vault_total = vault.get("total_activities", 0)
    date_range = vault.get("date_range")
    sync_log = vault.get("sync_log")

    lines.append("### 🏛️ Vault (Permanent Storage)\n")
    lines.append(f"- **Activities in vault:** {vault_total}")

    if date_range:
        earliest = _format_date(date_range.get("earliest"))
        latest = _format_date(date_range.get("latest"))
        lines.append(f"- **Date range:** {earliest} to {latest}")

    if sync_log:
        last_sync_epoch = sync_log.get("last_sync_at")
        if last_sync_epoch:
            last_sync_dt = datetime.fromtimestamp(last_sync_epoch)
            lines.append(f"- **Last sync:** {last_sync_dt.strftime('%b %-d, %Y %-I:%M %p')}")
        mode = sync_log.get("mode", "unknown")
        lines.append(f"- **Last sync mode:** {mode}")
    else:
        lines.append("- **Last sync:** Never (run sync_activities to populate)")

    lines.append("")

    # Cache section
    total = stats.get("total_cached_items", 0)
    db_size = stats.get("db_size_bytes", 0)
    db_kb = db_size / 1024

    lines.append("### ⏳ Cache (TTL-based)\n")
    lines.append(f"- **Cached items:** {total}")
    lines.append(f"- **Database size:** {db_kb:.1f} KB\n")

    categories = stats.get("categories", {})
    if categories:
        lines.append("### 🎯 Hit/Miss by Category\n")
        lines.append("| Category | Hits | Misses | Hit Rate |")
        lines.append("|----------|------|--------|----------|")
        for cat, data in sorted(categories.items()):
            hits = data.get("hits", 0)
            misses = data.get("misses", 0)
            total_req = hits + misses
            rate = f"{(hits / total_req * 100):.0f}%" if total_req > 0 else "N/A"
            lines.append(f"| {cat} | {hits} | {misses} | {rate} |")
        lines.append("")

    rate_limit = stats.get("rate_limit")
    if rate_limit:
        lines.append("### 🚦 API Rate Limits\n")
        short = rate_limit.get("short", {})
        long_ = rate_limit.get("long", {})
        lines.append(
            f"- **15-min window:** {short.get('usage', '?')}/{short.get('limit', '?')} requests"
        )
        lines.append(
            f"- **Daily window:** {long_.get('usage', '?')}/{long_.get('limit', '?')} requests"
        )
    else:
        lines.append("*Rate limit data not yet available (no API calls made this session).*")

    return "\n".join(lines)


def format_sync_result(result: dict) -> str:
    """Format sync result as markdown."""
    mode = result.get("mode", "unknown")
    fetched = result.get("activities_fetched", 0)
    new = result.get("new_activities", 0)
    total = result.get("total_in_vault", 0)
    calls = result.get("api_calls_used", 0)
    date_range = result.get("date_range")

    mode_labels = {
        "full": "Full historical sync",
        "incremental": "Incremental sync",
    }
    # Handle window_Nd patterns
    mode_label = mode_labels.get(mode, mode)
    if mode.startswith("window_"):
        days = mode.replace("window_", "").replace("d", "")
        mode_label = f"Window sync (last {days} days)"

    lines = ["## ✅ Sync Complete\n"]
    lines.append(f"- **🔄 Mode:** {mode_label}")
    lines.append(f"- **📥 Activities fetched:** {fetched}")
    lines.append(f"- **🆕 New activities added:** {new}")
    lines.append(f"- **🏛️ Total in vault:** {total}")
    lines.append(f"- **📡 API calls used:** {calls}")

    if date_range:
        earliest = _format_date(date_range.get("earliest"))
        latest = _format_date(date_range.get("latest"))
        lines.append(f"- **📅 Vault date range:** {earliest} to {latest}")

    return "\n".join(lines)


def format_vault_query(result: dict) -> str:
    """Format vault query summary with counts and totals."""
    total = result.get("total_activities", 0)
    breakdown = result.get("breakdown_by_type", [])
    dist_m = result.get("total_distance_meters", 0)
    time_s = result.get("total_moving_time_seconds", 0)
    elev_m = result.get("total_elevation_meters", 0)
    filters = result.get("filters", {})

    # Build filter description
    filter_parts = []
    if filters.get("sport_type"):
        filter_parts.append(f"type={filters['sport_type']}")
    if filters.get("after"):
        filter_parts.append(f"after {filters['after']}")
    if filters.get("before"):
        filter_parts.append(f"before {filters['before']}")
    filter_desc = ", ".join(filter_parts) if filter_parts else "all activities"

    lines = [f"## 🔍 Vault Query Results\n"]
    lines.append(f"**Filter:** {filter_desc}")
    lines.append(f"**Total Activities:** {total}\n")

    if total == 0:
        lines.append("📭 No activities match these filters.")
        return "\n".join(lines)

    # Totals
    miles = dist_m / METERS_PER_MILE
    hours = time_s / 3600

    lines.append("### 📊 Totals\n")
    lines.append(f"- 📏 **Distance:** {miles:.1f} mi")
    lines.append(f"- ⏱️ **Moving Time:** {hours:.1f} hours")
    lines.append(f"- ⛰️ **Elevation:** {_format_elevation(elev_m)}")

    # Breakdown by type
    if breakdown:
        lines.append("\n### 🏷️ By Activity Type\n")
        lines.append("| Type | Count | Icon |")
        lines.append("|------|-------|------|")
        for entry in breakdown:
            st = entry["sport_type"] or "Unknown"
            icon = _sport_icon(st)
            lines.append(f"| {st} | {entry['count']} | {icon} |")

    return "\n".join(lines)


def format_delete_activities(deleted: int, requested_ids: list[int]) -> str:
    not_found = len(requested_ids) - deleted
    lines = [f"## 🗑️ Delete Activities\n"]
    lines.append(f"- **✅ Deleted:** {deleted}")
    if not_found:
        lines.append(f"- **⚠️ Not found:** {not_found} (already removed or invalid ID)")
    lines.append(f"- **🏛️ IDs requested:** {', '.join(str(i) for i in requested_ids)}")
    return "\n".join(lines)
