#!/usr/bin/env python3
import os
import sys
import urllib.request

try:
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    req = urllib.request.Request(
        "http://localhost:18201/sse",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    resp = urllib.request.urlopen(req, timeout=5)
    sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
