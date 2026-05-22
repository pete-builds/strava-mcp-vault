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

- [x] **Wrong type hint + unused `api_key`** — `clients/base.py:5` — fixed in `15c451f` (parameter removed entirely)
- [x] **Dead duplicated code** — `clients/base.py:16-26` — fixed in `15c451f` (BaseClient._get removed)
- [x] **Cache-stats write amplification** — `cache/db.py:99-131` — fixed in `bd77279` (collapsed INSERT-OR-IGNORE + UPDATE into single UPSERT via ON CONFLICT)
- [x] **Empty-string location override stored as `""`** — `cache/db.py:382` — fixed in `bd77279` (empty normalized to NULL)
- [x] **`cleanup_expired` only runs at startup** — `cache/db.py:88` — fixed in `bd77279` (also called from `get_stats`)
- [x] **No upper bound on `sync_activities(days_back=…)`** — `server.py:329` — fixed in `da3f214` (validated to [0, 3650])
- [x] **`forward_geocode` results aren't cached** — `cache/geocode.py:30-40` — fixed in `da3f214` (in-process dict capped at 1000 entries, caches hits and misses)

---

## MCP spec conformance

Gaps surfaced by the `mcp-builder` best-practices doc.

- [x] **Server name violates Python convention** — `server.py:90` — fixed in `df5b856` ("strava-vault" → "strava_mcp")
- [x] **No service prefix on tool names** — all 11 `@mcp.tool()` definitions — fixed in `df5b856` (via `name=` arg; Python function names unchanged so tests still pass)
- [x] **Zero tool annotations** — every `@mcp.tool()` — fixed in `df5b856` (title + readOnly/destructive/idempotent/openWorld hints on all 11 tools)
- [x] **No `response_format: json | markdown` parameter** — fixed in `23fce8e` (all 8 read tools accept response_format; default "markdown" preserves existing behavior)
- [x] **No pagination on list tools** — fixed in `23fce8e` (offset on `get_recent_activities`, limit+offset on `get_activities_near`; JSON envelope returns total/count/offset/items/has_more/next_offset)
- [x] **DNS rebinding protection missing** — fixed in `1546581` (optional `OriginCheckMiddleware` activated by `MCP_ALLOWED_ORIGINS` env var; bind already moved to 127.0.0.1 in `6e131fe`)
- [x] **Generic `except Exception` in tool wrappers** — fixed in `d856d50` (centralized `_tool_error` maps RateLimitError, StravaAPIError 404/401/403/429, VaultError to actionable messages)
- [x] **No `Context` / progress reporting in `sync_activities`** — fixed in `a8b188f` (ctx auto-injected by FastMCP; per-page progress reports threaded through CacheManager.sync_activities)

### Deferred

- [ ] **No Pydantic input models with Field constraints** — current state: manual validation handles semantics correctly (`_validate_radius_miles`, `days_back` range, `limit`/`offset` checks). Adding Annotated[type, Field(...)] would enrich the MCP inputSchema, but the validation behavior is already covered. Worth a follow-up if multiple LLM clients prove confused by the absent schema constraints.
- [ ] **No structured output / `outputSchema`** — every tool would need a Pydantic/TypedDict return type and the markdown formatters reworked. JSON path now exists via `response_format="json"` which gives most of the value to programmatic callers. Defer the full outputSchema migration unless a downstream client really needs it.
- [ ] **MCP SDK pinned old (`mcp[cli]==1.26.0`)** — bumping requires testing against the newer FastMCP API (Context invocation shape, lifespan dict, structured-content helpers may differ). Defer until we have a local env to validate against.
- [ ] **No MCP Resources exposed** — athlete profile and vault summary are natural Resource candidates (`strava://athlete/profile`, `strava://vault/summary`). Lower priority per spec doc — tools work — and this is additive, not corrective.
- [ ] **Errors returned as plain strings, not `isError: true`** — current `"Error: ..."` envelope is functional and consistent. Raising from tools is more idiomatic MCP but a tool-by-tool migration with client-facing message shape changes. Defer unless we hear of clients having trouble distinguishing errors from happy-path responses that mention errors.
- [ ] **Tool docstrings missing return-schema and "don't use when" guidance** — tedious; best done iteratively as we observe real LLM misuse. Each tool has a clear summary today, so this is polish rather than correctness.

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
- **`sport_type` filter on `strava_get_recent_activities`**
  Observed 2026-05-22: when asked for "my 5 most recent cycling activities," Claude Desktop attempted to pass a `sport_type` parameter the tool doesn't accept, then fell back to fetching unfiltered and filtering client-side — producing two tool calls and a visible "the filter isn't strict, pulling a wider net" remark in the response. Add a `sport_type` parameter that accepts either a single Strava type (e.g., `"Ride"`) or the category aliases tracked above (e.g., `"rides"` → `_RIDE_TYPES`). Server-side filtering avoids the double-fetch and removes the temptation for LLM clients to invent the parameter.
