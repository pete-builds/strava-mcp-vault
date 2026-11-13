"""Tests for strava-mcp-vault-login. No network: Strava's token endpoint is a
local stub on 127.0.0.1, and the "browser" is a thread that follows the
redirect the way Strava would."""

import asyncio
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import strava_mcp_vault.cache.encryption as enc
from strava_mcp_vault import login
from strava_mcp_vault.cache.db import CacheDB

CLIENT_ID = "424242"
CLIENT_SECRET = "c0ffeec0ffeec0ffeec0ffeec0ffeec0ffeec0ff"
CODE = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
ACCESS = "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
REFRESH = "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"
EXPIRES_AT = 1893456000
SECRETS = (CLIENT_SECRET, CODE, ACCESS, REFRESH)
GRANTED = "read,activity:read_all"


# --------------------------------------------------------------------------
# Fixtures: a fake Strava token endpoint and a fake browser
# --------------------------------------------------------------------------


class _StubTokenEndpoint:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode())
                stub.requests.append({k: v[0] for k, v in form.items()})
                payload = json.dumps(stub.body).encode()
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/oauth/token"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def token_endpoint():
    stub = _StubTokenEndpoint(
        body={
            "token_type": "Bearer",
            "expires_at": EXPIRES_AT,
            "expires_in": 21600,
            "refresh_token": REFRESH,
            "access_token": ACCESS,
            "athlete": {"id": 1, "firstname": "Testy"},
        }
    )
    yield stub
    stub.close()


class _FakeBrowser:
    """Plays Strava: reads the authorize URL it was handed, then follows the
    redirect with whatever query it is told to send."""

    def __init__(self, **overrides):
        self.overrides = overrides
        self.authorize_url = None
        self.callback_url = None
        self.thread = None
        self.page = None

    def __call__(self, url):
        self.authorize_url = url
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        query = {"state": params["state"], "code": CODE, "scope": GRANTED}
        query.update(self.overrides)
        query = {k: v for k, v in query.items() if v is not None}
        self.callback_url = f"{params['redirect_uri']}?{urllib.parse.urlencode(query)}"
        # Follow the redirect via the loopback address the listener is bound
        # to, since that is where "localhost" resolves.
        target = self.callback_url.replace("//localhost:", "//127.0.0.1:", 1)

        def follow():
            with urllib.request.urlopen(target, timeout=5) as resp:
                self.page = resp.read().decode()

        self.thread = threading.Thread(target=follow, daemon=True)
        self.thread.start()
        return True


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for key in (
        "STRAVA_CLIENT_ID",
        "STRAVA_CLIENT_SECRET",
        "STRAVA_ACCESS_TOKEN",
        "STRAVA_REFRESH_TOKEN",
        "VAULT_DB_PATH",
        "TOKEN_ENCRYPTION_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    # main() calls load_dotenv() as the server does, which walks up from the
    # package directory. A developer's real .env must never reach these tests.
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.chdir(tmp_path)
    enc._fernet, enc._initialized = None, False
    yield
    enc._fernet, enc._initialized = None, False


def _read_vault(db_path):
    async def go():
        db = CacheDB(str(db_path))
        await db.init()
        try:
            return await db.get_tokens()
        finally:
            await db.close()

    return asyncio.run(go())


def _raw_token_rows(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT access_token, refresh_token FROM tokens").fetchall()


def _no_tokens_stored(db_path):
    return not Path(db_path).exists() or not _raw_token_rows(db_path)


def _run_main(argv, token_endpoint, browser, **kwargs):
    return login.main(argv, open_browser=browser, token_url=token_endpoint.url, **kwargs)


# --------------------------------------------------------------------------
# Authorize URL
# --------------------------------------------------------------------------


def test_authorize_url_matches_strava_documented_parameters():
    url = login.build_authorize_url(CLIENT_ID, "http://localhost:5000/callback", "s-t-a-t-e")
    parts = urllib.parse.urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://www.strava.com/oauth/authorize"
    )
    assert urllib.parse.parse_qs(parts.query) == {
        "client_id": [CLIENT_ID],
        "redirect_uri": ["http://localhost:5000/callback"],
        "response_type": ["code"],
        "approval_prompt": ["force"],
        "scope": ["read,activity:read_all"],
        "state": ["s-t-a-t-e"],
    }


def test_each_sign_in_uses_a_fresh_unguessable_state(token_endpoint):
    states = []
    for _ in range(2):
        browser = _FakeBrowser()
        login.run_login(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            timeout=5,
            open_browser=browser,
            token_url=token_endpoint.url,
        )
        states.append(dict(urllib.parse.parse_qsl(browser.authorize_url.split("?", 1)[1]))["state"])
    assert states[0] != states[1]
    assert all(len(s) >= 40 for s in states)


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


def test_happy_path_writes_vault_through_server_storage(
    token_endpoint, monkeypatch, tmp_path, capsys
):
    db_path = tmp_path / "data" / "vault.db"
    monkeypatch.setenv("VAULT_DB_PATH", str(db_path))
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key)

    browser = _FakeBrowser()
    assert _run_main([], token_endpoint, browser) == 0

    # Strava got exactly the documented exchange.
    assert token_endpoint.requests == [
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": CODE,
            "grant_type": "authorization_code",
        }
    ]
    # Read back the way the server reads at startup.
    assert _read_vault(db_path) == {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "expires_at": EXPIRES_AT,
    }
    # Encrypted at rest with the same key, by the same module.
    (row,) = _raw_token_rows(db_path)
    assert ACCESS not in row and REFRESH not in row
    assert Fernet(key.encode()).decrypt(row[0].encode()).decode() == ACCESS
    # A vault this command creates is owner-only.
    assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o600
    # The browser was told to go back to the terminal and shown nothing else.
    browser.thread.join(5)
    assert "Return to your terminal" in browser.page

    out = capsys.readouterr().out
    assert "Signed in to Strava as Testy" in out
    assert "Next step: strava-mcp-vault" in out


def test_server_startup_loads_what_login_stored(token_endpoint, monkeypatch, tmp_path):
    """The point of the command: a server started afterwards just works, with
    no STRAVA_ACCESS_TOKEN or STRAVA_REFRESH_TOKEN in its environment."""
    from strava_mcp_vault import server

    db_path = tmp_path / "vault.db"
    monkeypatch.setenv("VAULT_DB_PATH", str(db_path))
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    assert _run_main([], token_endpoint, _FakeBrowser()) == 0

    asyncio.run(server._startup())
    client = server.manager.client
    try:
        assert client._access_token == ACCESS
        assert client._refresh_token == REFRESH
        assert client._expires_at == EXPIRES_AT
    finally:
        asyncio.run(server.manager.db.close())
        server.manager = None


def test_existing_vault_keeps_its_data_and_mode(token_endpoint, monkeypatch, tmp_path):
    db_path = tmp_path / "vault.db"

    async def seed():
        db = CacheDB(str(db_path))
        await db.init()
        await db.set_tokens("old-access", "old-refresh", 1)
        await db._db.execute("INSERT INTO activities (id, data, synced_at) VALUES (7, '{}', 0)")
        await db._db.commit()
        await db.close()

    asyncio.run(seed())
    os.chmod(db_path, 0o640)
    monkeypatch.setenv("VAULT_DB_PATH", str(db_path))
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)

    assert _run_main([], token_endpoint, _FakeBrowser()) == 0
    assert _read_vault(db_path)["access_token"] == ACCESS
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT id FROM activities").fetchall() == [(7,)]
    assert stat.S_IMODE(os.stat(db_path).st_mode) == 0o640


def test_happy_path_docker_env_file(token_endpoint, tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Strava API credentials\n"
        f'STRAVA_CLIENT_ID="{CLIENT_ID}"\n'
        f"export STRAVA_CLIENT_SECRET={CLIENT_SECRET}  # from settings\n"
        "STRAVA_ACCESS_TOKEN=your-initial-access-token\n"
        "STRAVA_REFRESH_TOKEN=your-initial-refresh-token\n"
        "# MCP_AUTH_TOKEN=your-secret-token\n"
        "MCP_AUTH_TOKEN=keep-me\n"
    )
    os.chmod(env_file, 0o644)

    assert _run_main(["--env-file", str(env_file)], token_endpoint, _FakeBrowser()) == 0

    lines = env_file.read_text().splitlines()
    assert lines == [
        "# Strava API credentials",
        f'STRAVA_CLIENT_ID="{CLIENT_ID}"',
        f"export STRAVA_CLIENT_SECRET={CLIENT_SECRET}  # from settings",
        f"STRAVA_ACCESS_TOKEN={ACCESS}",
        f"STRAVA_REFRESH_TOKEN={REFRESH}",
        "# MCP_AUTH_TOKEN=your-secret-token",
        "MCP_AUTH_TOKEN=keep-me",
    ]
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert not list(tmp_path.glob(".env.*.tmp"))
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1] == "Next step: docker compose up -d"


def test_env_file_from_example_gets_prompted_credentials(token_endpoint, tmp_path):
    """Fresh copy of .env.example: placeholders count as unset, the user is
    prompted, the secret through getpass, and both land in the file."""
    example = Path(__file__).resolve().parent.parent / ".env.example"
    env_file = tmp_path / ".env"
    env_file.write_text(example.read_text())
    asked = []

    def prompt(text):
        asked.append(("input", text))
        return CLIENT_ID

    def secret_prompt(text):
        asked.append(("getpass", text))
        return CLIENT_SECRET

    rc = _run_main(
        ["--env-file", str(env_file)],
        token_endpoint,
        _FakeBrowser(),
        prompt=prompt,
        secret_prompt=secret_prompt,
    )
    assert rc == 0
    assert [kind for kind, _ in asked] == ["input", "getpass"]
    text = env_file.read_text()
    for line in (
        f"STRAVA_CLIENT_ID={CLIENT_ID}",
        f"STRAVA_CLIENT_SECRET={CLIENT_SECRET}",
        f"STRAVA_ACCESS_TOKEN={ACCESS}",
        f"STRAVA_REFRESH_TOKEN={REFRESH}",
    ):
        assert text.splitlines().count(line) == 1
    assert "your-client-secret" not in text
    # Every optional, commented setting from the example survives.
    assert "# TOKEN_ENCRYPTION_KEY=your-fernet-key" in text


def test_env_file_created_when_missing(token_endpoint, monkeypatch, tmp_path):
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    env_file = tmp_path / ".env"
    assert _run_main(["--env-file", str(env_file)], token_endpoint, _FakeBrowser()) == 0
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert f"STRAVA_REFRESH_TOKEN={REFRESH}" in env_file.read_text().splitlines()


# --------------------------------------------------------------------------
# Refusals: each must store nothing and never reach the token endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def vault_env(monkeypatch, tmp_path):
    db_path = tmp_path / "vault.db"
    monkeypatch.setenv("VAULT_DB_PATH", str(db_path))
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    return db_path


def test_wrong_state_is_refused_and_stores_nothing(token_endpoint, vault_env, capsys):
    browser = _FakeBrowser(state="not-the-state-this-sign-in-sent")
    assert _run_main([], token_endpoint, browser) == 1

    assert token_endpoint.requests == []  # the code was never exchanged
    assert _no_tokens_stored(vault_env)
    browser.thread.join(5)
    assert "not accepted" in browser.page
    assert "Refused the redirect" in capsys.readouterr().err


def test_missing_state_is_refused(token_endpoint, vault_env):
    assert _run_main([], token_endpoint, _FakeBrowser(state=None)) == 1
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)


def test_wrong_state_refused_in_env_file_mode(token_endpoint, monkeypatch, tmp_path):
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    env_file = tmp_path / ".env"
    env_file.write_text("STRAVA_ACCESS_TOKEN=unchanged\n")
    browser = _FakeBrowser(state="forged")
    assert _run_main(["--env-file", str(env_file)], token_endpoint, browser) == 1
    assert env_file.read_text() == "STRAVA_ACCESS_TOKEN=unchanged\n"
    assert token_endpoint.requests == []


def test_denied_authorization_stores_nothing(token_endpoint, vault_env, capsys):
    # What Strava sends when the athlete clicks Cancel: error, no code.
    browser = _FakeBrowser(error="access_denied", code=None, scope=None)
    assert _run_main([], token_endpoint, browser) == 1
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)
    assert "not approved" in capsys.readouterr().err


def test_missing_activity_scope_stores_nothing(token_endpoint, vault_env, capsys):
    # The athlete unticked the private-activities box on Strava's page.
    assert _run_main([], token_endpoint, _FakeBrowser(scope="read")) == 1
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)
    assert "activity:read_all" in capsys.readouterr().err


def test_wrong_path_is_refused(token_endpoint, vault_env):
    """A correct state and code sent to any path but /callback is refused."""

    def follow_elsewhere(url):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        port = urllib.parse.urlsplit(params["redirect_uri"]).port
        q = urllib.parse.urlencode({"state": params["state"], "code": CODE, "scope": GRANTED})
        threading.Thread(
            target=lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/other?{q}", timeout=5),
            daemon=True,
        ).start()

    assert _run_main([], token_endpoint, follow_elsewhere) == 1
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)


def test_timeout_stores_nothing_and_closes_listener(token_endpoint, vault_env, capsys):
    seen = {}

    def never_returns(url):
        seen["redirect"] = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))[
            "redirect_uri"
        ]

    assert _run_main(["--timeout", "0.5"], token_endpoint, never_returns) == 1
    assert "Timed out" in capsys.readouterr().err
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)
    # The listener is gone: nothing is accepting on that port any more.
    port = urllib.parse.urlsplit(seen["redirect"]).port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_listener_takes_one_request_then_closes(token_endpoint, vault_env):
    seen = {}

    class Recording(_FakeBrowser):
        def __call__(self, url):
            seen["redirect"] = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))[
                "redirect_uri"
            ]
            return super().__call__(url)

    assert _run_main([], token_endpoint, Recording()) == 0
    port = urllib.parse.urlsplit(seen["redirect"]).port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_token_endpoint_error_stores_nothing(vault_env, capsys):
    stub = _StubTokenEndpoint(status=400, body={"message": "Bad Request"})
    try:
        assert _run_main([], stub, _FakeBrowser()) == 1
    finally:
        stub.close()
    assert _no_tokens_stored(vault_env)
    assert "HTTP 400" in capsys.readouterr().err


def test_malformed_token_response_stores_nothing(vault_env):
    # A token value carrying a newline would inject a line into an env file.
    stub = _StubTokenEndpoint(
        body={"access_token": "x\nMCP_AUTH_TOKEN=", "refresh_token": REFRESH, "expires_at": 1}
    )
    try:
        assert _run_main([], stub, _FakeBrowser()) == 1
    finally:
        stub.close()
    assert _no_tokens_stored(vault_env)


# --------------------------------------------------------------------------
# Secrets never reach output or logs
# --------------------------------------------------------------------------


def test_nothing_secret_in_output_or_logs(token_endpoint, vault_env, capfd, caplog):
    caplog.set_level("DEBUG")
    assert _run_main([], token_endpoint, _FakeBrowser()) == 0
    # A refused run too, since error paths are where secrets usually leak.
    assert _run_main([], token_endpoint, _FakeBrowser(state="forged")) == 1

    captured = capfd.readouterr()
    everything = captured.out + captured.err + caplog.text
    assert "Signed in" in captured.out  # the capture is live, not empty
    for secret in SECRETS:
        assert secret not in everything


def test_nothing_secret_in_output_env_file_mode(token_endpoint, tmp_path, capfd, caplog):
    caplog.set_level("DEBUG")
    env_file = tmp_path / ".env"
    rc = _run_main(
        ["--env-file", str(env_file)],
        token_endpoint,
        _FakeBrowser(),
        prompt=lambda _: CLIENT_ID,
        secret_prompt=lambda _: CLIENT_SECRET,
    )
    assert rc == 0
    captured = capfd.readouterr()
    everything = captured.out + captured.err + caplog.text
    assert "docker compose up -d" in captured.out
    for secret in SECRETS:
        assert secret not in everything


# --------------------------------------------------------------------------
# Guard rails
# --------------------------------------------------------------------------


def test_listener_binds_loopback_only():
    server = login._OneShotServer(0, "x")
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_unwritable_vault_fails_before_browser_opens(token_endpoint, monkeypatch, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    monkeypatch.setenv("VAULT_DB_PATH", str(locked / "vault.db"))
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    opened = []
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        assert _run_main([], token_endpoint, opened.append) == 1
    finally:
        os.chmod(locked, 0o700)
    assert opened == []


@pytest.mark.parametrize(
    "client_id,client_secret",
    [("abc", CLIENT_SECRET), (CLIENT_ID, "has space"), (CLIENT_ID, 'q"uote')],
)
def test_bad_credentials_rejected_before_browser(
    token_endpoint, monkeypatch, tmp_path, client_id, client_secret
):
    monkeypatch.setenv("STRAVA_CLIENT_ID", client_id)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", client_secret)
    opened = []
    assert _run_main(["--env-file", str(tmp_path / ".env")], token_endpoint, opened.append) == 1
    assert opened == []


def test_bad_arguments_rejected(token_endpoint):
    assert _run_main(["--port", "70000"], token_endpoint, _FakeBrowser()) == 2
    assert _run_main(["--timeout", "0"], token_endpoint, _FakeBrowser()) == 2


def test_runs_from_a_clone_with_stdlib_only():
    """README tells Docker users to run the file straight from the clone, so it
    must not import anything outside the standard library at module level."""
    script = Path(login.__file__)
    probe = (
        "import runpy, sys\n"
        f"runpy.run_path({str(script)!r}, run_name='not_main')\n"
        "third = {'dotenv', 'aiosqlite', 'cryptography', 'httpx', 'mcp', 'strava_mcp_vault'}\n"
        "print(sorted(third & set(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"

    help_run = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=30
    )
    assert help_run.returncode == 0, help_run.stderr
    assert "--env-file" in help_run.stdout
