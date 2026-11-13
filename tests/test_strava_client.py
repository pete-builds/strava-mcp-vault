"""Tests for clients/strava.py."""

import time
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from strava_mcp_vault.clients.strava import RateLimitError, StravaClient
from strava_mcp_vault.exceptions import ReadBudgetExhausted


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


# ── Read budget (X-ReadRateLimit-*) ────────────────────────────────────
# Strava keeps a separate, smaller budget for reads: 100 per 15 minutes and
# 1,000 a day by default, reported in X-ReadRateLimit-Usage and -Limit as
# "15min,daily" (developers.strava.com/docs/rate-limits).


def _read_headers(usage: str, limit: str = "100,1000") -> dict:
    return {
        "X-RateLimit-Usage": "10,200",
        "X-RateLimit-Limit": "200,2000",
        "X-ReadRateLimit-Usage": usage,
        "X-ReadRateLimit-Limit": limit,
    }


@respx.mock
async def test_read_budget_is_parsed_and_shown_with_the_general_limit(client):
    respx.get("https://www.strava.com/api/v3/athlete").mock(
        return_value=httpx.Response(200, json={"id": 1}, headers=_read_headers("42,310"))
    )
    await client.get_athlete()
    rl = client.rate_limit_remaining
    assert rl["short"] == {"usage": 10, "limit": 200}
    assert rl["read"]["short"]["usage"] == 42 and rl["read"]["short"]["limit"] == 100
    assert rl["read"]["long"]["usage"] == 310 and rl["read"]["long"]["limit"] == 1000
    assert rl["read"]["short"]["resets_at"].endswith("UTC")
    assert client.read_budget_remaining() == 58


@respx.mock
async def test_a_call_past_the_read_budget_is_refused_before_sending(client):
    route = respx.get("https://www.strava.com/api/v3/athlete").mock(
        return_value=httpx.Response(200, json={"id": 1}, headers=_read_headers("100,400"))
    )
    await client.get_athlete()
    with pytest.raises(ReadBudgetExhausted) as exc_info:
        await client.get_athlete()
    assert route.call_count == 1  # the refused call never left the host
    message = str(exc_info.value)
    assert "15-minute read limit resets at" in message and "UTC" in message
    assert isinstance(exc_info.value, RateLimitError)


@respx.mock
async def test_a_spent_daily_read_budget_says_midnight_utc(client):
    respx.get("https://www.strava.com/api/v3/athlete").mock(
        return_value=httpx.Response(200, json={"id": 1}, headers=_read_headers("3,1000"))
    )
    await client.get_athlete()
    with pytest.raises(ReadBudgetExhausted, match="daily read limit resets at 00:00 UTC"):
        await client.get_athlete()


@respx.mock
async def test_a_reset_window_lets_calls_through_again(client):
    """Usage is only refreshed by a call, so a stale "100 of 100" must not refuse forever."""
    route = respx.get("https://www.strava.com/api/v3/athlete").mock(
        return_value=httpx.Response(200, json={"id": 1}, headers=_read_headers("100,400"))
    )
    await client.get_athlete()
    later = time.time() + 900
    client._now = lambda: later
    assert client.read_budget()["short"]["usage"] == 0
    await client.get_athlete()
    assert route.call_count == 2
