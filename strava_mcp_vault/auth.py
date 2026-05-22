"""HTTP middleware for the MCP endpoint.

Two opt-in ASGI middlewares:

- BearerAuthMiddleware: enforces Authorization: Bearer <token> when
  MCP_AUTH_TOKEN is set. Required at startup unless
  MCP_ALLOW_UNAUTHENTICATED=1 is also set.
- OriginCheckMiddleware: rejects browser requests whose Origin header
  isn't in MCP_ALLOWED_ORIGINS. Provides DNS-rebinding protection per
  the MCP spec for local streamable-HTTP servers. Requests without an
  Origin header (non-browser clients) are allowed through.

Both are pure ASGI middlewares (not BaseHTTPMiddleware) to stay
compatible with the long-lived streaming responses that Streamable HTTP
and the deprecated HTTP+SSE transport emit.
"""

import hmac
import logging
import os

logger = logging.getLogger(__name__)


class BearerAuthMiddleware:
    """Pure ASGI middleware for bearer token auth.

    Unlike BaseHTTPMiddleware, this does not wrap the response stream
    and is fully compatible with long-lived streaming connections
    (Streamable HTTP and the deprecated HTTP+SSE).
    """

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"")
            if not hmac.compare_digest(auth_header, self._expected):
                logger.warning("Rejected request: invalid or missing auth token")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            [b"content-type", b"application/json"],
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error": "Unauthorized"}',
                    }
                )
                return
        await self.app(scope, receive, send)


class OriginCheckMiddleware:
    """Reject HTTP requests whose Origin header isn't in the allowlist.

    DNS-rebinding protection for browser clients of a streamable-HTTP MCP
    server (per the MCP spec). Requests with no Origin header (curl, MCP
    desktop clients, server-to-server) are allowed through.
    """

    def __init__(self, app, allowed_origins: set[str]):
        self.app = app
        self._allowed = allowed_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin", b"").decode()
            if origin and origin not in self._allowed:
                logger.warning("Rejected request: origin %r not in allowlist", origin)
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [[b"content-type", b"application/json"]],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error": "Origin not allowed"}',
                    }
                )
                return
        await self.app(scope, receive, send)


def maybe_add_auth(app):
    """Wrap the ASGI app with bearer auth if MCP_AUTH_TOKEN is set.

    Returns the (possibly wrapped) app.
    """
    token = os.getenv("MCP_AUTH_TOKEN")
    if not token:
        logger.info("MCP_AUTH_TOKEN not set; endpoint authentication disabled")
        return app
    logger.info("MCP endpoint authentication enabled")
    return BearerAuthMiddleware(app, token=token)


def maybe_add_origin_check(app):
    """Wrap the ASGI app with Origin enforcement if MCP_ALLOWED_ORIGINS is set.

    MCP_ALLOWED_ORIGINS is a comma-separated list of exact origin strings
    (scheme + host + optional port). Example:
        MCP_ALLOWED_ORIGINS=http://localhost:18201,https://claude.ai
    """
    raw = os.getenv("MCP_ALLOWED_ORIGINS", "").strip()
    if not raw:
        logger.info("MCP_ALLOWED_ORIGINS not set; Origin header not enforced")
        return app
    allowed = {o.strip() for o in raw.split(",") if o.strip()}
    logger.info("Origin allowlist enforced (%d entries)", len(allowed))
    return OriginCheckMiddleware(app, allowed_origins=allowed)
