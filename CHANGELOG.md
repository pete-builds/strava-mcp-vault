# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Power-meter data surfaced across read tools.** `strava_get_activity` now
  renders an ⚡ Power section with average, weighted-average ("NP"), max,
  total work (kJ), and source ("Power meter" vs. "Estimated"). Rides with
  power data in `strava_get_recent_activities` show a power line in card
  view and `Avg W` + `NP` columns in compact mode. `strava_query_vault`
  returns `total_kilojoules`, `avg_weighted_power`, and `power_rides_count`
  when any matching activities recorded power.
- **`has_power` filter** on `strava_get_recent_activities` and
  `strava_query_vault`. `true` returns only power-recorded activities;
  `false` returns only those without.
- **Sport-type category aliases.** `sport_type` accepts lowercase aliases
  (`"rides"`, `"running"`, `"cycling"`, `"snow"`, `"walks"`, `"swims"`) that
  expand to all members of the category, plus comma-separated lists like
  `"Ride,GravelRide"`. CamelCase Strava types remain literal —
  `sport_type="Ride"` still matches only `Ride`.
- New `strava_mcp_vault.sport_types` module with the canonical `RIDE_TYPES`,
  `RUN_TYPES`, `SNOW_TYPES`, `WALK_TYPES`, `SWIM_TYPES` sets and the
  `expand_sport_type()` helper.
- First-time setup script (`setup.sh`) that generates secrets and writes `.env`.
- Cloudflare Tunnel sidecar via opt-in `docker-compose.override.example.yml` —
  exposes the MCP server at a public HTTPS URL for Claude.ai (web) and Cowork.
- Documentation for sharing one Strava API app across multiple services
  (independent refresh, shared seed token).

- `strava_get_zone_distribution` — time-in-zone for HR and/or power, using athlete zones from Strava (24 h cache).
- `strava_get_power_curve` — mean-max power at standard durations, with AP + NP for the activity.
- `strava_get_cardiac_drift` — first-half vs second-half HR drift with optional Pa:HR decoupling when power data is present. Requires ≥ 20 min activity.
- `strava_get_hr_power_decoupling` — Pa:HR decoupling between two segments (split in half by default, or first/last N minutes via `segment_minutes`).
- `strava_get_activity_streams`: `max_points` parameter for evenly-spaced downsampling with cross-stream alignment.
- `strava_get_activity_streams`: `export_path` parameter to write the full dataset to disk and return a small pointer (bypasses the size cap).
- Pre-flight size guard on `strava_get_activity_streams`: when response would exceed ~800 KB and neither `max_points` nor `export_path` is set, returns a structured error with a recommended `max_points` value.
- Defensive stream-type filter in `CacheManager.get_streams_normalized`: even if Strava returns extra paired streams, only requested types are surfaced.

### Changed
- **Database migration:** `activities` table grows five denormalized columns
  — `average_watts`, `weighted_average_watts`, `max_watts`, `kilojoules`,
  `device_watts` — plus an index on `average_watts`. Existing rows are
  backfilled from the stored JSON blob on the next server start; no re-sync
  required.
- 401 responses from Strava are now classified by cause: missing `activity:read_all`
  scope vs. expired/revoked tokens, with actionable recovery guidance per case.
- Server binds to `127.0.0.1` by default outside Docker; set `MCP_BIND_HOST` to
  override.
- `VAULT_DB_PATH` defaults to `./data/vault.db` outside Docker; `/app/data/vault.db`
  inside the container.

- **`strava_get_activity_streams` markdown mode** now includes a downsample banner (when applicable) and an inline preview of up to 60 evenly-spaced points per stream, in addition to the existing min/max/avg summary.

### Fixed
- **`query_vault` and `get_recent_activities` now use the same data source.**
  When the vault was empty, `get_recent_activities` returned live API
  results while `query_vault` silently reported zero rows — callers asking
  the same question with the same filters got wildly different answers
  ("16 rides via the list tool, 0 via the aggregate tool"). Both tools now
  share a single `_fetch_api_filtered` helper: when the vault has data they
  use it; when it's empty they fall back to the Strava API with identical
  filter semantics (`before`/`after` passed natively, `sport_type` +
  `has_power` applied client-side). The API fallback is capped at Strava's
  per-page limit of 200 activities; when the page is full, `query_vault`
  sets a `truncated` flag and the markdown output flags incomplete totals.
  Result: same input → same answer regardless of vault state, no user
  action required.
- **API fallback path now honors every filter.** When the vault is empty,
  `get_recent_activities` used to silently drop `sport_type`, `has_power`,
  `before`, and `after`. `before`/`after` are now converted to epoch and
  passed to Strava's `/athlete/activities` natively; `sport_type` and
  `has_power` are applied client-side after fetch. Fetch size widens to 200
  when filters are present so client-side filtering can still satisfy
  `count`. Cache key includes the full filter signature so unfiltered and
  filtered results don't collide.
- README warning about Strava refresh-token rotation corrected — long-lived
  refresh tokens are stable across refreshes.

### Documentation
- README: new "Supported clients" section clarifies this server targets Claude Desktop / Claude Code and is not designed for claude.ai web chat.
- README: new "Working with stream data" section documents the size guard, downsampling, and disk export.

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
