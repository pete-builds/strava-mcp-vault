# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- First-time setup script (`setup.sh`) that generates secrets and writes `.env`.
- Cloudflare Tunnel sidecar via opt-in `docker-compose.override.example.yml` —
  exposes the MCP server at a public HTTPS URL for Claude.ai (web) and Cowork.
- Documentation for sharing one Strava API app across multiple services
  (independent refresh, shared seed token).

### Changed
- 401 responses from Strava are now classified by cause: missing `activity:read_all`
  scope vs. expired/revoked tokens, with actionable recovery guidance per case.
- Server binds to `127.0.0.1` by default outside Docker; set `MCP_BIND_HOST` to
  override.
- `VAULT_DB_PATH` defaults to `./data/vault.db` outside Docker; `/app/data/vault.db`
  inside the container.

### Fixed
- README warning about Strava refresh-token rotation corrected — long-lived
  refresh tokens are stable across refreshes.

## [0.2.0]

### Changed
- **BREAKING:** Migrated from HTTP+SSE transport (`/sse`) to Streamable HTTP
  transport (`/mcp`), per MCP spec 2025-06-18. Existing clients must re-register
  with the new URL and transport.

### Added
- Pagination (`offset`, `limit`) on `get_recent_activities` and
  `get_activities_near` with JSON envelope (`total`, `count`, `offset`, `items`,
  `has_more`, `next_offset`).
- `response_format` parameter (`"json"` | `"markdown"`) on all 8 read tools.
- Optional `Origin` allowlist (`MCP_ALLOWED_ORIGINS`) for DNS-rebinding
  protection on browser clients.
- Per-page progress reporting from `sync_activities` via the MCP `Context`.
- Tool annotations (title, readOnly / destructive / idempotent / openWorld
  hints) on all 11 tools.
- Service prefix (`strava_`) on all tool names; server name conforms to Python
  naming (`strava_mcp`).
- Centralized `_tool_error` mapping `RateLimitError`, `StravaAPIError` 404/401/403/429,
  and `VaultError` to actionable messages.
- Constant-time bearer-token comparison (`hmac.compare_digest`).
- Explicit auth opt-in/opt-out at startup — server refuses to boot without
  either `MCP_AUTH_TOKEN` or `MCP_ALLOW_UNAUTHENTICATED=1`.
- Loud-failure on Fernet decrypt errors for previously-encrypted tokens.
- WAL mode on SQLite; opportunistic cache cleanup on `get_stats`.
- Day-cap and in-process geocode caching for `forward_geocode`.

### Fixed
- `get_activities_near` and `delete_vault_activity` input validation hardened.
- Healthcheck includes the `MCP_AUTH_TOKEN` bearer header.

## [0.1.0]

Initial release.

### Added
- FastMCP server exposing Strava read tools (recent activities, activity detail,
  streams, athlete profile, athlete stats, vault query, activities-near,
  cache stats).
- Write tools: `sync_activities`, `delete_vault_activity`, `set_activity_location`.
- SQLite vault with per-data-type TTL cache (activities 1h, detail 24h,
  streams 7d, athlete stats 1d).
- Automatic OAuth token refresh with at-rest Fernet encryption.
- Reverse geocoding via Nominatim with rate-limit lock.
- Forward geocoding for `get_activities_near`.
- Docker + docker-compose deployment.
- MIT license.

[Unreleased]: https://github.com/<owner>/strava-mcp-vault/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/<owner>/strava-mcp-vault/releases/tag/v0.2.0
[0.1.0]: https://github.com/<owner>/strava-mcp-vault/releases/tag/v0.1.0
