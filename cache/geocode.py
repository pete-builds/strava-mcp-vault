"""Nominatim geocoding helpers (forward only — no API key required)."""

import asyncio
import json
import urllib.parse
import urllib.request

_USER_AGENT = "strava-mcp-vault/1.0"
_BASE = "https://nominatim.openstreetmap.org"


def _get(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


async def forward_geocode(place: str) -> tuple[float, float] | None:
    """Resolve a place name to (lat, lon). Returns None if not found."""
    url = f"{_BASE}/search?q={urllib.parse.quote(place)}&format=json&limit=1"
    result = await asyncio.to_thread(_get, url)
    if not result:
        return None
    return float(result[0]["lat"]), float(result[0]["lon"])
