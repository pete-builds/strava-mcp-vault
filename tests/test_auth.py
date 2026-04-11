"""Tests for auth.py."""

from unittest.mock import AsyncMock

from auth import BearerAuthMiddleware, maybe_add_auth

# ── maybe_add_auth ─────────────────────────────────────────────────────


def test_maybe_add_auth_no_token(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    app = object()
    result = maybe_add_auth(app)
    assert result is app  # unwrapped, same object


def test_maybe_add_auth_with_token(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret123")
    app = object()
    result = maybe_add_auth(app)
    assert isinstance(result, BearerAuthMiddleware)


# ── BearerAuthMiddleware ───────────────────────────────────────────────


async def test_middleware_allows_valid_token():
    inner_app = AsyncMock()
    middleware = BearerAuthMiddleware(inner_app, token="secret123")

    scope = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer secret123")],
    }
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)
    inner_app.assert_called_once_with(scope, receive, send)


async def test_middleware_rejects_invalid_token():
    inner_app = AsyncMock()
    middleware = BearerAuthMiddleware(inner_app, token="secret123")

    scope = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer wrong-token")],
    }
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)
    inner_app.assert_not_called()
    # Should have sent a 401 response
    assert send.call_count == 2
    start_call = send.call_args_list[0][0][0]
    assert start_call["status"] == 401


async def test_middleware_rejects_missing_token():
    inner_app = AsyncMock()
    middleware = BearerAuthMiddleware(inner_app, token="secret123")

    scope = {
        "type": "http",
        "headers": [],
    }
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)
    inner_app.assert_not_called()


async def test_middleware_passes_non_http_scope():
    """Non-HTTP scopes (like websocket) should pass through without auth check."""
    inner_app = AsyncMock()
    middleware = BearerAuthMiddleware(inner_app, token="secret123")

    scope = {"type": "websocket", "headers": []}
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)
    inner_app.assert_called_once_with(scope, receive, send)
