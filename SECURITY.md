# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Instead, report privately by emailing the maintainer or opening a
[GitHub security advisory](https://github.com/<owner>/strava-mcp-vault/security/advisories/new)
on this repository. Include:

- A description of the issue and its impact.
- Steps to reproduce, ideally with a minimal proof of concept.
- The affected version (commit SHA or release tag).
- Any suggested mitigation if you have one.

Expect an initial acknowledgement within **5 business days**. Triage and
remediation timelines depend on severity; critical issues are prioritized.

## Supported versions

Only the latest release on `main` is supported. Older releases will not
receive backports.

## Scope

In scope:

- The MCP server itself (`server.py`, auth middleware, transport).
- Token storage and refresh (`auth.py`, `cache/encryption.py`).
- The cache + vault layer (`cache/`, `clients/`).
- Default Docker / docker-compose configuration shipped in this repo.

Out of scope:

- Vulnerabilities in the Strava API, MCP spec, or upstream dependencies
  (please report those upstream).
- Vulnerabilities that require an attacker to already control the host
  running the server, or to already hold a valid `MCP_AUTH_TOKEN`.
- Misconfiguration by the operator (e.g. running with
  `MCP_ALLOW_UNAUTHENTICATED=1` on a public interface) — these are
  documented risks, not bugs.

## Sensitive data handled by this project

This server holds:

- Strava OAuth `client_id` / `client_secret` (in `.env`).
- Strava `access_token` / `refresh_token` (in SQLite, optionally
  Fernet-encrypted at rest via `TOKEN_ENCRYPTION_KEY`).
- A bearer token for the MCP endpoint (`MCP_AUTH_TOKEN`).
- Cached activity, athlete, and stream data from the Strava API.

Operators are responsible for protecting `.env`, the SQLite file, and the
network reachability of the MCP port. The README documents recommended
defaults (loopback bind, bearer auth, optional Cloudflare Tunnel with TLS).
