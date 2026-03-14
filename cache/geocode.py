"""Nominatim geocoding helpers (forward + reverse — no API key required)."""

import asyncio
import json
import time
import urllib.parse
import urllib.request

_USER_AGENT = "strava-mcp-vault/1.0"
_BASE = "https://nominatim.openstreetmap.org"
_last_request_time: float = 0.0


def _get(url: str) -> dict | list:
    global _last_request_time
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
    """Resolve a place name to (lat, lon). Returns None if not found."""
    url = f"{_BASE}/search?q={urllib.parse.quote(place)}&format=json&limit=1"
    result = await asyncio.to_thread(_get, url)
    if not result:
        return None
    return float(result[0]["lat"]), float(result[0]["lon"])


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
            addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("hamlet")
            or ""
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
