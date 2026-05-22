# Roadmap

Tracking issues found during project review and ideas for future work.

Mark items with `[x]` when complete and add a one-line note with the commit SHA or PR link.

---

## Fixes

### High severity

- [x] **Timing-attack vulnerable token comparison** — `auth.py:35` — fixed in `910da8c` (hmac.compare_digest on bytes)
- [x] **Silent fallback to plaintext on decrypt failure** — `cache/encryption.py:51-56` — fixed in `cdaa7e2` (detect Fernet shape, raise on real decrypt failure, init_tokens falls through to env-var seed)
- [x] **Default `0.0.0.0` bind with optional auth** — `server.py:90`, `docker-compose.yml:13` — fixed in `6e131fe` (startup refuses without MCP_AUTH_TOKEN unless MCP_ALLOW_UNAUTHENTICATED=1; compose default bind changed to 127.0.0.1)

### Medium severity

- [x] **Bare `except Exception: pass` on `ALTER TABLE`** — `cache/db.py:76-79` — fixed in `6daa45e` (narrow to sqlite3.OperationalError + "duplicate column name" check)
- [x] **SQLite never enabled WAL mode** — `cache/db.py:29` — fixed in `6daa45e` (PRAGMA journal_mode=WAL + synchronous=NORMAL)
- [x] **Inconsistent tool error handling** — `server.py:229-233, 292-308, 311-325` — fixed in `21be328` (try/except VaultError wrappers added to all 3 tools)
- [x] **`forward_geocode` rate-limit global isn't locked** — `cache/geocode.py:14-26` — fixed in `f6605d4` (threading.Lock around the gating sleep, safe across asyncio.to_thread workers)
- [x] **Serial gear-name lookups in async** — `cache/manager.py:151-155` — fixed in `84e5fa8` (asyncio.gather)
- [x] **`_resolve_gear_name` swallows all exceptions silently** — `cache/manager.py:224-229` — fixed in `84e5fa8` (logger.warning with exc_info)

### Low severity / nits

- [ ] **Wrong type hint** — `clients/base.py:5`
  `api_key: str = None` → `str | None = None`. Also: `api_key` is unused (StravaClient never passes it) — consider removing.
- [ ] **Dead duplicated code** — `clients/base.py:16-26`
  `BaseClient._get` is shadowed entirely by `StravaClient._get`. Delete it or have Strava reuse it.
- [ ] **Cache-stats write amplification** — `cache/db.py:99-131`
  Every cache read issues 2-3 writes. Buffer counters in memory and flush periodically.
- [ ] **Empty-string location override stored as `""`** — `cache/db.py:382`
  Docstring says "Pass null to clear" but `set_activity_location("")` stores `""`, not `NULL`. Normalize empty → `NULL` for consistency.
- [ ] **`cleanup_expired` only runs at startup** — `cache/db.py:88`
  Long-running containers accumulate expired rows forever. Call it from `get_cache_stats` or on a periodic task.
- [ ] **No upper bound on `sync_activities(days_back=…)`** — `server.py:329`
  `days_back=10**9` is accepted. Cap to ~3650 (10 years) and document the limit.
- [ ] **`forward_geocode` results aren't cached** — `cache/geocode.py:30-40`
  Reverse geocoding is deduplicated, but every `get_activities_near("Syracuse, NY")` call re-hits Nominatim. Add a small persistent cache (the `geocoding_cache` table mentioned in the explore report may already exist).

---

## MCP spec conformance

Gaps surfaced by the `mcp-builder` best-practices doc.

- [ ] **Server name violates Python convention** — `server.py:90`
  `"strava-vault"` → `"strava_mcp"` (spec: `{service}_mcp`, snake_case).
- [ ] **No service prefix on tool names** — all 11 `@mcp.tool()` definitions
  Spec: `{service}_{action}_{resource}` to avoid collisions when multiple MCP servers are loaded together.
  - `get_recent_activities` → `strava_get_recent_activities`
  - `query_vault` → `strava_query_vault`
  - `get_activity` → `strava_get_activity`
  - `get_activity_streams` → `strava_get_activity_streams`
  - `get_athlete_profile` → `strava_get_athlete_profile`
  - `get_athlete_stats` → `strava_get_athlete_stats`
  - `get_cache_stats` → `strava_get_cache_stats`
  - `get_activities_near` → `strava_get_activities_near`
  - `set_activity_location` → `strava_set_activity_location`
  - `delete_vault_activity` → `strava_delete_vault_activity`
  - `sync_activities` → `strava_sync_activities`
- [ ] **Zero tool annotations** — every `@mcp.tool()`
  Add `title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` so clients can reason about tool safety.
- [ ] **No Pydantic input models** — all tools take bare params
  Move to `BaseModel` with `Field(...)` constraints. Replaces ad-hoc validators like `_validate_radius_miles` (`server.py:221`) with declarative constraints (`Field(gt=0, le=250)`). Add ISO-date `field_validator` for `after`/`before`.
- [ ] **No `response_format: json | markdown` parameter** — all tools return Markdown only
  Spec requires both. JSON unlocks programmatic chaining; Markdown stays the human-friendly default.
- [ ] **No pagination on list tools** — `get_recent_activities`, `get_activities_near`, `query_vault`
  Return `{total, count, offset, items, has_more, next_offset}`. `get_activities_near` currently has no result cap at all.
- [ ] **No structured output / `outputSchema`** — all tools return plain `str`
  Use FastMCP TypedDict / Pydantic return types so clients get `structuredContent` instead of having to parse markdown.
- [ ] **DNS rebinding protection missing** — `server.py:90`
  Bind `127.0.0.1` for local deployments; validate the `Origin` header on incoming requests. Overlaps with the High-severity bind item.
- [ ] **MCP SDK pinned old** — `requirements.txt`
  `mcp[cli]==1.26.0`. Newer SDKs add Context injection, lifespan dicts, structured-content helpers. Bump after compatibility check.
- [ ] **No `Context` / progress reporting in `sync_activities`** — `server.py:328-350`
  Sync can take many seconds. Use `ctx.report_progress(...)` so clients can render a progress bar.
- [ ] **No MCP Resources exposed**
  Static-ish endpoints like athlete profile and vault summary fit the Resource model (e.g. `strava://athlete/profile`, `strava://vault/summary`). Lower priority — tools work — but it's idiomatic MCP.
- [ ] **Generic `except Exception` in tool wrappers** — `server.py:122, 153, etc.`
  Catch `httpx.HTTPStatusError`, `httpx.TimeoutException`, `VaultError` subclasses specifically and map status codes (404 → "check the ID", 429 → "wait before retrying") to actionable messages.
- [ ] **Errors returned as plain strings, not `isError: true`**
  Returning `"Error: ..."` looks indistinguishable from a successful response that mentions an error. Raise exceptions (FastMCP sets `isError`) or return the structured error envelope.
- [ ] **Tool docstrings missing return-schema and "don't use when" guidance**
  Spec example includes: full JSON schema of the return value (field names, types, units), "Use when…" examples, "Don't use when… use {other_tool} instead", and error string examples.

---

## Future Features

- **Sport-type category aliases** (from `docs/plans/2026-03-11-vault-query-and-filtering.md:629-631`)
  Accept comma-separated types or category aliases like `"rides"` that map to the `_RIDE_TYPES` set, so callers don't have to enumerate `Ride, GravelRide, MountainBikeRide, VirtualRide, EBikeRide`.
- **Persistent `forward_geocode` cache**
  The existing `geocoding_cache` table (per explore report) should be wired into `forward_geocode` so repeated `get_activities_near("Syracuse, NY")` calls don't re-hit Nominatim.
- **Periodic background sync**
  Today `sync_activities` is manual. Optional `STRAVA_SYNC_INTERVAL_HOURS` env var → background task that runs incremental sync on a schedule.
- **MCP Resources for athlete profile and vault summary**
  Listed under MCP conformance; promoting to a feature because it unlocks lighter-weight access patterns for clients that just want to read static-ish state.
- **Structured JSON output mode**
  Once `response_format` lands (under MCP conformance), the JSON variants become a feature for downstream agents that want to do their own aggregation / chaining without re-parsing markdown tables.
