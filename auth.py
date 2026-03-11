"""Optional bearer token authentication middleware.

If MCP_AUTH_TOKEN is set in the environment, all incoming HTTP requests
must include a matching Authorization: Bearer <token> header. If the
env var is not set, all requests are allowed (backwards compatible).
"""

import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {self.token}":
            logger.warning("Rejected request: invalid or missing auth token")
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


def maybe_add_auth(app):
    """Wrap the ASGI app with bearer auth if MCP_AUTH_TOKEN is set.

    Returns the (possibly wrapped) app.
    """
    token = os.getenv("MCP_AUTH_TOKEN")
    if not token:
        logger.info("MCP_AUTH_TOKEN not set; endpoint authentication disabled")
        return app
    logger.info("MCP endpoint authentication enabled")
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app
