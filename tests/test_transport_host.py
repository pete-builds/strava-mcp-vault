"""Regression tests for the Streamable HTTP transport bind host.

Since mcp 2.0, MCPServer.streamable_http_app() auto-enables DNS-rebinding
protection whenever it is called with a loopback host, and its default host
is "127.0.0.1". Under that protection the server answers HTTP 421
"Invalid Host header" to any request whose Host header is not localhost.

strava-mcp-vault is reached over the LAN and Tailscale, so dropping the
explicit non-loopback host would break every real client while the unit
suite stayed green and a localhost healthcheck kept passing. These tests
exist so that regression cannot happen silently.
"""

import ipaddress
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

# The real deployment target. If a LAN client with this Host header cannot
# talk to the server, the deployment is broken.
DEPLOY_HOST_HEADER = "192.168.86.20:18201"

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "regression-test", "version": "1.0"},
    },
}

MCP_HEADERS = {
    "Host": DEPLOY_HOST_HEADER,
    "Origin": f"http://{DEPLOY_HOST_HEADER}",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# The exact set mcp 2.0 treats as loopback when deciding whether to
# auto-enable DNS-rebinding protection.
SDK_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


@pytest.fixture(autouse=True)
def _no_auth_token(monkeypatch):
    """Keep bearer auth out of the way; these tests are about transport."""
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)


def _post_initialize(app):
    """Drive one initialize request through the app's real ASGI lifespan.

    _startup() is stubbed out: it wants live Strava credentials and a database,
    and none of that is what these tests are checking.
    """
    with patch("server._startup", AsyncMock()), TestClient(app) as client:
        return client.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE_REQUEST)


def test_http_host_is_not_loopback():
    """The configured bind host must not trip the SDK's loopback check."""
    from server import HTTP_HOST

    assert HTTP_HOST not in SDK_LOOPBACK_HOSTS
    assert not ipaddress.ip_address(HTTP_HOST).is_loopback


def test_lan_client_is_not_rejected():
    """A LAN-style Host header must get a real MCP response, not a 421."""
    from server import build_app

    response = _post_initialize(build_app())

    assert response.status_code != 421, (
        "Server rejected a LAN Host header with 'Invalid Host header'. "
        "streamable_http_app() was most likely called without an explicit "
        "non-loopback host, so mcp auto-enabled DNS-rebinding protection."
    )
    assert response.status_code == 200
    assert "protocolVersion" in response.text


def test_loopback_default_would_reject_lan_client():
    """Positive control: prove the test above can detect the failure mode.

    This builds the app the way a well-meaning "cleanup" commit would (letting
    the host default to loopback) and asserts it produces exactly the 421 that
    test_lan_client_is_not_rejected guards against. If this test ever stops
    failing that way, the guard above has gone blind and needs rewriting.
    """
    from server import mcp

    response = _post_initialize(mcp.streamable_http_app())

    assert response.status_code == 421
    assert "Invalid Host header" in response.text
