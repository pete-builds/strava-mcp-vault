"""Tests for strava-mcp-vault-login. No network: Strava's token endpoint is a
local stub on 127.0.0.1, and the "browser" is a thread that follows the
redirect the way Strava would."""

import asyncio
import base64
import fnmatch
import gc
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import warnings
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
                posted = {k: v[0] for k, v in form.items()}
                stub.requests.append(posted)
                body = stub.body(posted) if callable(stub.body) else stub.body
                payload = json.dumps(body).encode()
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
        self.status = None

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
            try:
                with urllib.request.urlopen(target, timeout=5) as resp:
                    self.status, self.page = resp.status, resp.read().decode()
            except urllib.error.HTTPError as exc:  # a refusal is a 400 with a page
                self.status, self.page = exc.code, exc.read().decode()

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
    """Refused with a 400 and a terminal line, nothing exchanged or stored, and
    the command keeps waiting (here until a short --timeout)."""
    browser = _FakeBrowser(state="not-the-state-this-sign-in-sent")
    assert _run_main(["--timeout", "1"], token_endpoint, browser) == 1

    assert token_endpoint.requests == []  # the code was never exchanged
    assert _no_tokens_stored(vault_env)
    browser.thread.join(5)
    assert browser.status == 400
    assert "not accepted" in browser.page
    captured = capsys.readouterr()
    assert captured.out.count(login.REFUSAL_LINE) == 1
    assert "Timed out" in captured.err


def test_missing_state_is_refused(token_endpoint, vault_env, capsys):
    assert _run_main(["--timeout", "1"], token_endpoint, _FakeBrowser(state=None)) == 1
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)
    assert capsys.readouterr().out.count(login.REFUSAL_LINE) == 1


def test_wrong_state_refused_in_env_file_mode(token_endpoint, monkeypatch, tmp_path):
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    env_file = tmp_path / ".env"
    env_file.write_text("STRAVA_ACCESS_TOKEN=unchanged\n")
    browser = _FakeBrowser(state="forged")
    rc = _run_main(["--env-file", str(env_file), "--timeout", "1"], token_endpoint, browser)
    assert rc == 1
    assert env_file.read_text() == "STRAVA_ACCESS_TOKEN=unchanged\n"
    assert token_endpoint.requests == []


def test_check_callback_refuses_wrong_state_on_its_own():
    """Second line of defence: even if the listener handed over a query with
    the wrong state, the code is never returned."""
    query = {"state": ["other"], "code": [CODE], "scope": [GRANTED]}
    with pytest.raises(login.LoginError, match="Refused the redirect"):
        login._check_callback(query, "expected")
    assert login._check_callback(dict(query, state=["expected"]), "expected") == CODE


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


def _redirect_parts(url):
    """(port, state) from the authorize URL the command opened."""
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    return urllib.parse.urlsplit(params["redirect_uri"]).port, params["state"]


def _callback_target(state, **extra):
    query = {"state": state, "code": CODE, "scope": GRANTED}
    query.update(extra)
    return f"{login.CALLBACK_PATH}?{urllib.parse.urlencode(query)}"


def _raw_get(port, target, method="GET", host="localhost:{port}", extra="", pieces=1):
    """One HTTP request on its own connection, as a browser would send it to
    http://localhost:PORT. Returns the status line, or "" if the connection was
    refused or closed without a response. host=None sends no Host header;
    pieces > 1 sends the request in that many separate writes."""
    host_line = "" if host is None else f"Host: {host.format(port=port)}\r\n"
    raw = f"{method} {target} HTTP/1.1\r\n{host_line}{extra}\r\n".encode("latin-1")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            size = -(-len(raw) // pieces)
            for i in range(0, len(raw), size):
                sock.sendall(raw[i : i + size])
                if pieces > 1:
                    time.sleep(0.05)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError:
        return ""
    return data.split(b"\r\n", 1)[0].decode()


@pytest.fixture
def listeners(monkeypatch):
    """Every listening socket the command opens, held here so garbage
    collection cannot close one behind the test's back."""
    opened = []
    real = login._open_listener

    def recording(port):
        sock = real(port)
        opened.append(sock)
        return sock

    monkeypatch.setattr(login, "_open_listener", recording)
    return opened


def test_stray_path_is_ignored_not_trusted(token_endpoint, vault_env, capsys):
    """A correct state and code sent to any path but /callback is answered
    404 and ignored: it neither completes nor ends the sign-in."""
    statuses = []

    def follow_elsewhere(url):
        port, state = _redirect_parts(url)
        target = _callback_target(state).replace(login.CALLBACK_PATH, "/other", 1)
        statuses.append(_raw_get(port, target))

    def browser(url):
        threading.Thread(target=follow_elsewhere, args=(url,), daemon=True).start()

    assert _run_main(["--timeout", "1"], token_endpoint, browser) == 1
    assert statuses == ["HTTP/1.1 404 Not Found"]
    assert "Timed out" in capsys.readouterr().err
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)


def test_timeout_stores_nothing_and_closes_listener(token_endpoint, vault_env, capsys, listeners):
    assert _run_main(["--timeout", "0.5"], token_endpoint, lambda url: None) == 1
    assert "Timed out" in capsys.readouterr().err
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)
    assert len(listeners) == 1
    assert listeners[0].fileno() == -1  # closed by the command, not by GC


def test_listener_closes_after_the_callback(token_endpoint, vault_env, listeners):
    assert _run_main([], token_endpoint, _FakeBrowser()) == 0
    assert len(listeners) == 1
    assert listeners[0].fileno() == -1


def test_idle_and_early_closed_connections_do_not_block_redirect(token_endpoint, vault_env):
    """Browsers open spare connections and leave them idle. Neither an idle
    connection nor one closed without a request may use up the sign-in."""
    held = []
    done = threading.Event()

    def browser(url):
        port, state = _redirect_parts(url)
        held.append(socket.create_connection(("127.0.0.1", port), timeout=5))  # idle
        socket.create_connection(("127.0.0.1", port), timeout=5).close()  # closed early

        def follow():
            time.sleep(0.2)
            _raw_get(port, _callback_target(state))
            done.set()

        threading.Thread(target=follow, daemon=True).start()

    started = time.monotonic()
    try:
        assert _run_main(["--timeout", "8"], token_endpoint, browser) == 0
    finally:
        for sock in held:
            sock.close()
    assert time.monotonic() - started < 5
    assert done.wait(5)
    assert _read_vault(vault_env)["access_token"] == ACCESS


def test_stray_favicon_before_redirect_does_not_break_it(token_endpoint, vault_env):
    statuses = []

    def browser(url):
        port, state = _redirect_parts(url)

        def follow():
            statuses.append(_raw_get(port, "/favicon.ico"))
            statuses.append(_raw_get(port, _callback_target(state)))

        browser.thread = threading.Thread(target=follow, daemon=True)
        browser.thread.start()

    assert _run_main(["--timeout", "8"], token_endpoint, browser) == 0
    browser.thread.join(5)
    assert statuses == ["HTTP/1.1 404 Not Found", "HTTP/1.1 200 OK"]
    assert _read_vault(vault_env)["access_token"] == ACCESS


def test_flood_of_connections_cannot_crowd_out_redirect(token_endpoint, vault_env):
    """More open connections than the cap, some mid-request, then the real
    redirect: the oldest are dropped and the sign-in still completes."""
    held = []

    def browser(url):
        port, state = _redirect_parts(url)

        def flood_then_follow():
            for i in range(login.MAX_OPEN_CONNECTIONS + 8):
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                if i % 2:
                    sock.sendall(b"GET /callback?state=partial")  # never finished
                held.append(sock)
            time.sleep(0.3)
            _raw_get(port, _callback_target(state))

        threading.Thread(target=flood_then_follow, daemon=True).start()

    try:
        assert _run_main(["--timeout", "8"], token_endpoint, browser) == 0
    finally:
        for sock in held:
            sock.close()
    assert _read_vault(vault_env)["access_token"] == ACCESS


def test_oldest_connection_closed_past_the_cap(token_endpoint, vault_env, monkeypatch):
    """Open connections are capped (file descriptors are finite). Past the
    cap the oldest is closed by the listener, and the redirect still lands."""
    monkeypatch.setattr(login, "MAX_OPEN_CONNECTIONS", 2)
    seen = {}

    def browser(url):
        port, state = _redirect_parts(url)

        def run():
            first = socket.create_connection(("127.0.0.1", port), timeout=5)
            others = []
            for _ in range(2):
                time.sleep(0.1)
                others.append(socket.create_connection(("127.0.0.1", port), timeout=5))
            first.settimeout(3)
            try:
                seen["first"] = first.recv(1)  # b"" means the listener closed it
            except OSError as exc:
                seen["first"] = exc
            _raw_get(port, _callback_target(state))
            for sock in [first, *others]:
                sock.close()

        browser.thread = threading.Thread(target=run, daemon=True)
        browser.thread.start()

    assert _run_main(["--timeout", "8"], token_endpoint, browser) == 0
    browser.thread.join(5)
    assert seen["first"] == b""
    assert _read_vault(vault_env)["access_token"] == ACCESS


def test_slow_byte_connection_cannot_outlive_deadline(token_endpoint, vault_env, capsys):
    """A local program sending one byte at a time, never finishing its
    request, must not hold the sign-in open past --timeout."""
    stop = threading.Event()

    def browser(url):
        port, state = _redirect_parts(url)

        def dribble():
            payload = f"GET {_callback_target(state)} HTTP/1.1\r\n".encode()
            give_up_at = time.monotonic() + 6
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                    while not stop.is_set() and time.monotonic() < give_up_at:
                        sock.sendall(payload[:1])
                        payload = payload[1:] or b"X-Pad: y\r\n"
                        time.sleep(0.05)
            except OSError:
                return

        threading.Thread(target=dribble, daemon=True).start()

    started = time.monotonic()
    try:
        rc = _run_main(["--timeout", "1"], token_endpoint, browser)
    finally:
        stop.set()
    assert rc == 1
    assert time.monotonic() - started < 3
    assert "Timed out" in capsys.readouterr().err
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)


class _Script:
    """A browser stand-in that runs a list of raw requests in order."""

    def __init__(self, *steps):
        self.steps = steps
        self.results = []
        self.thread = None

    def __call__(self, url):
        port, state = _redirect_parts(url)

        def run():
            for step in self.steps:
                self.results.append(step(port, state))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self):
        self.thread.join(10)
        return self.results


def _real_redirect(port, state):
    return _raw_get(port, _callback_target(state))


def _q(*pairs):
    return f"{login.CALLBACK_PATH}?{urllib.parse.urlencode(pairs)}"


def test_wrong_callback_does_not_end_the_sign_in(token_endpoint, vault_env, capsys):
    """A wrong state is refused, but it no longer ends the sign-in: any web page
    could otherwise kill it with one request. The right redirect still lands."""
    browser = _Script(
        lambda port, state: _raw_get(
            port, _q(("state", "a-guess"), ("code", "guess"), ("scope", GRANTED))
        ),
        _real_redirect,
    )
    assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0
    assert browser.join() == ["HTTP/1.1 400 Bad Request", "HTTP/1.1 200 OK"]
    assert [r["code"] for r in token_endpoint.requests] == [CODE]
    assert capsys.readouterr().out.count(login.REFUSAL_LINE) == 1
    assert _read_vault(vault_env)["access_token"] == ACCESS


def test_too_many_refusals_give_up(token_endpoint, vault_env, capsys, listeners):
    spray = [
        lambda port, state: _raw_get(port, _q(("state", "x"), ("code", "c")))
    ] * login.MAX_REFUSALS
    browser = _Script(*spray)
    started = time.monotonic()
    assert _run_main(["--timeout", "8"], token_endpoint, browser) == 1
    assert time.monotonic() - started < 5
    assert browser.join() == ["HTTP/1.1 400 Bad Request"] * login.MAX_REFUSALS
    captured = capsys.readouterr()
    assert captured.out.count(login.REFUSAL_LINE) == login.MAX_REFUSALS
    assert "Another program or web page on this computer" in captured.err
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)
    assert listeners[0].fileno() == -1


# --------------------------------------------------------------------------
# Request parsing: exact, and never a crash
# --------------------------------------------------------------------------


def test_unparseable_targets_do_not_crash(token_endpoint, vault_env):
    """urlsplit raised ValueError on these, which escaped as a traceback."""
    browser = _Script(
        lambda port, state: _raw_get(port, "//[x"),
        lambda port, state: _raw_get(port, "http://[::1/callback"),
        _real_redirect,
    )
    assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0
    assert browser.join() == ["HTTP/1.1 404 Not Found"] * 2 + ["HTTP/1.1 200 OK"]


@pytest.mark.parametrize(
    "shape",
    [
        "http://evil.example{path}",
        "http:{path}",
        "/call\tback{query}",
        "\x01{path}",
        "/callbackX{query}",
        "{path_only}/x{query}",
    ],
    ids=[
        "absolute-url",
        "scheme-no-slashes",
        "tab-in-path",
        "leading-control",
        "suffix",
        "subpath",
    ],
)
def test_lookalike_paths_get_404(token_endpoint, vault_env, shape):
    """Each carries the RIGHT state and a code, so if it were treated as
    /callback the sign-in would finish on it. It must be a 404 instead."""

    def lookalike(port, state):
        query = "?" + urllib.parse.urlencode(
            {"state": state, "code": "lookalike", "scope": GRANTED}
        )
        target = shape.format(
            path=login.CALLBACK_PATH + query, query=query, path_only=login.CALLBACK_PATH
        )
        return _raw_get(port, target)

    browser = _Script(lookalike, _real_redirect)
    assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0
    assert browser.join() == ["HTTP/1.1 404 Not Found", "HTTP/1.1 200 OK"]
    assert [r["code"] for r in token_endpoint.requests] == [CODE]


def test_only_get_is_accepted_on_callback(token_endpoint, vault_env):
    browser = _Script(
        lambda port, state: _raw_get(port, _callback_target(state, code="posted"), method="POST"),
        _real_redirect,
    )
    assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0
    assert browser.join() == ["HTTP/1.1 404 Not Found", "HTTP/1.1 200 OK"]
    assert [r["code"] for r in token_endpoint.requests] == [CODE]


@pytest.mark.parametrize(
    "host", ["evil.example:{port}", None, "localhost:1", "localhost:{port}\r\nHost: x"]
)
def test_callback_needs_a_loopback_host_header(token_endpoint, vault_env, host):
    """A page on another name that resolves to 127.0.0.1 (DNS rebinding) sends
    its own Host header. Only localhost:PORT and 127.0.0.1:PORT are served."""
    browser = _Script(
        lambda port, state: _raw_get(port, _callback_target(state, code="rebound"), host=host),
        _real_redirect,
    )
    assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0
    assert browser.join() == ["HTTP/1.1 404 Not Found", "HTTP/1.1 200 OK"]
    assert [r["code"] for r in token_endpoint.requests] == [CODE]


def test_127_0_0_1_host_is_served(token_endpoint, vault_env):
    browser = _Script(
        lambda port, state: _raw_get(port, _callback_target(state), host="127.0.0.1:{port}")
    )
    assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0


@pytest.mark.parametrize(
    "states",
    [("GOOD", "GOOD"), ("BAD", "GOOD"), ("GOOD", "BAD")],
    ids=["same", "bad-first", "good-first"],
)
def test_duplicate_state_is_refused(token_endpoint, vault_env, states):
    def duplicated(port, state):
        values = [state if s == "GOOD" else "bad" for s in states]
        pairs = [("state", v) for v in values] + [("code", "dup"), ("scope", GRANTED)]
        return _raw_get(port, _q(*pairs))

    browser = _Script(duplicated, _real_redirect)
    assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0
    assert browser.join() == ["HTTP/1.1 400 Bad Request", "HTTP/1.1 200 OK"]
    assert [r["code"] for r in token_endpoint.requests] == [CODE]


def test_request_in_five_pieces_is_accepted(token_endpoint, vault_env):
    browser = _Script(lambda port, state: _raw_get(port, _callback_target(state), pieces=5))
    assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0
    assert browser.join() == ["HTTP/1.1 200 OK"]


def test_oversized_request_is_dropped(token_endpoint, vault_env, monkeypatch):
    monkeypatch.setattr(login, "MAX_REQUEST_BYTES", 64)
    seen = {}

    def oversized(port, state):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"GET /callback?" + b"a" * 200)  # no end of headers
            sock.settimeout(2)
            try:
                seen["read"] = sock.recv(1)  # b"" means the listener dropped it
            except OSError as exc:
                seen["read"] = exc
        return "sent"

    browser = _Script(oversized, _real_redirect)
    assert _run_main(["--timeout", "8"], token_endpoint, browser) == 0
    assert browser.join() == ["sent", "HTTP/1.1 200 OK"]
    assert seen["read"] == b""


def test_large_cookies_do_not_drop_the_redirect(token_endpoint, vault_env):
    """Developers' localhost cookies can run to tens of kilobytes."""
    cookie = "Cookie: " + "; ".join(f"c{i}={'v' * 90}" for i in range(400)) + "\r\n"
    assert 32768 < len(cookie) < login.MAX_REQUEST_BYTES
    browser = _Script(lambda port, state: _raw_get(port, _callback_target(state), extra=cookie))
    assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0
    assert browser.join() == ["HTTP/1.1 200 OK"]


def test_held_connections_are_closed_on_exit(token_endpoint, vault_env):
    """Idle connections still open when the sign-in ends are closed by the
    command itself, not left for garbage collection."""
    held = []

    def hold_two_then_redirect(port, state):
        for _ in range(2):
            held.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        time.sleep(0.2)
        return _real_redirect(port, state)

    browser = _Script(hold_two_then_redirect)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        assert _run_main(["--timeout", "5"], token_endpoint, browser) == 0
        browser.join()
        reads = []
        for sock in held:
            sock.settimeout(2)
            try:
                reads.append(sock.recv(1))
            except OSError as exc:
                reads.append(exc)
            sock.close()
        gc.collect()
    assert reads == [b"", b""]
    unclosed = [
        w for w in caught if issubclass(w.category, ResourceWarning) and "socket" in str(w.message)
    ]
    assert unclosed == []


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
    assert _run_main(["--timeout", "1"], token_endpoint, _FakeBrowser(state="forged")) == 1

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


def test_token_endpoint_error_leaks_nothing(vault_env, capfd, caplog):
    """Strava's error body is not relayed. Here the stub echoes the whole
    request back, secret and code included, and none of it may surface in
    output, logs, the exception, or its traceback."""
    caplog.set_level("DEBUG")
    stub = _StubTokenEndpoint(
        status=400,
        body=lambda posted: {
            "message": f"Bad Request {posted}",
            "errors": [{"field": posted.get("client_secret"), "code": posted.get("code")}],
            "echo": posted,
        },
    )
    try:
        assert _run_main([], stub, _FakeBrowser()) == 1
        with pytest.raises(login.LoginError) as info:
            login.exchange_code(CLIENT_ID, CLIENT_SECRET, CODE, stub.url)
    finally:
        stub.close()
    # The stub really held the secrets, so their absence below means something.
    assert stub.requests[0]["client_secret"] == CLIENT_SECRET
    assert stub.requests[0]["code"] == CODE

    exc = info.value
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    captured = capfd.readouterr()
    everything = captured.out + captured.err + caplog.text + rendered + repr(exc)
    assert "HTTP 400" in captured.err
    assert exc.__cause__ is None and exc.__suppress_context__
    for secret in SECRETS:
        assert secret not in everything


# --------------------------------------------------------------------------
# Encryption is reported only when it happened
# --------------------------------------------------------------------------


def test_invalid_encryption_key_refused_before_browser(token_endpoint, vault_env, monkeypatch):
    """The server's encryption module falls back to plain text on a bad key.
    The command must refuse instead of storing plain text."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    opened = []
    assert _run_main(["--timeout", "2"], token_endpoint, opened.append) == 1
    assert opened == []
    assert token_endpoint.requests == []
    assert _no_tokens_stored(vault_env)


def test_invalid_encryption_key_in_env_file_refused(token_endpoint, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    env_file = tmp_path / ".env"
    original = 'TOKEN_ENCRYPTION_KEY="abc123"\nSTRAVA_ACCESS_TOKEN=unchanged\n'
    env_file.write_text(original)
    opened = []
    assert (
        _run_main(["--env-file", str(env_file), "--timeout", "2"], token_endpoint, opened.append)
        == 1
    )
    assert opened == []
    assert env_file.read_text() == original
    assert "TOKEN_ENCRYPTION_KEY" in capsys.readouterr().err


def test_valid_encryption_key_in_env_file_is_accepted(token_endpoint, monkeypatch, tmp_path):
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    env_file = tmp_path / ".env"
    env_file.write_text(f"TOKEN_ENCRYPTION_KEY='{Fernet.generate_key().decode()}'\n")
    assert _run_main(["--env-file", str(env_file)], token_endpoint, _FakeBrowser()) == 0


def test_encryption_reported_from_what_happened(token_endpoint, vault_env, monkeypatch, capsys):
    """Even with a key that looks valid, the success message must come from the
    encryption module's real state after the write, not from the environment."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

    def init_without_encryption():
        enc._initialized = True
        enc._fernet = None

    monkeypatch.setattr(enc, "_init", init_without_encryption)
    rc = _run_main([], token_endpoint, _FakeBrowser())
    captured = capsys.readouterr()
    assert "encrypted with" not in captured.out + captured.err
    assert "NOT encrypted" in captured.err
    assert rc == 1
    (row,) = _raw_token_rows(vault_env)
    assert row == (ACCESS, REFRESH)  # what really happened: plain text


_REAL_KEY = Fernet.generate_key().decode()

# (key, what the stdlib check must answer). The "=" insertions at positions
# 3, 19 and 39 are keys Python 3.14's base64 decodes to 32 bytes while the
# server's Python 3.13 Fernet rejects them; "junk after padding" goes the other
# way. A shape check makes the answer the same on every Python.
_KEY_CASES = [
    (_REAL_KEY, True),
    (_REAL_KEY[:-1], False),
    (_REAL_KEY.replace("=", ""), False),
    ("!" + _REAL_KEY, False),
    ("not-a-valid-fernet-key", False),
    ("abc123", False),
    ("\u00e9" * 44, False),
    (base64.urlsafe_b64encode(b"x" * 31).decode(), False),
    (base64.urlsafe_b64encode(b"x" * 33).decode(), False),
    (_REAL_KEY[:3] + "=" + _REAL_KEY[3:], False),
    (_REAL_KEY[:19] + "=" + _REAL_KEY[19:], False),
    (_REAL_KEY[:39] + "=" + _REAL_KEY[39:], False),
    (_REAL_KEY + "AAAA", False),
    (_REAL_KEY + "=", False),
    (" " + _REAL_KEY, False),
    (_REAL_KEY + "\n", False),
]
_KEY_IDS = [
    "valid",
    "truncated",
    "padding-stripped",
    "junk-prefix",
    "plain-words",
    "short",
    "non-ascii",
    "31-bytes",
    "33-bytes",
    "eq-at-3",
    "eq-at-19",
    "eq-at-39",
    "junk-after-padding",
    "double-padding",
    "leading-space",
    "trailing-newline",
]


@pytest.mark.parametrize("key,expected", _KEY_CASES, ids=_KEY_IDS)
def test_stdlib_key_check(key, expected):
    assert login._fernet_key_is_valid(key) is expected
    if expected:
        # Sound: whatever the stdlib check accepts, Fernet accepts.
        Fernet(key.encode())


def test_stdlib_key_check_accepts_every_generated_key():
    keys = [Fernet.generate_key().decode() for _ in range(500)]
    assert all(login._fernet_key_is_valid(k) for k in keys)


# --------------------------------------------------------------------------
# Env file writes
# --------------------------------------------------------------------------


def test_gitignore_covers_env_temp_files():
    """The atomic write's temp file sits next to .env; a crash must not leave
    something `git add .` would pick up."""
    gitignore = Path(__file__).resolve().parent.parent / ".gitignore"
    patterns = [ln.strip() for ln in gitignore.read_text().splitlines() if ln.strip()]
    name = ".env.k2j3h4x9.tmp"  # mkstemp(prefix=".env.", suffix=".tmp") shape
    assert name.startswith(".env.") and name.endswith(".tmp")
    assert any(fnmatch.fnmatch(name, p) for p in patterns)


def test_env_write_failure_leaves_no_temp_and_keeps_original(
    token_endpoint, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("STRAVA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", CLIENT_SECRET)
    env_file = tmp_path / ".env"
    env_file.write_text("STRAVA_ACCESS_TOKEN=unchanged\n")

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(login.os, "replace", failing_replace)
    assert _run_main(["--env-file", str(env_file)], token_endpoint, _FakeBrowser()) == 1
    assert env_file.read_text() == "STRAVA_ACCESS_TOKEN=unchanged\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]
    err = capsys.readouterr().err
    assert "Could not write" in err
    for secret in SECRETS:
        assert secret not in err


# --------------------------------------------------------------------------
# Guard rails
# --------------------------------------------------------------------------


def test_listener_binds_loopback_only():
    sock = login._open_listener(0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


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


@pytest.mark.parametrize("value", ["inf", "nan", "-inf"])
def test_non_finite_timeout_rejected(token_endpoint, vault_env, value):
    opened = []

    def browser(url):
        opened.append(url)
        _FakeBrowser()(url)  # complete the sign-in if the check is missing

    assert _run_main([f"--timeout={value}"], token_endpoint, browser) == 2
    assert opened == []


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


def test_stdlib_smoke_script_passes():
    """tests/stdlib_smoke.py is what CI runs on Python 3.9 with no packages.
    Run it here too, isolated (-I), so it stays green on every Python."""
    script = Path(__file__).resolve().parent / "stdlib_smoke.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all checks passed" in result.stdout
