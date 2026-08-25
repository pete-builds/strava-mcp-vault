"""Every tool declares what it does, and whether it leaves this host.

Nothing in an MCP manifest distinguishes delete_vault_activity from
get_activity unless the tool says so, so a client has no basis on which to
prompt before a destructive call.

The open-world split is the part worth stating. The vault is a LOCAL store, so
most reads never leave the host, while the Strava-backed tools do. Marking
everything open-world would have been the easy uniform answer and would have
misdescribed most of the surface.
"""

from __future__ import annotations

import asyncio

import pytest

from strava_mcp_vault.server import mcp

LOCAL = {
    "query_vault",
    "get_cache_stats",
    "get_activities_near",
    "set_activity_location",
    "delete_vault_activity",
}
REMOTE = {
    "get_recent_activities",
    "get_activity",
    "get_activity_streams",
    "get_athlete_profile",
    "get_athlete_stats",
    "sync_activities",
}
WRITES = {"set_activity_location", "delete_vault_activity", "sync_activities"}


def hint(tool, name: str):
    """Read one annotation hint, tolerating either spelling.

    This repo's MCP types expose the hints as snake_case (read_only_hint);
    other servers in the fleet expose the wire-format camelCase. Reading
    whichever is present keeps this test about the classification rather than
    about which version of the types package happens to be installed.
    """
    ann = tool.annotations
    snake = "".join("_" + c.lower() if c.isupper() else c for c in name)
    for attr in (name, snake):
        if hasattr(ann, attr):
            return getattr(ann, attr)
    raise AttributeError(f"{name} not present as either spelling on {ann!r}")


@pytest.fixture(scope="module")
def tools():
    """The live manifest, not the source. What a client would receive."""
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_every_tool_is_annotated(tools):
    assert sorted(n for n, t in tools.items() if t.annotations is None) == []


def test_the_expected_eleven_are_present(tools):
    """Guards the guard: an empty manifest would pass everything below."""
    assert set(tools) == LOCAL | REMOTE


def test_the_delete_is_marked_destructive(tools):
    assert hint(tools["delete_vault_activity"], "destructiveHint") is True


def test_writes_are_never_marked_read_only(tools):
    mislabelled = sorted(n for n in WRITES if hint(tools[n], "readOnlyHint"))
    assert mislabelled == []


def test_reads_are_not_marked_destructive(tools):
    """A safe tool that scares the client is also wrong."""
    wrong = sorted(n for n, t in tools.items() if n not in WRITES and hint(t, "destructiveHint"))
    assert wrong == []


def test_local_tools_do_not_claim_an_open_world(tools):
    wrong = sorted(n for n in LOCAL if hint(tools[n], "openWorldHint") is not False)
    assert wrong == []


def test_strava_backed_tools_do(tools):
    wrong = sorted(n for n in REMOTE if hint(tools[n], "openWorldHint") is not True)
    assert wrong == []


def test_the_sync_is_idempotent(tools):
    """It upserts, so running it twice converges rather than duplicating.

    Worth pinning because "sync" reads like a create, and a non-idempotent hint
    here would discourage exactly the retry that is safe and useful.
    """
    assert hint(tools["sync_activities"], "idempotentHint") is True
