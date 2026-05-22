# Sharing One Strava API App Across Multiple Consumers

**Status:** Approved
**Date:** 2026-05-22
**Authors:** Seth Neal, Claude

## Context

Strava limits each account to **one API application**. The application produces a single `(client_id, client_secret)` pair, a single configured OAuth callback domain, and a single webhook subscription slot.

This project (`strava-mcp-vault`) is the second consumer of an existing Strava app already used by `coach.sethneal.com` (a Vercel-hosted service with a Neon Postgres backend that pulls 1–5 activities per day via a daily cron). We need a pattern that lets both apps coexist on a single Strava client.

A previous session note in this repo's README warned that sharing a `client_id` would cause refresh-token rotation races where the "loser" gets deauthorized. **That assumption was wrong.** A direct test against `POST https://www.strava.com/oauth/token` confirmed that Strava returns the *same* `refresh_token` we sent in — refresh tokens are not rotated per call. This spec replaces the earlier warning with the correct guidance.

## Goals

- Let `strava-mcp-vault` and `coach.sethneal.com` share one Strava app indefinitely.
- Keep both apps' Strava access independent: neither requires the other to be running.
- Document the pattern clearly so other users hitting the one-app limit can follow it.
- Correct prior README content that overstated the rotation risk.

## Non-goals

- Building a token-broker service (deferred until evidence of actual rotation).
- Sharing a Postgres token table between the two apps (over-engineered for the load).
- Modifying `coach.sethneal.com` beyond updating its stored environment variables.
- Solving webhook routing — neither app uses Strava webhooks today.

## Architecture

**Independent refresh, shared seed.** Both apps hold the same long-lived `refresh_token` (the one minted by the initial OAuth flow) in their own configuration store. Each refreshes against Strava independently and caches its own short-lived `access_token` locally.

```
                                       ┌──────────────┐
   MCP server (local)  ──────────────► │              │
   .venv/python                        │  Strava OAuth│
   refresh_token=<X>                   │  /oauth/token│
   access_token cached in SQLite       │              │
                                       │              │
   coach.sethneal.com  ──────────────► │              │
   Vercel function (daily cron)        └──────────────┘
   refresh_token=<X>  (same X)
   access_token cached in Neon
```

The two apps never observe or modify each other's state. They share only the seed `refresh_token`, which is treated as a stable secret.

## Components

No new components. Both consumers already have their own token-refresh code paths:

- `strava-mcp-vault/clients/strava.py` — refreshes against Strava and persists tokens in the SQLite vault.
- `coach.sethneal.com` — refreshes against Strava and persists tokens in Neon (implementation external to this repo).

## Data flow

**Initial OAuth (one-time, manual):**

1. User opens authorization URL with `scope=read,activity:read_all` (critical — without this, `/athlete/activities` returns 401).
2. User authorizes the app; browser redirects to the configured callback domain with an authorization `code`.
3. User exchanges the code at `POST /oauth/token` → receives `access_token`, `refresh_token`, `expires_at`.
4. User copies both tokens into:
   - `strava-mcp-vault/.env` → `STRAVA_ACCESS_TOKEN`, `STRAVA_REFRESH_TOKEN`
   - `coach.sethneal.com`'s Vercel environment variables → same fields

**Steady state (per app, independent):**

```
on_api_call():
  if access_token in local store is unexpired:
    use it directly
  else:
    POST /oauth/token with shared refresh_token
    store the returned access_token locally
    use it
```

Each app's access_token cache diverges over time. This is intentional and harmless.

## Error handling

Single failure mode worth designing for: a refresh attempt returns 401.

Causes (in rough order of likelihood):
- User revoked the app at <https://www.strava.com/settings/apps>
- User re-ran the OAuth flow elsewhere, invalidating the prior refresh_token
- The original tokens had insufficient scope (e.g., `read` only) — this is what we hit in today's session
- Strava changes its rotation policy in the future

Recovery in all cases: re-run the OAuth dance once and update both apps' env vars with the new tokens.

Implementation:
- `strava-mcp-vault/clients/strava.py` — when refresh returns 401, log a clear message naming the env vars to update and pointing to `README.md#oauth-get-your-access-tokens`. Already partially done; polish the wording.
- `coach.sethneal.com` — already handles this in its own error logging; out of scope here.

## Codebase changes

Scoped to `strava-mcp-vault` only.

1. **README correction.** Replace the rotation-race warning added earlier today with the actual nuance:
   - One Strava API app per account is a hard limit.
   - Refresh tokens are stable per current evidence; sharing them across consumers works.
   - Real shared constraints to be aware of: pooled rate limit (1,000 req/day), single webhook slot.
   - Recovery procedure if a refresh ever fails: re-run OAuth, update all consumers.

2. **README addition.** Add a short "Sharing one Strava app across multiple services" subsection under Setup, since this is now a documented supported pattern.

3. **MCP error message polish (optional).** When `clients/strava.py` detects a 401 on refresh, surface a message that names the env vars and points users at the OAuth README anchor.

## Testing

After the fix:

1. Re-run OAuth with `scope=read,activity:read_all` → obtain new tokens.
2. Update `strava-mcp-vault/.env` with new `STRAVA_ACCESS_TOKEN` and `STRAVA_REFRESH_TOKEN`.
3. Update `coach.sethneal.com`'s Vercel env vars with the same tokens.
4. Restart MCP server. Verify Claude Desktop can call `strava_get_recent_activities` and receive a real activity list.
5. Wait for the next coach daily cron run (or trigger manually). Verify it succeeds.
6. After ~6 hours, verify both apps have refreshed independently: each will have a different cached `access_token`, both still working.

## Migration plan

For this session:

1. Re-run OAuth with correct scope; capture new tokens.
2. Update MCP server's local `.env`.
3. Restart MCP server. Confirm Claude Desktop works end-to-end.
4. User updates Vercel env vars for coach (manual).
5. Apply README corrections + additions described above.
6. Apply optional error-message polish.
7. Commit + push.

## Open questions

None. All decisions resolved during the brainstorm.

## Out of scope (deferred)

- **Token broker pattern (Option 2 from the brainstorm).** Defer until a real rotation event proves it necessary.
- **Shared Neon table (Option 3).** Same reasoning; the coupling cost outweighs benefits at current scale.
- **Webhook routing.** Not used by either consumer today. Revisit if webhooks are introduced.
- **Rate-limit coordination.** Combined daily usage is <2% of the quota; no coordination needed.
