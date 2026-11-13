"""One-command Strava sign-in: ``strava-mcp-vault-login``.

Replaces the manual setup step of building an authorize URL, copying a code out
of the browser address bar, and pasting tokens by hand. Run it on a computer
with a web browser:

1. It reads the Strava client id and secret from the environment (or the env
   file given with ``--env-file``), and prompts for whichever is missing. The
   secret prompt does not echo.
2. It starts a one-shot listener bound to 127.0.0.1 only, on a free port (or
   ``--port``).
3. It opens the browser at Strava's authorize URL with a random ``state``.
4. It accepts exactly one request. Anything that is not a ``/callback`` whose
   ``state`` matches is refused, and nothing is stored. The listener closes
   after that one request or after ``--timeout`` seconds, whichever is first.
5. It exchanges the code for tokens and stores them where the server will look:

   * Default: the vault database at ``VAULT_DB_PATH``, written through
     ``CacheDB.set_tokens``, the same call the server uses on refresh, so
     ``TOKEN_ENCRYPTION_KEY`` encryption applies exactly as it does there.
   * ``--env-file .env``: the Docker layout. The compose file keeps the vault in
     a named volume the host cannot write safely, and the server seeds that
     volume from ``STRAVA_ACCESS_TOKEN`` and ``STRAVA_REFRESH_TOKEN`` on first
     boot. This mode writes those two keys into the env file (mode 0600) and
     prints one next step: ``docker compose up -d``.

Tokens, the authorization code, and the client secret are never printed or
logged. The callback handler's request logging is switched off because the
default handler writes the full request line, code included, to stderr.

The env-file mode imports nothing outside the standard library, so it also runs
straight from a clone with no install: ``python3 src/strava_mcp_vault/login.py
--env-file .env``. Keep the top-level imports stdlib-only;
``tests/test_login.py`` fails if they stop being so.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import socketserver
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"

# What the server needs today. Every Strava call it makes is a GET on the
# athlete, their activities, streams, photos, gear, or stats. `read` covers the
# profile; `activity:read_all` covers activities, including Only You ones.
# Without it every activity call returns 401 activity:read_permission missing.
SCOPES = "read,activity:read_all"
REQUIRED_SCOPE = "activity:read_all"

CALLBACK_PATH = "/callback"
LISTEN_HOST = "127.0.0.1"
# Strava's own getting-started guide uses http://localhost/... with the
# Authorization Callback Domain set to "localhost". The browser resolves it to
# the loopback address the listener is bound to.
REDIRECT_HOST = "localhost"
DEFAULT_TIMEOUT = 300
# Per-connection read timeout, so a client that connects and sends nothing
# cannot hold the one-shot listener open past the overall deadline.
REQUEST_READ_TIMEOUT = 10
TOKEN_REQUEST_TIMEOUT = 30
DEFAULT_DB_PATH = "/app/data/vault.db"  # same default as server._startup

ENV_CLIENT_ID = "STRAVA_CLIENT_ID"
ENV_CLIENT_SECRET = "STRAVA_CLIENT_SECRET"
ENV_ACCESS_TOKEN = "STRAVA_ACCESS_TOKEN"
ENV_REFRESH_TOKEN = "STRAVA_REFRESH_TOKEN"

# Values copied verbatim from .env.example. Treated as "not set".
_PLACEHOLDERS = {"", "your-client-id", "your-client-secret"}

# Everything written into an env file must match this. Strava ids are digits
# and secrets and tokens are hex, so this refuses nothing real, and it means a
# value can never carry a newline, quote, or "#" into the file.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._-]{1,512}$")

_SUCCESS_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>strava-mcp-vault</title>"
    b"<p>Strava sign-in received. Return to your terminal to finish.</p>"
)
_REFUSED_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>strava-mcp-vault</title>"
    b"<p>This sign-in was not accepted. Return to your terminal.</p>"
)


class LoginError(Exception):
    """A sign-in failure with a message safe to show the user."""


# --------------------------------------------------------------------------
# Authorize URL
# --------------------------------------------------------------------------


def build_authorize_url(
    client_id: str, redirect_uri: str, state: str, authorize_url: str = AUTHORIZE_URL
) -> str:
    """Strava's documented authorize parameters, nothing else."""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        # force: always show the consent page, so the person signing in sees
        # and can check the scopes being granted.
        "approval_prompt": "force",
        "scope": SCOPES,
        "state": state,
    }
    return f"{authorize_url}?{urllib.parse.urlencode(params)}"


# --------------------------------------------------------------------------
# One-shot loopback listener
# --------------------------------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    timeout = REQUEST_READ_TIMEOUT

    def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urllib.parse.urlsplit(self.path)
        self.server.received = {
            "path": parsed.path,
            "query": urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
        }
        page = _SUCCESS_PAGE if _state_matches(self.server) else _REFUSED_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by base class
        # The default writes the request line to stderr, and the request line
        # carries the authorization code. Log nothing.
        return


class _OneShotServer(socketserver.TCPServer):
    # TCPServer rather than http.server.HTTPServer: HTTPServer.server_bind does
    # a reverse DNS lookup of the bind address, which can stall for seconds.
    allow_reuse_address = False

    def __init__(self, port: int, expected_state: str):
        self.expected_state = expected_state
        self.received: dict | None = None
        self.got_connection = False
        super().__init__((LISTEN_HOST, port), _CallbackHandler)

    def verify_request(self, request, client_address):
        self.got_connection = True
        return True


def _single(query: dict, name: str) -> str | None:
    values = query.get(name)
    if not values or len(values) != 1:
        return None
    return values[0]


def _state_matches(server: _OneShotServer) -> bool:
    received = server.received
    if received is None or received["path"] != CALLBACK_PATH:
        return False
    state = _single(received["query"], "state")
    if state is None:
        return False
    return secrets.compare_digest(state.encode(), server.expected_state.encode())


def _check_callback(server: _OneShotServer) -> str:
    """Return the authorization code, or raise LoginError. Order matters:
    the state is checked before anything else in the redirect is believed."""
    if server.received is None:
        if server.got_connection:
            raise LoginError("The sign-in redirect could not be read. Nothing was stored.")
        raise LoginError(
            "Timed out waiting for Strava to send you back. Nothing was stored. "
            "Run the command again."
        )
    if not _state_matches(server):
        raise LoginError(
            "Refused the redirect: it did not carry this sign-in's state value, so it "
            "may not have come from the sign-in you started. Nothing was stored."
        )
    query = server.received["query"]
    error = _single(query, "error")
    if error is not None:
        if error == "access_denied":
            raise LoginError("Strava access was not approved. Nothing was stored.")
        raise LoginError("Strava returned an error instead of a code. Nothing was stored.")
    code = _single(query, "code")
    if not code:
        raise LoginError("Strava's redirect had no authorization code. Nothing was stored.")
    granted = set(re.split(r"[ ,]+", _single(query, "scope") or ""))
    if REQUIRED_SCOPE not in granted:
        raise LoginError(
            f"The {REQUIRED_SCOPE} permission was not granted, and the server cannot read "
            "activities without it. Run the command again and leave every box ticked on "
            "Strava's page. Nothing was stored."
        )
    return code


# --------------------------------------------------------------------------
# Token exchange
# --------------------------------------------------------------------------


def exchange_code(client_id: str, client_secret: str, code: str, token_url: str) -> dict:
    """POST the code to Strava's token endpoint. Returns access_token,
    refresh_token, expires_at, and the athlete's first name if Strava sent it."""
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
    ).encode()
    request = urllib.request.Request(token_url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=TOKEN_REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # Status only. The body is Strava's, and there is no reason to relay it.
        raise LoginError(
            f"Strava refused the code exchange (HTTP {exc.code}). Check the client id and "
            "secret, then run the command again. Nothing was stored."
        ) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise LoginError(
            "Could not complete the code exchange with Strava. Nothing was stored."
        ) from None

    if not isinstance(payload, dict):
        raise LoginError("Strava's token response was not understood. Nothing was stored.")
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires_at = payload.get("expires_at")
    if not (
        isinstance(access, str)
        and _SAFE_VALUE.match(access)
        and isinstance(refresh, str)
        and _SAFE_VALUE.match(refresh)
        and isinstance(expires_at, int)
        and not isinstance(expires_at, bool)
    ):
        raise LoginError("Strava's token response was not understood. Nothing was stored.")
    athlete = payload.get("athlete")
    firstname = athlete.get("firstname") if isinstance(athlete, dict) else None
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "firstname": firstname if isinstance(firstname, str) else None,
    }


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def _db_preflight(db_path: str) -> None:
    """Fail before the browser opens, not after the user has signed in."""
    try:
        import aiosqlite  # noqa: F401
    except ImportError:
        raise LoginError(
            "Writing the vault database needs the package installed (pip install .). "
            "For the Docker setup, use --env-file .env instead."
        ) from None
    directory = os.path.dirname(os.path.abspath(db_path))
    try:
        os.makedirs(directory, exist_ok=True)  # the server does the same at startup
    except OSError:
        raise LoginError(
            f"Cannot create {directory}. Set VAULT_DB_PATH to the file your server uses "
            "(for example ./data/vault.db), or for Docker use --env-file .env."
        ) from None
    target = db_path if os.path.exists(db_path) else directory
    if not os.access(target, os.W_OK):
        raise LoginError(f"Cannot write to {target}. Nothing was stored.")


async def _write_db(db_path: str, tokens: dict) -> None:
    from strava_mcp_vault.cache.db import CacheDB

    db = CacheDB(db_path)
    await db.init()
    try:
        await db.set_tokens(tokens["access_token"], tokens["refresh_token"], tokens["expires_at"])
    finally:
        await db.close()


def store_in_vault(db_path: str, tokens: dict) -> bool:
    """Write tokens through CacheDB.set_tokens. Returns True if encrypted."""
    import asyncio
    import logging

    if not os.path.exists(db_path):
        # A vault this command creates is readable by its owner only. SQLite
        # gives its journal files the same mode as the database file. An
        # existing vault keeps whatever mode it already has.
        fd = os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    # aiosqlite logs every statement WITH its parameters at DEBUG, which here
    # means the tokens. Nothing in this command turns DEBUG on, but hold the
    # line anyway: that logger stays quiet while the tokens pass through it.
    sql_logger = logging.getLogger("aiosqlite")
    previous_level = sql_logger.level
    sql_logger.setLevel(logging.WARNING)
    try:
        asyncio.run(_write_db(db_path, tokens))
    finally:
        sql_logger.setLevel(previous_level)
    return bool(os.getenv("TOKEN_ENCRYPTION_KEY"))


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    # Unquoted: an inline comment starts at " #".
    return value.split(" #", 1)[0].strip()


def _env_key_pattern(key: str) -> re.Pattern:
    return re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=(.*)$")


def read_env_file(env_path: str) -> dict:
    """Read KEY=VALUE pairs. Only the keys this command needs are looked up."""
    if not os.path.exists(env_path):
        return {}
    with open(env_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    values = {}
    for key in (ENV_CLIENT_ID, ENV_CLIENT_SECRET):
        pattern = _env_key_pattern(key)
        for line in lines:
            match = pattern.match(line)
            if match:
                values[key] = _parse_env_value(match.group(1))  # last one wins
    return values


def _env_preflight(env_path: str) -> None:
    directory = os.path.dirname(os.path.abspath(env_path))
    if not os.path.isdir(directory) or not os.access(directory, os.W_OK):
        raise LoginError(f"Cannot write to {directory}. Nothing was stored.")
    if os.path.isdir(env_path):
        raise LoginError(f"{env_path} is a directory, not an env file.")
    if os.path.exists(env_path) and not os.access(env_path, os.R_OK | os.W_OK):
        raise LoginError(f"Cannot read and write {env_path}. Nothing was stored.")


def update_env_file(env_path: str, updates: dict) -> None:
    """Set each key in the env file, keeping every other line as it was.

    Written to a temporary file in the same directory (created mode 0600) and
    renamed over the original, so a crash never leaves a half-written file.
    """
    for value in updates.values():
        if not _SAFE_VALUE.match(value):
            raise LoginError("Refusing to write an unexpected value into the env file.")
    real_path = os.path.realpath(env_path)
    lines = []
    if os.path.exists(real_path):
        with open(real_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    for key, value in updates.items():
        pattern = _env_key_pattern(key)
        replaced = False
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{key}={value}"
                replaced = True
        if not replaced:
            lines.append(f"{key}={value}")
    directory = os.path.dirname(real_path)
    fd, tmp_path = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, real_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return None if value in _PLACEHOLDERS else value


def resolve_credentials(file_values: dict, prompt=input, secret_prompt=getpass.getpass):
    """Environment first, then the env file, then a prompt. Returns
    (client_id, client_secret, prompted) where prompted says whether either
    value came from the keyboard."""
    client_id = _clean(os.getenv(ENV_CLIENT_ID)) or _clean(file_values.get(ENV_CLIENT_ID))
    client_secret = _clean(os.getenv(ENV_CLIENT_SECRET)) or _clean(
        file_values.get(ENV_CLIENT_SECRET)
    )
    prompted = False
    if not client_id:
        client_id = _clean(prompt("Strava Client ID: "))
        prompted = True
    if not client_secret:
        # getpass: typed characters are not echoed to the terminal.
        client_secret = _clean(secret_prompt("Strava Client Secret (not shown): "))
        prompted = True
    if not client_id or not client_id.isdigit():
        raise LoginError("The Strava Client ID is a number, shown on strava.com/settings/api.")
    if not client_secret or not _SAFE_VALUE.match(client_secret):
        raise LoginError(
            "That Client Secret does not look right. Copy it from strava.com/settings/api."
        )
    return client_id, client_secret, prompted


# --------------------------------------------------------------------------
# Flow
# --------------------------------------------------------------------------


def run_login(
    *,
    client_id: str,
    client_secret: str,
    port: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
    open_browser=webbrowser.open,
    token_url: str = TOKEN_URL,
    authorize_url: str = AUTHORIZE_URL,
    out=None,
) -> dict:
    """Run the browser flow and return the tokens. Stores nothing itself."""
    out = out or sys.stdout
    state = secrets.token_urlsafe(32)
    try:
        server = _OneShotServer(port, state)
    except OSError:
        raise LoginError(
            f"Could not listen on {LISTEN_HOST}:{port}. Choose another port with --port."
        ) from None
    try:
        bound_port = server.server_address[1]
        redirect_uri = f"http://{REDIRECT_HOST}:{bound_port}{CALLBACK_PATH}"
        url = build_authorize_url(client_id, redirect_uri, state, authorize_url)
        print("Opening Strava in your browser to approve access.", file=out)
        print("If it does not open, visit this address on this computer:", file=out)
        print(f"  {url}", file=out)
        print(f"Waiting up to {int(timeout)} seconds...", file=out)
        out.flush()
        try:
            open_browser(url)
        except Exception:  # noqa: BLE001 - the printed address is the fallback
            pass
        server.timeout = timeout
        server.handle_request()  # exactly one request, or the timeout
    finally:
        server.server_close()
    code = _check_callback(server)
    return exchange_code(client_id, client_secret, code, token_url)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strava-mcp-vault-login",
        description=(
            "Sign in to Strava in your browser and store the tokens where strava-mcp-vault "
            "reads them. Run it on a computer with a web browser."
        ),
    )
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        help=(
            "Docker setup: write the tokens into this env file (the one docker-compose.yml "
            "reads, normally .env) instead of the vault database."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port for the temporary listener on 127.0.0.1 (default: any free port).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Seconds to wait for the browser (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser; print the address to open instead.",
    )
    return parser


def main(
    argv=None,
    *,
    open_browser=None,
    token_url: str = TOKEN_URL,
    authorize_url: str = AUTHORIZE_URL,
    prompt=input,
    secret_prompt=getpass.getpass,
) -> int:
    args = _parser().parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("--port must be between 0 and 65535.", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("--timeout must be more than zero.", file=sys.stderr)
        return 2
    if open_browser is None:
        open_browser = (lambda url: False) if args.no_browser else webbrowser.open

    try:
        if args.env_file:
            env_path = args.env_file
            _env_preflight(env_path)
            file_values = read_env_file(env_path)
        else:
            # Same .env loading as the server, so VAULT_DB_PATH and
            # TOKEN_ENCRYPTION_KEY resolve to what the server will use.
            try:
                from dotenv import load_dotenv
            except ImportError:
                raise LoginError(
                    "Writing the vault database needs the package installed (pip install .). "
                    "For the Docker setup, use --env-file .env instead."
                ) from None
            load_dotenv()
            db_path = os.getenv("VAULT_DB_PATH", DEFAULT_DB_PATH)
            _db_preflight(db_path)
            file_values = {}

        client_id, client_secret, prompted = resolve_credentials(
            file_values, prompt=prompt, secret_prompt=secret_prompt
        )
        tokens = run_login(
            client_id=client_id,
            client_secret=client_secret,
            port=args.port,
            timeout=args.timeout,
            open_browser=open_browser,
            token_url=token_url,
            authorize_url=authorize_url,
        )

        if args.env_file:
            updates = {
                ENV_ACCESS_TOKEN: tokens["access_token"],
                ENV_REFRESH_TOKEN: tokens["refresh_token"],
            }
            # The server needs the client id and secret too. Write them only
            # when the file does not already hold real values.
            if not _clean(file_values.get(ENV_CLIENT_ID)):
                updates[ENV_CLIENT_ID] = client_id
            if not _clean(file_values.get(ENV_CLIENT_SECRET)):
                updates[ENV_CLIENT_SECRET] = client_secret
            update_env_file(env_path, updates)
            where = f"{env_path} (readable by you only)"
            next_step = "docker compose up -d"
        else:
            encrypted = store_in_vault(db_path, tokens)
            where = f"the vault at {db_path}" + (
                ", encrypted with TOKEN_ENCRYPTION_KEY"
                if encrypted
                else " (not encrypted: TOKEN_ENCRYPTION_KEY is not set)"
            )
            if prompted:
                next_step = (
                    f"put {ENV_CLIENT_ID} and {ENV_CLIENT_SECRET} in the server's .env, "
                    "then run strava-mcp-vault"
                )
            else:
                next_step = "strava-mcp-vault"
    except LoginError as exc:
        print(f"Sign-in failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was stored.", file=sys.stderr)
        return 130

    who = f" as {tokens['firstname']}" if tokens.get("firstname") else ""
    print(f"Signed in to Strava{who}. Tokens saved to {where}.")
    print(f"Next step: {next_step}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
