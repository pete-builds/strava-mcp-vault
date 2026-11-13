"""Standard-library-only checks for strava-mcp-vault-login's Docker mode.

The README tells Docker users to run ``python3 src/strava_mcp_vault/login.py
--env-file .env`` straight from a clone, on whatever python3 the machine has
(macOS still ships 3.9). pytest and the package's dependencies are not
available there, so these checks import nothing outside the standard library
and run as a plain script:

    python -I tests/stdlib_smoke.py

CI runs it inside the official python:3.9-slim image. tests/test_login.py also
runs it on every supported Python, so it cannot rot. No network: Strava's token
endpoint is a local stub, and the browser is a thread.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import socket
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOGIN = Path(__file__).resolve().parent.parent / "src" / "strava_mcp_vault" / "login.py"
login = runpy.run_path(str(LOGIN), run_name="strava_login_smoke")

ACCESS = "a" * 40
REFRESH = "b" * 40
SECRET = "f" * 40
CODE = "c" * 40
GRANTED = "read,activity:read_all"
REAL_KEY = "Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6cXV4Zm9vYmFyYmE="


class _Stub(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        _Stub.requests.append(dict(urllib.parse.parse_qsl(self.rfile.read(length).decode())))
        body = json.dumps(
            {"access_token": ACCESS, "refresh_token": REFRESH, "expires_at": 2000000000}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def _raw_get(port: int, target: str, host: str | None = None) -> str:
    host_line = f"Host: {host or f'localhost:{port}'}\r\n"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(f"GET {target} HTTP/1.1\r\n{host_line}\r\n".encode("latin-1"))
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError:
        return ""
    return data.split(b"\r\n", 1)[0].decode()


def _callback(state: str, code: str = CODE) -> str:
    query = urllib.parse.urlencode({"state": state, "code": code, "scope": GRANTED})
    return f"/callback?{query}"


class _Browser:
    """Runs raw requests in order once the command opens the browser."""

    def __init__(self, *steps):
        self.steps = steps
        self.results: list = []
        self.thread: threading.Thread | None = None

    def __call__(self, url: str) -> None:
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        port = urllib.parse.urlsplit(params["redirect_uri"]).port
        state = params["state"]

        def run():
            for step in self.steps:
                self.results.append(step(port, state))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def join(self) -> list:
        if self.thread is not None:
            self.thread.join(10)
        return self.results


def _real(port: int, state: str) -> str:
    return _raw_get(port, _callback(state))


def _env_file(text: str) -> str:
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, ".env")
    with open(path, "w") as fh:
        fh.write(text)
    return path


def _run(argv: list, browser, token_url: str):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = login["main"](argv, open_browser=browser, token_url=token_url)
    return rc, out.getvalue(), err.getvalue()


FAILURES: list = []


def check(name: str, condition: bool) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}")
    if not condition:
        FAILURES.append(name)


def main() -> int:
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    token_url = f"http://127.0.0.1:{stub.server_address[1]}/oauth/token"
    base = f"STRAVA_CLIENT_ID=12345\nSTRAVA_CLIENT_SECRET={SECRET}\n"
    print(f"strava-mcp-vault-login stdlib smoke on Python {sys.version.split()[0]}")
    try:
        # 1. The README path: sign in, tokens land in the env file, owner-only.
        env = _env_file(base)
        browser = _Browser(_real)
        rc, out, err = _run(["--env-file", env, "--timeout", "10"], browser, token_url)
        browser.join()
        with open(env) as fh:
            text = fh.read()
        check("sign-in writes the env file", rc == 0 and f"STRAVA_ACCESS_TOKEN={ACCESS}" in text)
        check("env file is owner-only", os.stat(env).st_mode & 0o777 == 0o600)
        check("next step printed", "Next step: docker compose up -d" in out)
        check(
            "nothing secret printed",
            not any(s in out + err for s in (ACCESS, REFRESH, SECRET, CODE)),
        )

        # 2. A target urlsplit could not parse used to crash the command.
        env = _env_file(base)
        browser = _Browser(lambda port, state: _raw_get(port, "//[x"), _real)
        rc, out, err = _run(["--env-file", env, "--timeout", "10"], browser, token_url)
        check(
            "unparseable target is a 404, not a crash",
            rc == 0 and browser.join()[0].endswith("404 Not Found"),
        )

        # 3. A wrong state is refused and the real redirect still lands.
        env = _env_file(base)
        browser = _Browser(lambda port, state: _raw_get(port, _callback("guess", "guess")), _real)
        rc, out, err = _run(["--env-file", env, "--timeout", "10"], browser, token_url)
        statuses = browser.join()
        check(
            "wrong state refused, then signed in",
            rc == 0 and statuses[0].endswith("400 Bad Request"),
        )
        check("refusal line printed", login["REFUSAL_LINE"] in out)

        # 4. A foreign Host header is a 404 (DNS rebinding).
        env = _env_file(base)
        browser = _Browser(
            lambda port, state: _raw_get(port, _callback(state, "x"), host="evil.example"), _real
        )
        rc, out, err = _run(["--env-file", env, "--timeout", "10"], browser, token_url)
        check("foreign Host is a 404", rc == 0 and browser.join()[0].endswith("404 Not Found"))

        # 5. Encryption keys: refused before the browser, same answer on any Python.
        for label, key in (
            ("not a key", "abc123"),
            ("stray = mid-key", REAL_KEY[:19] + "=" + REAL_KEY[19:]),
            ("junk after padding", REAL_KEY + "AAAA"),
        ):
            env = _env_file(base + f"TOKEN_ENCRYPTION_KEY={key}\n")
            opened: list = []
            rc, out, err = _run(["--env-file", env, "--timeout", "2"], opened.append, token_url)
            check(f"bad key refused before browser ({label})", rc == 1 and not opened)
        check("real key shape accepted", login["_fernet_key_is_valid"](REAL_KEY))

        # 6. --timeout must be a finite number.
        for value in ("inf", "nan"):
            rc, out, err = _run([f"--timeout={value}"], lambda url: None, token_url)
            check(f"--timeout={value} rejected", rc == 2)

        # 7. Nothing arrives: times out, nothing written.
        env = _env_file(base)
        rc, out, err = _run(["--env-file", env, "--timeout", "0.5"], lambda url: None, token_url)
        with open(env) as fh:
            check("timeout stores nothing", rc == 1 and fh.read() == base)
    finally:
        stub.shutdown()
        stub.server_close()

    print(f"{len(FAILURES)} failed" if FAILURES else "all checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
