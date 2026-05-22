#!/usr/bin/env python3
"""Docker HEALTHCHECK probe.

Streamable HTTP exposes a single ``/mcp`` endpoint. A bare GET (no
SSE-streaming Accept header) returns HTTP 400/405/406 from the MCP
transport layer. Treat that as healthy: it confirms the server is
listening, routed, and (if MCP_AUTH_TOKEN is set) accepting the bearer
token. Without the token, the auth middleware short-circuits to 401
before the request reaches the MCP layer, so the token matters here.
"""

import os
import sys
import urllib.error
import urllib.request

_HEALTHY_NON_OK_CODES: frozenset[int] = frozenset({400, 405, 406})


def check() -> int:
    port = os.environ.get("STRAVA_MCP_PORT", "18201")
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"http://localhost:{port}/mcp"
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=5)  # noqa: S310 - localhost only
        return 0 if resp.status == 200 else 1
    except urllib.error.HTTPError as exc:
        return 0 if exc.code in _HEALTHY_NON_OK_CODES else 1
    except (urllib.error.URLError, OSError):
        return 1


if __name__ == "__main__":
    sys.exit(check())
