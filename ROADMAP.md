# Roadmap

Ideas on the table. None of these are committed deliverables — open an issue
before doing significant work so we can align on scope.

> Past fixes and code-review punch-list items have moved to git history and
> [`CHANGELOG.md`](CHANGELOG.md).

---

## Planned

- **Persistent `forward_geocode` cache**
  The existing `geocoding_cache` table should be wired into `forward_geocode`
  so repeated `get_activities_near("Syracuse, NY")` calls don't re-hit
  Nominatim across restarts.
- **Periodic background sync**
  Today `sync_activities` is manual. Optional `STRAVA_SYNC_INTERVAL_HOURS` env
  var → background task that runs incremental sync on a schedule.
- **MCP Resources for athlete profile and vault summary**
  `strava://athlete/profile` and `strava://vault/summary` are natural Resource
  candidates — additive, not corrective.

---

## Deferred (MCP conformance polish)

These are tracked because the `mcp-builder` best-practices doc lists them, but
none are correctness issues at the time of writing. Each one will get picked up
if a real-world client surfaces friction.

- **Pydantic input models with `Field` constraints**
  Manual validation already enforces semantics (`_validate_radius_miles`,
  `days_back` range, `limit`/`offset` checks). Adding `Annotated[type, Field(...)]`
  would enrich the MCP `inputSchema`. Defer until multiple clients prove confused.
- **Structured output / `outputSchema`**
  Would require a Pydantic/TypedDict return type per tool plus reworking the
  markdown formatters. `response_format="json"` already gives programmatic
  callers most of the value.
- **Bump MCP SDK (`mcp[cli]==1.26.0`)**
  Newer FastMCP API changes (Context invocation shape, lifespan dict,
  structured-content helpers) need re-validation. Defer until there's a
  reason — security patch, needed feature, etc.
- **Errors returned as `isError: true` instead of `"Error: ..."` strings**
  Current envelope is functional and consistent across all tools. Migration is
  tool-by-tool with client-facing message-shape changes; defer unless clients
  struggle to distinguish errors from happy-path responses that mention errors.
- **Tool docstrings with return-schema + "don't use when" guidance**
  Polish, not correctness. Best done iteratively as real LLM misuse surfaces.
