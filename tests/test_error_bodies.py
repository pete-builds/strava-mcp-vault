"""Upstream error bodies are summarised, not forwarded into a tool result.

The detail string ends up in a tool result, which goes straight into an agent's
context. Strava's documented JSON shape is genuinely useful and is kept;
everything else is an arbitrary blob that says nothing an agent can act on, and
truncating it to 200 characters made it less readable rather than less risky.

On the credential question, since that is the reason to look at a path like
this and it does NOT apply here: the bearer token is sent as a header and the
refresh token only in a POST body, so nothing Strava echoes back carries a
secret. This is context hygiene, not a leak.
"""

from __future__ import annotations

import json

import httpx

from strava_mcp_vault.clients.strava import _describe_error_body


def _resp(status: int, body: str, content_type: str) -> httpx.Response:
    return httpx.Response(
        status,
        content=body.encode(),
        headers={"content-type": content_type},
        request=httpx.Request("GET", "https://www.strava.com/api/v3/athlete"),
    )


def test_the_documented_json_shape_is_kept():
    body = json.dumps({
        "message": "Authorization Error",
        "errors": [{"resource": "Athlete", "field": "access_token", "code": "invalid"}],
    })
    detail = _describe_error_body(_resp(401, body, "application/json"))

    assert "Authorization Error" in detail
    # The field/code pair is the part that says what to do about it.
    assert "access_token: invalid" in detail


def test_a_message_without_a_field_list_still_works():
    body = json.dumps({"message": "Rate Limit Exceeded"})
    assert _describe_error_body(_resp(429, body, "application/json")) == (
        "Rate Limit Exceeded"
    )


def test_an_html_maintenance_page_reports_its_shape_not_its_markup():
    html = "<html><head><title>Strava is down</title></head>" + "x" * 3000
    detail = _describe_error_body(_resp(503, html, "text/html"))

    assert "<html>" not in detail
    assert "text/html" in detail
    assert str(len(html)) in detail


def test_a_long_upstream_message_is_bounded():
    """A "documented field" is only short if something enforces it."""
    body = json.dumps({"message": "z" * 5000})
    assert len(_describe_error_body(_resp(400, body, "application/json"))) == 200


def test_an_empty_body_produces_an_empty_detail():
    """StravaAPIError already renders status and path, so there is nothing to add."""
    assert _describe_error_body(_resp(500, "", "text/plain")) == ""


async def test_the_request_path_actually_uses_the_helper():
    """Guards the call site, not just the helper.

    Testing _describe_error_body alone would keep passing if someone put
    `e.response.text[:200]` back at the raise site: the helper would still be
    correct and still be dead code. This drives a real _get.
    """
    import pytest

    from strava_mcp_vault.clients.strava import StravaClient
    from strava_mcp_vault.exceptions import StravaAPIError

    html = "<html><body>Strava is down for maintenance</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=html.encode(),
                              headers={"content-type": "text/html"})

    client = StravaClient(client_id="id", client_secret="secret", cache_db=None)
    client._access_token = "token"
    client._expires_at = 2**31  # far future, so no refresh is attempted
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    with pytest.raises(StravaAPIError) as caught:
        await client._get("/athlete")

    assert "<html>" not in str(caught.value)
    assert "text/html" in str(caught.value)
    await client._client.aclose()
