#!/usr/bin/env python3
import sys
import urllib.request

try:
    resp = urllib.request.urlopen("http://localhost:18201/sse", timeout=5)
    sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
