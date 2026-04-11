"""Tests for clients/strava.py."""

import time
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from clients.strava import RateLimitError, StravaClient


@pytest.fixture
def mock_cache_db():
    db = AsyncMock()
    db.get_tokens.return_value = {
        "access_token": "test_access",
        "refresh_token": "test_refresh",
        "expires_at": int(time.time()) + 7200,
    }
    db.set_tokens = AsyncMock()
    return db


@pytest.fixture
async def client(mock_cache_db):
    c = StravaClient(
        client_id="test_id",
        client_secret="test_secret",
        cache_db=mock_cache_db,
    )
    await c.init_tokens()
    return c


# ── init_tokens ────────────────────────────────────────────────────────


async def test_init_tokens_from_db(mock_cache_db):
    c = StravaClient("id", "secret", cache_db=mock_cache_db)
    await c.init_tokens()
    assert c._access_token == "test_access"
    assert c._refresh_token == "test_refresh"


async def test_init_tokens_first_boot():
    db = AsyncMock()
    db.get_tokens.return_value = None
    c = StravaClient("id", "secret", cache_db=db)
    await c.init_tokens()
    assert c._access_token is None


# ── API methods ────────────────────────────────────────────────────────


@respx.mock
async def test_get_athlete(client):
    respx.get("https://www.strava.com/api/v3/athlete").mock(
        return_value=httpx.Response(200, json={"id": 12345, "firstname": "Pete"})
    )
    result = await client.get_athlete()
    assert result["id"] == 12345


@respx.mock
async def test_get_activities_pagination(client):
    route = respx.get("https://www.strava.com/api/v3/athlete/activities").mock(
        return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}])
    )
    result = await client.get_activities(page=2, per_page=50, after=1000)
    assert len(result) == 2
    assert route.calls.last.request.url.params["page"] == "2"
    assert route.calls.last.request.url.params["per_page"] == "50"
    assert route.calls.last.request.url.params["after"] == "1000"


@respx.mock
async def test_get_activity(client):
    respx.get("https://www.strava.com/api/v3/activities/999").mock(
        return_value=httpx.Response(200, json={"id": 999, "name": "Test"})
    )
    result = await client.get_activity(999)
    assert result["id"] == 999


@respx.mock
async def test_get_gear(client):
    respx.get("https://www.strava.com/api/v3/gear/g123").mock(
        return_value=httpx.Response(200, json={"id": "g123", "name": "Trek Domane"})
    )
    result = await client.get_gear("g123")
    assert result["name"] == "Trek Domane"


# ── Token refresh ──────────────────────────────────────────────────────


@respx.mock
async def test_token_refresh_on_expiry(client, mock_cache_db):
    # Force token to be expired
    client._expires_at = 0

    respx.post("https://www.strava.com/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new_access",
                "refresh_token": "new_refresh",
                "expires_at": int(time.time()) + 7200,
            },
        )
    )
    respx.get("https://www.strava.com/api/v3/athlete").mock(
        return_value=httpx.Response(200, json={"id": 12345})
    )

    result = await client.get_athlete()
    assert result["id"] == 12345
    assert client._access_token == "new_access"
    mock_cache_db.set_tokens.assert_called_once()


# ── Rate limiting ──────────────────────────────────────────────────────


@respx.mock
async def test_rate_limit_error(client):
    respx.get("https://www.strava.com/api/v3/athlete").mock(
        return_value=httpx.Response(
            429,
            headers={
                "X-RateLimit-Usage": "100,900",
                "X-RateLimit-Limit": "100,1000",
            },
        )
    )
    with pytest.raises(RateLimitError) as exc_info:
        await client.get_athlete()
    assert "rate limit" in str(exc_info.value).lower()


@respx.mock
async def test_rate_limit_tracking(client):
    respx.get("https://www.strava.com/api/v3/athlete").mock(
        return_value=httpx.Response(
            200,
            json={"id": 1},
            headers={
                "X-RateLimit-Usage": "10,200",
                "X-RateLimit-Limit": "100,1000",
            },
        )
    )
    await client.get_athlete()
    rl = client.rate_limit_remaining
    assert rl is not None
    assert rl["short"]["usage"] == 10
    assert rl["long"]["limit"] == 1000


# ── Retry on transient errors ─────────────────────────────────────────


@respx.mock
async def test_retry_on_transient_error(client):
    route = respx.get("https://www.strava.com/api/v3/athlete")
    route.side_effect = [
        httpx.ConnectError("connection reset"),
        httpx.Response(200, json={"id": 12345}),
    ]
    result = await client.get_athlete()
    assert result["id"] == 12345
    assert route.call_count == 2


@respx.mock
async def test_retry_exhausted(client):
    route = respx.get("https://www.strava.com/api/v3/athlete")
    route.side_effect = httpx.ConnectError("connection reset")
    with pytest.raises(httpx.ConnectError):
        await client.get_athlete()
    assert route.call_count == 2


# ── rate_limit_remaining property ──────────────────────────────────────


def test_rate_limit_remaining_none():
    db = AsyncMock()
    c = StravaClient("id", "secret", cache_db=db)
    assert c.rate_limit_remaining is None


def test_rate_limit_remaining_parse():
    db = AsyncMock()
    c = StravaClient("id", "secret", cache_db=db)
    c._rate_limit_usage = "15,300"
    c._rate_limit_limit = "100,1000"
    rl = c.rate_limit_remaining
    assert rl["short"]["usage"] == 15
    assert rl["long"]["usage"] == 300
