# Multi-App Strava Token Sharing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get `strava-mcp-vault` working end-to-end through Claude Desktop on the user's machine (currently blocked by a missing OAuth scope), then correct documentation and error-handling so other users with a single Strava API app can follow the same pattern.

**Architecture:** Independent refresh, shared seed. Both `strava-mcp-vault` (local) and `coach.sethneal.com` (Vercel + Neon) hold the same `refresh_token`. Each refreshes independently. No coordination. Documented + verified by the spec at `docs/superpowers/specs/2026-05-22-multi-app-strava-tokens-design.md`.

**Tech Stack:** Python 3.13, httpx, FastMCP, SQLite, Strava OAuth.

---

## Task 1: Re-run OAuth with the correct scope

**Files:** None changed in this task — it's a user-driven browser + curl operation. Output is two new tokens that Task 2 will consume.

The previous OAuth was completed without `activity:read_all`, so refreshed tokens have `scope=read` and Strava returns 401 on any `/athlete/activities` call. Recovery is to re-run the authorization step with the correct scope and exchange the new code for tokens.

- [ ] **Step 1: Open authorization URL in browser**

Construct the URL from your `.env` values (or look the client_id up at <https://www.strava.com/settings/api>):

```
https://www.strava.com/oauth/authorize?client_id=<YOUR_CLIENT_ID>&redirect_uri=<YOUR_CALLBACK_DOMAIN>&response_type=code&scope=read,activity:read_all
```

Click **Authorize**. Strava will redirect to `<YOUR_CALLBACK_DOMAIN>/?state=&code=<CODE>&scope=read,activity:read_all`.

Expected: the address bar URL contains `scope=read,activity:read_all`. If it shows only `scope=read`, the wrong scope was sent — start over with the URL above.

- [ ] **Step 2: Capture the authorization code**

From the redirect URL, copy the value between `code=` and `&scope`. It will be ~40 hex characters.

- [ ] **Step 3: Exchange the code for tokens**

Load credentials from `.env` so this plan file never holds secrets, then post the code. Replace `<CODE>`:

```bash
set -a; source /Users/sethneal/Documents/Claude/Code/strava-mcp-vault/.env; set +a
curl -sS -X POST https://www.strava.com/oauth/token \
  -d client_id="$STRAVA_CLIENT_ID" \
  -d client_secret="$STRAVA_CLIENT_SECRET" \
  -d code=<CODE> \
  -d grant_type=authorization_code | python3 -m json.tool
```

Expected output (formatted):

```json
{
  "token_type": "Bearer",
  "expires_at": <unix_ts>,
  "expires_in": 21600,
  "refresh_token": "<new_refresh_token>",
  "access_token": "<new_access_token>",
  "athlete": { ... },
  "scope": "read,activity:read_all"
}
```

If `scope` shows `read,activity:read_all`, the new tokens are correct. Save `access_token` and `refresh_token` for Task 2.

If `scope` is missing or only `read`, the authorization code in Step 1/2 used the wrong scope. Repeat Steps 1–3.

- [ ] **Step 4: No commit (no file changes in this task)**

---

## Task 2: Update local `.env` and restart the MCP server

**Files:**
- Modify: `/Users/sethneal/Documents/Claude/Code/strava-mcp-vault/.env` (lines 7–8 — `STRAVA_ACCESS_TOKEN`, `STRAVA_REFRESH_TOKEN`)

- [ ] **Step 1: Replace the access_token and refresh_token in `.env`**

Edit `.env`:
- Set `STRAVA_ACCESS_TOKEN="<new_access_token>"` (from Task 1, Step 3)
- Set `STRAVA_REFRESH_TOKEN="<new_refresh_token>"` (from Task 1, Step 3)

Leave `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `MCP_AUTH_TOKEN`, `TOKEN_ENCRYPTION_KEY`, `VAULT_DB_PATH` unchanged.

- [ ] **Step 2: Clear the SQLite token cache so the new tokens get loaded fresh**

The server loads tokens from `.env` on first boot, then writes them to SQLite. After that the SQLite copy wins. To force a clean reseed:

```bash
rm -f /Users/sethneal/Documents/Claude/Code/strava-mcp-vault/data/vault.db
```

Expected: file removed silently. (If it doesn't exist yet, that's fine.)

- [ ] **Step 3: Restart the MCP server**

```bash
pkill -f "server.py" 2>/dev/null; sleep 1
cd /Users/sethneal/Documents/Claude/Code/strava-mcp-vault
nohup .venv/bin/python server.py > /tmp/strava-mcp.log 2>&1 &
sleep 3
tail -10 /tmp/strava-mcp.log
```

Expected: log ends with `Uvicorn running on http://127.0.0.1:18201`. No `OSError` or `[Errno 30]`.

- [ ] **Step 4: Smoke-test directly against Strava through the new tokens**

```bash
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  -H "Authorization: Bearer <new_access_token>" \
  "https://www.strava.com/api/v3/athlete/activities?per_page=1"
```

Expected: `HTTP 200` (not 401). If 401, the token doesn't have `activity:read_all` — go back to Task 1.

- [ ] **Step 5: Test end-to-end through Claude Desktop**

Quit and reopen Claude Desktop. In a new chat, ask:

> What are my 5 most recent Strava activities? Show distance, pace, and date for each.

Expected: Claude calls `strava_get_recent_activities` and returns a real activity list with your own data.

If it fails, check `tail -50 /tmp/strava-mcp.log` and `~/Library/Logs/Claude/mcp-server-strava-vault.log` for the actual error.

- [ ] **Step 6: No commit (`.env` is gitignored)**

---

## Task 3: Update `coach.sethneal.com`'s Strava env vars on Vercel

**Files:** None in this repo — this is a parallel user-driven step on Vercel.

- [ ] **Step 1: Update Vercel env vars**

In the Vercel dashboard for `coach.sethneal.com`, update the same two environment variables to the new tokens from Task 1, Step 3:
- `STRAVA_ACCESS_TOKEN` → new value
- `STRAVA_REFRESH_TOKEN` → new value

(Whatever they're called in coach's env; map by purpose.)

- [ ] **Step 2: Redeploy or wait for next daily cron**

Either trigger a redeploy from the Vercel dashboard, or wait for the next scheduled daily cron run. Verify in coach's logs that the Strava sync succeeds — it should, because the new tokens have the same scope coach already needed.

- [ ] **Step 3: No commit in this repo**

---

## Task 4: README — replace the rotation warning with accurate guidance

**Files:**
- Modify: `README.md:122` (the `> ⚠️ Don't reuse a client_id...` block added earlier this session)

The earlier warning was based on a wrong assumption that Strava rotates refresh tokens. The spec confirms this is not the case. Replace the warning with the actual nuance: shared rate limit, single webhook slot, and a documented recovery path if a refresh ever fails.

- [ ] **Step 1: Read the existing warning**

Open `README.md` and locate line 122:

```markdown
> **⚠️ Don't reuse a `client_id` you're already using elsewhere.** Strava can rotate the `refresh_token` on each refresh call. If two services share one `client_id` and both refresh tokens, they will fight and randomly deauth each other. You **can** create multiple Strava apps under the same account — create a dedicated one for this MCP server.
```

- [ ] **Step 2: Replace it with corrected guidance**

Replace the entire block above with:

```markdown
> **Heads up: one Strava API app per account.** Strava limits each account to a single API application. If you already use this `client_id` for another service (e.g. your own website), both can share the same Strava app — see [Sharing one Strava app across multiple services](#sharing-one-strava-app-across-multiple-services) below. The original concern that two services would race on refresh-token rotation does not appear to apply in practice — Strava returns a stable `refresh_token` across refresh calls — but you should still expect to re-run OAuth if a refresh ever returns 401.
```

The link anchor will resolve to the new section added in Task 5.

- [ ] **Step 3: Verify the markdown is syntactically valid**

```bash
cd /Users/sethneal/Documents/Claude/Code/strava-mcp-vault
grep -n "Don't reuse" README.md
```

Expected: no matches (the old text is gone).

```bash
grep -n "Heads up: one Strava" README.md
```

Expected: one match, showing the new text.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): correct refresh-token rotation warning

Earlier in this session we added a warning that two services sharing a
Strava client_id would race on refresh-token rotation. Direct testing
against Strava's /oauth/token confirmed the refresh_token returned is
stable across calls. Replace the alarming warning with the actual
constraints (one app per account, plus a pointer to the upcoming
multi-app section) and a recovery note for the unlikely rotation case.

See docs/superpowers/specs/2026-05-22-multi-app-strava-tokens-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: README — add "Sharing one Strava app across multiple services" subsection

**Files:**
- Modify: `README.md` (add a new subsection under `## Setup`, after the existing OAuth subsection, before `## Quick Start`)

- [ ] **Step 1: Locate the insertion point**

```bash
grep -n "^## Quick Start" README.md
```

Note the line number `N`. The new subsection goes immediately before that line.

- [ ] **Step 2: Insert the new subsection**

Insert the following block in `README.md` immediately before `## Quick Start`:

```markdown
### Sharing one Strava app across multiple services

Strava allows only one API application per account, so if you have an existing service (a coach site, a daily-stats dashboard, etc.) already using your `client_id`, this MCP server has to share it. The pattern that works:

1. **One OAuth dance.** Run the OAuth flow above *once*. The resulting `access_token` and `refresh_token` will work for all consumers.
2. **Copy the tokens into every service's config.** Same `STRAVA_ACCESS_TOKEN` and `STRAVA_REFRESH_TOKEN` in each service's environment. No central token store needed.
3. **Let each service refresh independently.** Each one calls `POST https://www.strava.com/oauth/token` when its short-lived access_token expires and caches the new one locally. Strava returns a stable `refresh_token`, so the services don't interfere with each other.
4. **Watch your shared rate limit.** The 100 req / 15 min and 1,000 req / day quotas are pooled across every consumer of the `client_id`. Heavy usage in one service can starve another.
5. **If a refresh ever returns 401, re-run OAuth once and update every service.** This shouldn't happen during normal operation. If it does — for example, you clicked "Revoke access" in Strava's settings — repeat the OAuth flow and copy the new tokens into every service's env again.

Strava's webhook subscription is also one-per-app, so if you want push events, only one of your services can receive them; the others will need to poll or proxy.

```

- [ ] **Step 3: Verify the anchor link from Task 4 resolves**

```bash
grep -n "sharing-one-strava-app-across-multiple-services\|## Sharing one Strava\|### Sharing one Strava" README.md
```

Expected output:
- One match for the `### Sharing one Strava app across multiple services` heading.
- One match for the inline anchor link from Task 4 (`#sharing-one-strava-app-across-multiple-services`).

(GitHub generates the anchor from the heading text by lowercasing and replacing spaces with hyphens.)

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
docs(readme): document sharing one Strava app across multiple services

Strava limits each account to one API application. Add a setup
subsection explaining the supported pattern when multiple services
(e.g. this MCP server + a coach.sethneal.com-style site) need to use
the same client_id: independent refresh, shared seed refresh_token,
pooled rate limit, single webhook slot.

See docs/superpowers/specs/2026-05-22-multi-app-strava-tokens-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Improve the 401 error message to detect scope-related failures

**Files:**
- Test: `tests/test_server.py` (add one new test, no fixture changes)
- Modify: `server.py:56-60` (the `if e.status_code in (401, 403):` branch in `_format_tool_error`)

When a 401 comes back after a *successful* refresh (the case we hit today), the cause is almost always insufficient OAuth scope, not an expired/revoked token. The current message says "expired or revoked; reseed..." which sends users down the wrong recovery path. Detect the scope case by sniffing the response body and tailor the message.

- [ ] **Step 1: Read the existing `_format_tool_error` function and the test patterns**

```bash
sed -n '40,70p' /Users/sethneal/Documents/Claude/Code/strava-mcp-vault/server.py
grep -n "_format_tool_error\|StravaAPIError" /Users/sethneal/Documents/Claude/Code/strava-mcp-vault/tests/test_server.py | head -10
```

This shows the function being modified and how it's currently tested.

- [ ] **Step 2: Write the failing tests**

Append the following to `tests/test_server.py`:

```python
def test_format_error_401_with_scope_hint_says_rerun_oauth():
    """When 401 detail mentions a missing scope, point users at OAuth, not reseed."""
    from server import _format_tool_error
    from exceptions import StravaAPIError

    err = StravaAPIError(
        status_code=401,
        path="/athlete/activities",
        detail='{"message":"Authorization Error","errors":[{"resource":"AccessToken","field":"activity:read_permission","code":"missing"}]}',
    )
    msg = _format_tool_error(err, "strava_get_recent_activities")

    assert "scope" in msg.lower()
    assert "activity:read_all" in msg
    assert "reseed" not in msg.lower()


def test_format_error_401_without_scope_hint_says_reseed():
    """Plain 401 (no scope marker in body) still recommends reseeding tokens."""
    from server import _format_tool_error
    from exceptions import StravaAPIError

    err = StravaAPIError(
        status_code=401,
        path="/athlete/activities",
        detail="",
    )
    msg = _format_tool_error(err, "strava_get_recent_activities")

    assert "reseed" in msg.lower() or "re-seed" in msg.lower()
```

- [ ] **Step 3: Run the new tests to confirm they fail**

```bash
cd /Users/sethneal/Documents/Claude/Code/strava-mcp-vault
.venv/bin/python -m pytest tests/test_server.py::test_format_error_401_with_scope_hint_says_rerun_oauth tests/test_server.py::test_format_error_401_without_scope_hint_says_reseed -v
```

Expected: the first test FAILS (message doesn't mention scope yet); the second test PASSES (existing message already says reseed).

- [ ] **Step 4: Implement the detection**

Replace the existing 401/403 branch in `server.py` (lines 56–60):

```python
        if e.status_code in (401, 403):
            return (
                f"Strava API: unauthorized ({e.path}). The access token may be "
                "expired or revoked; reseed STRAVA_ACCESS_TOKEN / STRAVA_REFRESH_TOKEN."
            )
```

with:

```python
        if e.status_code in (401, 403):
            detail_lower = (e.detail or "").lower()
            scope_markers = ("activity:read_permission", "missing scope", "insufficient scope")
            if any(m in detail_lower for m in scope_markers):
                return (
                    f"Strava API: insufficient scope on {e.path}. The current "
                    "tokens are missing 'activity:read_all'. Re-run the OAuth "
                    "flow with scope=read,activity:read_all and update "
                    "STRAVA_ACCESS_TOKEN / STRAVA_REFRESH_TOKEN. "
                    "See README#oauth-get-your-access-tokens."
                )
            return (
                f"Strava API: unauthorized ({e.path}). The access token may be "
                "expired or revoked; reseed STRAVA_ACCESS_TOKEN / STRAVA_REFRESH_TOKEN."
            )
```

- [ ] **Step 5: Run the tests to confirm they pass**

```bash
cd /Users/sethneal/Documents/Claude/Code/strava-mcp-vault
.venv/bin/python -m pytest tests/test_server.py::test_format_error_401_with_scope_hint_says_rerun_oauth tests/test_server.py::test_format_error_401_without_scope_hint_says_reseed -v
```

Expected: both PASS.

- [ ] **Step 6: Run the full test suite to confirm no regressions**

```bash
cd /Users/sethneal/Documents/Claude/Code/strava-mcp-vault
.venv/bin/python -m pytest -x
```

Expected: all tests pass. If anything fails, fix before moving on.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "$(cat <<'EOF'
feat(server): distinguish scope-related 401s from expired-token 401s

The previous 401/403 message always recommended reseeding the env var
tokens. When the real cause is missing OAuth scope (the most common
case in practice — e.g. tokens minted without activity:read_all),
reseeding alone won't fix anything: the user needs to re-run OAuth
with the correct scope parameter.

Sniff the response body for known scope markers and route those
failures to a more specific message that names activity:read_all and
points at the README OAuth section. Other 401s keep the original
reseed advice.

See docs/superpowers/specs/2026-05-22-multi-app-strava-tokens-design.md

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Push all commits to origin

**Files:** None changed in this task.

By this point the local branch has accumulated:
- `7c17417` `fix(server): default VAULT_DB_PATH to ./data outside Docker` (from before this plan)
- `71a3b12` `docs(spec): multi-app Strava token sharing design`
- Task 4's `docs(readme): correct refresh-token rotation warning`
- Task 5's `docs(readme): document sharing one Strava app across multiple services`
- Task 6's `feat(server): distinguish scope-related 401s from expired-token 401s`

Plus the plan file we wrote at the start of this implementation.

- [ ] **Step 1: Commit the plan file**

```bash
cd /Users/sethneal/Documents/Claude/Code/strava-mcp-vault
git add docs/superpowers/plans/2026-05-22-multi-app-strava-tokens.md
git commit -m "$(cat <<'EOF'
docs(plan): implementation plan for multi-app Strava token sharing

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 2: Verify the local branch is clean**

```bash
git status
```

Expected: `nothing to commit, working tree clean` and `Your branch is ahead of 'origin/main' by N commits`.

- [ ] **Step 3: Push**

```bash
git push
```

Expected: push succeeds, no rejections. Branch is now in sync with origin.

- [ ] **Step 4: Final verification**

```bash
git log --oneline origin/main..HEAD
```

Expected: empty (everything is pushed).
