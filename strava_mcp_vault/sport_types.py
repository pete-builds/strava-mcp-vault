"""Strava sport-type sets and alias expansion.

Strava distinguishes sport types finely (Ride, GravelRide, MountainBikeRide,
VirtualRide, EBikeRide…). When filtering, callers usually want a category
("rides") rather than enumerating every member. This module owns the
canonical sets and the expander used by both the cache layer (filtering)
and the formatters (categorization).
"""

# Canonical Strava sport_type sets. Keep these in sync with values seen on
# /athlete/activities responses.
RIDE_TYPES = frozenset({"Ride", "VirtualRide", "MountainBikeRide", "GravelRide", "EBikeRide"})
RUN_TYPES = frozenset({"Run", "VirtualRun", "TrailRun"})
SNOW_TYPES = frozenset({"Snowboard", "AlpineSki", "BackcountrySki", "NordicSki", "Snowshoe"})
WALK_TYPES = frozenset({"Walk", "Hike"})
SWIM_TYPES = frozenset({"Swim"})

# Alias → expanded set. Keys are lowercase, plural where natural.
_ALIASES: dict[str, frozenset[str]] = {
    "ride": RIDE_TYPES,
    "rides": RIDE_TYPES,
    "cycling": RIDE_TYPES,
    "run": RUN_TYPES,
    "runs": RUN_TYPES,
    "running": RUN_TYPES,
    "snow": SNOW_TYPES,
    "ski": SNOW_TYPES,
    "skiing": SNOW_TYPES,
    "walk": WALK_TYPES,
    "walks": WALK_TYPES,
    "hike": WALK_TYPES,
    "hikes": WALK_TYPES,
    "hiking": WALK_TYPES,
    "swim": SWIM_TYPES,
    "swims": SWIM_TYPES,
    "swimming": SWIM_TYPES,
}


def expand_sport_type(value: str | None) -> list[str] | None:
    """Expand a sport_type filter value into a list of Strava sport_types.

    Accepts:
    - ``None`` or empty → returns ``None`` (caller treats as no filter).
    - A single Strava type (e.g. ``"Ride"``) → ``["Ride"]``.
    - A comma-separated list (e.g. ``"Ride,GravelRide"``) → both, with each
      token also expanded if it's a known alias.
    - A category alias (e.g. ``"rides"``, ``"cycling"``) → all members of
      that category.

    Alias matching is case-sensitive on lowercase keys — ``"rides"`` expands,
    but ``"Ride"`` is treated as the literal Strava sport_type so existing
    callers passing CamelCase types keep working unchanged. Use the
    lowercase form for category expansion. Duplicates are collapsed.
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    expanded: set[str] = set()
    for token in (t.strip() for t in raw.split(",")):
        if not token:
            continue
        alias_hit = _ALIASES.get(token)
        if alias_hit is not None:
            expanded.update(alias_hit)
        else:
            # Treat as a literal Strava sport_type. Preserve casing.
            expanded.add(token)

    return sorted(expanded) if expanded else None
