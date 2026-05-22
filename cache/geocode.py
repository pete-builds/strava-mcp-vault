"""Nominatim geocoding helpers (forward + reverse, no API key required)."""

import asyncio
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_USER_AGENT = "strava-mcp-vault/1.0"
_BASE = "https://nominatim.openstreetmap.org"
_last_request_time: float = 0.0
# Guards _last_request_time. _get runs under asyncio.to_thread, so concurrent
# reverse-geocode batches can race the read/sleep/write triple without this.
_request_lock = threading.Lock()

# In-memory cache for forward geocoding. Place names rarely change and we
# bound this to keep memory predictable; on overflow we just clear it.
_forward_cache: dict[str, tuple[float, float] | None] = {}
_FORWARD_CACHE_MAX = 1000


def _get(url: str) -> dict | list:
    global _last_request_time
    with _request_lock:
        # Nominatim requires max 1 request/second
        elapsed = time.monotonic() - _last_request_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as r:
            result = json.loads(r.read())
        _last_request_time = time.monotonic()
        return result


async def forward_geocode(place: str) -> tuple[float, float] | None:
    """Resolve a place name to (lat, lon). Returns None if not found or on error.

    Results (hits and misses) are cached in-process to avoid re-hitting
    Nominatim for repeated queries.
    """
    key = place.strip().lower()
    if key in _forward_cache:
        return _forward_cache[key]

    url = f"{_BASE}/search?q={urllib.parse.quote(place)}&format=json&limit=1"
    try:
        result = await asyncio.to_thread(_get, url)
    except Exception:
        logger.warning("Geocoding failed for '%s'", place, exc_info=True)
        return None
    coords = (float(result[0]["lat"]), float(result[0]["lon"])) if result else None

    if len(_forward_cache) >= _FORWARD_CACHE_MAX:
        _forward_cache.clear()
    _forward_cache[key] = coords
    return coords


async def reverse_geocode_many(
    coords: list[tuple[float, float]],
) -> dict[tuple[float, float], str]:
    """Reverse geocode a list of (lat, lon) pairs.

    Deduplicates by rounding to 2 decimal places (~1 km), so nearby
    activities only trigger one request. Returns a dict mapping each
    original (lat, lon) to a 'City, State' string.
    """

    def _city_state(addr: dict) -> str:
        city = (
            addr.get("city") or addr.get("town") or addr.get("village") or addr.get("hamlet") or ""
        )
        state = addr.get("state", "")
        return f"{city}, {state}" if city else state

    def _fetch_one(lat: float, lon: float) -> str:
        url = f"{_BASE}/reverse?lat={lat}&lon={lon}&format=json"
        try:
            data = _get(url)
            return _city_state(data.get("address", {}))
        except Exception:
            return ""

    # Build unique rounded keys
    rounded: dict[tuple[float, float], tuple[float, float]] = {}
    for lat, lon in coords:
        key = (round(lat, 2), round(lon, 2))
        rounded[(lat, lon)] = key

    unique_keys = list({v for v in rounded.values()})
    cache: dict[tuple[float, float], str] = {}
    for key in unique_keys:
        cache[key] = await asyncio.to_thread(_fetch_one, *key)

    return {orig: cache[rounded[orig]] for orig in coords}
