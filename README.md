# strava-mcp-vault

An unofficial, custom-built MCP server that lets your AI assistant talk to your Strava data. Connect it to Claude Code (or any MCP-compatible client) and ask questions like "how far did I run this week?" or "show me my ride stats for January." It pulls your activities, stats, and streams from Strava's API and stores everything in a local SQLite vault so you're not hitting the API every time.

This is not affiliated with or endorsed by Strava. It's a personal project built to scratch an itch.

## What it does

- Connects your AI to Strava through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
- Caches your activity data locally in SQLite so repeat queries are instant
- Handles OAuth token refresh automatically (Strava tokens expire every 6 hours)
- Formats output with sport-specific stats, emoji labels, and markdown tables
- Supports bulk sync to pull your full activity history into the local vault
- Runs as a Docker container with Streamable HTTP transport for network-wide access

## Why a server instead of running locally?

Most MCP servers run on your local machine alongside your AI client. This one is designed to run on a separate server (a homelab box, a VPS, anything with Docker) for a few reasons:

- **Access from any machine.** Connect from your laptop, your desktop, or any device running Claude Code. One server, multiple clients.
- **Your vault stays put.** The SQLite database lives on the server in a Docker volume. You don't lose your cached data when you reimage a laptop or switch machines.
- **Always running.** Token refresh happens in the background even when your laptop is off. Your data stays fresh.
- **Backups are simpler.** One database file on one server. Back it up however you back up everything else.

If you only use one machine, this works fine running locally too. The Docker setup is the same either way.

## Why not just use the Strava API directly?

Strava's rate limits are tight: 100 requests per 15 minutes, 1,000 per day. Every time your AI asks a question, it burns API calls. Other Strava MCP servers exist, but they're thin API wrappers that proxy every request, don't cache anything, and break when tokens expire.

strava-mcp-vault takes a different approach:

- **Cache-aside architecture:** check SQLite first, hit the API only on cache miss
- **Automatic token management:** tokens stored in SQLite, refreshed before expiration
- **Bulk sync:** paginated import pulls entire activity histories without manual intervention
- **Offline access:** anything previously cached works without an internet connection
- **Hit/miss tracking:** see exactly how the cache is performing and how much API budget remains

For a simpler setup that just wraps the existing npm package in Docker, see [strava-mcp-docker](https://github.com/pete-builds/strava-mcp-docker).

## Tools

| Tool | Description | Cache TTL |
|------|-------------|-----------|
| `get_recent_activities` | List recent activities with distance, time, HR | 1 hour |
| `get_activity` | Full activity detail (segments, splits, gear) | 24 hours |
| `get_activity_streams` | Time-series data (heart rate, elevation, GPS) | 7 days |
| `get_athlete_profile` | Authenticated athlete info | 24 hours |
| `get_athlete_stats` | YTD and all-time totals | 1 day |
| `get_cache_stats` | Cache hit/miss rates, API rate limits (general and read), recent syncs | none |
| `sync_activities` | Bulk-sync recent activities into cache | varies |
| `check_vault` | Compare the vault with Strava: what is missing, what is gone, by month | none |
| `repair_vault` | Add what `check_vault` found missing; delete only with `delete_gone=true` | none |
| `query_vault` | Filter and aggregate cached activities by date, sport type | none |
| `get_activities_near` | Vault activities that started near a place name | none |
| `set_activity_location` | Hand-label an indoor or GPS-less activity | none |
| `delete_vault_activity` | Remove activities from the local vault | none |
| `set_ride_spot` | Name a riding location so it can be published | none |
| `list_ride_spots` | Show the curated spots the export may publish | none |
| `export_ride_spots` | Curated ride spots as JSON for a public page | none |
| `get_route_track` | Full-resolution route geometry, clipped, for GPX | 7 days |
| `sync_route_tracks` | Warm the stream cache one rate-limited batch at a time | varies |
| `export_spot_photos` | A photo per curated spot, EXIF location stripped | 7 days |

## Publishing ride spots

`export_ride_spots` exists to feed a public web page, so it is built to publish
nothing by default rather than to publish what it has.

A spot appears only after you name it with `set_ride_spot`. There is no
fallback that names a cluster from a reverse geocode, and that omission is the
point: clustering ride start points and labelling the clusters automatically
sounds right and is not. On a real vault the largest cluster of bike activities
was 59 rides starting in a neighborhood, outranking the trail system that came
third. Auto-naming publishes a home address first and largest.

Three guarantees the export makes, each pinned by a test that fails when the
guarantee is removed:

- **Only `visibility == "everyone"` is published.** A missing, empty, or
  unrecognized visibility is withheld. Strava has changed this field before, so
  an unknown value fails closed instead of open.
- **Uncurated clusters are counted, never named.** The payload reports a bare
  `unassigned_rides` number, because those coordinates are exactly what must
  stay off a public page.
- **No recorded start coordinate is ever emitted.** Only route polylines go
  out, and a spot's map pin is the coordinate you curated. Strava's own
  privacy-zone trimming is not a substitute: it is applied inconsistently, and
  `start_latlng` is the raw recorded start regardless.

Curated spots live in their own `ride_spots` table, which `sync_activities`
never writes. A re-sync also keeps any location set with
`set_activity_location`; older versions wiped those.

### Route tracks, and why they are clipped

`get_route_track` exists because `summary_polyline` is decimated: it is enough to
draw a line on a map and not enough for a GPX you would follow. The `latlng`
stream behind it is full resolution and carries elevation.

**The stream is not privacy-zone trimmed and the polyline is.** Across the 256
activities in one real vault, the gap between `start_latlng` and the first point
of `summary_polyline` had a median of 344 ft and a p90 of 954 ft, while 95 of
256 sat within 50 ft. So Strava does hide the start of many activities, does it
inconsistently, and hides it only in the polyline. Emitting a raw stream
republishes every start Strava was concealing, on routes that look unchanged.

So the polyline is treated as the authority for where a route may begin and end,
and the stream is clipped to it. Where the recording cannot be matched to its
polyline the tool refuses rather than guessing, because a track nobody can bound
is one that may expose a trimmed start.

Defaults are tuned for files you can actually ship: 5 m simplification (inside
phone GPS noise, so it removes jitter rather than shape), 5 decimal places, and
elevation kept with timestamps off. A 5,000 point recording becomes about 300
points, which is roughly 4 MB across 175 rides instead of 87 MB.

`sync_route_tracks` warms the cache in batches, defaulting to 80 to stay inside
Strava's 100-per-15-minutes limit. Streams cache for 7 days.

### Photos

`export_spot_photos` picks a photo per curated spot for a page to self-host.
Strava's photo URLs are CDN renditions that rotate, so download them rather than
hotlinking.

**Every Strava photo carries an EXIF `location`**, a full-precision coordinate of
where the shutter was pressed. It is unrelated to the trimmed route, and on a
photo taken before setting off it is the athlete's front door. It is stripped
here rather than left to a caller to remember, and `tests/test_photos.py` fails
if it ever survives.

```
set_ride_spot(name="Shindagin Hollow", lat=42.3451, lon=-76.3505, radius_miles=1.0)
export_ride_spots(sport_types="Ride,MountainBikeRide,GravelRide")
```

## Example Output

Ask your AI "show me my recent activities" and you'll get formatted, sport-specific cards:

```
## 🏃 Recent Activities (3)

### 🚴 Morning Commute
Ride | Mar 10, 2026 3:45 PM

📏 Distance: 5.50 mi | 🚀 Speed: 12.3 mph | ⏱️ Time: 0:27:34 | ⛰️ Elevation: 245 ft
❤️ Avg HR: 145 bpm | 💓 Max HR: 167 bpm | 🔥 Calories: 450

### 🏃 Evening Run
Run | Mar 9, 2026 6:15 PM

📏 Distance: 3.20 mi | 🏃 Pace: 8:59/mi | ⏱️ Time: 0:28:45 | ⛰️ Elevation: 125 ft
❤️ Avg HR: 152 bpm | 💓 Max HR: 175 bpm
```

Or ask for a compact table view with `compact: true`:

```
## 📋 Activities (5)

| # | Date   | Type | Name            | Distance | Time    | Elevation | HR  |
|---|--------|------|-----------------|----------|---------|-----------|-----|
| 1 | Mar 10 | 🚴   | Morning Commute | 5.5mi    | 0:27:34 | 245 ft    | 145 |
| 2 | Mar 9  | 🏃   | Evening Run     | 3.2mi    | 0:28:45 | 125 ft    | 152 |
| 3 | Mar 8  | 🏊   | Pool Swim       | 1500yd   | 0:32:10 | N/A       | 128 |
```

Use `query_vault` to get aggregated stats from your cached data without hitting the API:

```
## 🔍 Vault Query Results

Filter: type=Ride, after 2026-01-01
Total Activities: 24

📏 Distance: 342.5 mi | ⏱️ Time: 28.4 hours | ⛰️ Elevation: 12,450 ft
```

## Prerequisites

- Docker and Docker Compose
- A Strava account
- A Strava API application (see below)

## Setup

### Create a Strava API application

1. Go to <https://www.strava.com/settings/api>
2. Fill in the form:
   - **Application Name:** Whatever you want (e.g., "My MCP Server")
   - **Category:** Choose any
   - **Club:** Leave blank
   - **Website:** Any URL (e.g., `https://example.com`)
   - **Authorization Callback Domain:** `localhost`. You do not need a domain of your own. Strava's [authentication documentation](https://developers.strava.com/docs/authentication/) says of the redirect address: "`localhost` and `127.0.0.1` are white-listed." (Earlier versions of this README said the callback could not be `localhost`. That was wrong.)
3. After creating the app, you'll see your **Client ID** and **Client Secret** on the app settings page. Keep the page open; the next step asks for both.

### Sign in to Strava with one command

From the folder you cloned, on a computer with a web browser:

```bash
cp .env.example .env
python3 src/strava_mcp_vault/login.py --env-file .env
```

It asks for your Client ID and Client Secret (the secret is not shown as you type, and it skips the question for any value already in `.env`), opens Strava in your browser, and waits. Click **Authorize** on Strava's page and leave every box ticked. The terminal then shows:

```
Signed in to Strava as Alex. Tokens saved to .env (readable by you only).
Next step: docker compose up -d
```

That command needs only Python 3, no install. If you installed the package instead (`pip install .`), the same thing is available as `strava-mcp-vault-login`.

What it does to keep your credentials safe:

- The temporary listener that catches Strava's redirect accepts connections from this computer only (`127.0.0.1`), with a single 5 minute limit on the whole wait (`--timeout`). It closes as soon as the real redirect arrives or the wait ends. Only an exact `GET /callback` addressed to `localhost` or `127.0.0.1` on its own port is considered; anything else a browser or another program sends (a favicon, an idle connection, a lookalike path) gets a 404 and cannot use up the sign-in or stretch the limit.
- Each sign-in carries a fresh random `state` value. A `/callback` request without it is refused, nothing is stored, and the terminal says so in one line while it keeps waiting for the real one. After 10 such refusals it stops, because something else on your computer is sending them; run the command again.
- Tokens and the client secret are never printed or logged. `.env` is rewritten readable by you only. Each line it sets is written as plain `KEY=value` (an `export ` in front of one of those lines is dropped), the file is saved with Unix line endings, and comments and other settings keep their text.
- If `TOKEN_ENCRYPTION_KEY` is set but is not a valid key, it stops before opening the browser rather than let the server store tokens unencrypted. It only says tokens are encrypted when they were.
- It refuses to store tokens that lack the `activity:read_all` permission, since every activity request fails without it.

The server copies the tokens from `.env` into its vault the first time it starts with an empty vault. After that it refreshes them on its own and stores them in SQLite (encrypted if you set `TOKEN_ENCRYPTION_KEY`).

> **Already running with Docker? Running this command again changes nothing.** Once the server's vault holds tokens, it never reads `STRAVA_ACCESS_TOKEN` or `STRAVA_REFRESH_TOKEN` from `.env` again, so new tokens written there are ignored. You do not need to re-run it: the server keeps its own tokens fresh.

**Server with no browser (reached over SSH):** forward a port, then run the command on the server and open the address it prints in your own browser:

```bash
ssh -L 8765:127.0.0.1:8765 you@your-server
python3 src/strava_mcp_vault/login.py --env-file .env --port 8765 --no-browser
```

**Without Docker:** run `strava-mcp-vault-login` with no `--env-file`. It writes the tokens straight into the vault at `VAULT_DB_PATH` (read from `.env` the same way the server reads it), through the server's own storage code, so `TOKEN_ENCRYPTION_KEY` applies. Then start the server with `strava-mcp-vault`.

### Manual fallback: get your tokens by hand

Use this only if the command above cannot run on your machine.

> **CRITICAL: You MUST include `activity:read_all` in the scope parameter.** The default `read` scope only gives profile access. Without `activity:read_all`, every activity request returns a 401 with `"field": "activity:read_permission", "code": "missing"`. This is the #1 gotcha and it's poorly documented.

**Step 1: Build the authorization URL**

```
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=http://localhost/exchange_token&response_type=code&approval_prompt=force&scope=read,activity:read_all
```

Replace `YOUR_CLIENT_ID` with the Client ID from your app settings.

**Step 2: Authorize and grab the code**

Open that URL in your browser and authorize the app. Strava redirects to `localhost`, where nothing is listening, so the browser shows an error page. That is expected. The only thing you need is the **authorization code in your browser's address bar**, which will look something like:

```
http://localhost/exchange_token?state=&code=abc123def456ghi789&scope=read,activity:read_all
```

Copy the value between `code=` and `&scope` (in this example, `abc123def456ghi789`). That's your one-time authorization code for the next step.

**Step 3: Exchange the code for tokens**

```bash
curl -X POST https://www.strava.com/oauth/token \
  -d client_id=YOUR_CLIENT_ID \
  -d client_secret=YOUR_CLIENT_SECRET \
  -d code=YOUR_CODE \
  -d grant_type=authorization_code
```

Copy `access_token` and `refresh_token` from the JSON response into your `.env` file. After first boot, the server manages token refresh automatically in SQLite. You won't need to do this again.

## Quick Start

```bash
git clone https://github.com/pete-builds/strava-mcp-vault.git
cd strava-mcp-vault
cp .env.example .env
python3 src/strava_mcp_vault/login.py --env-file .env
docker compose up -d
```

The server starts on port 18201 by default. Change it with `STRAVA_MCP_PORT` in your `.env`.

## Connecting to Claude Code

Once the container is running, you need to register it as an MCP server so Claude Code can use the tools. The MCP endpoint is `http://YOUR_SERVER_IP:18201/mcp` (Streamable HTTP).

**Which IP to use:** Use the IP of the machine running the Docker container, not `localhost` (unless Claude Code runs on the same machine). If you're on a Tailscale network, use the Tailscale IP. If running everything on one machine, `localhost` or `127.0.0.1` works.

**Authentication:** If you set `MCP_AUTH_TOKEN` in your `.env` (recommended), you need to pass it as a Bearer token header when registering. If you didn't set one, the server accepts unauthenticated requests.

Via CLI (recommended):

```bash
# With auth token:
claude mcp add strava http://YOUR_SERVER_IP:18201/mcp --transport http -H "Authorization: Bearer YOUR_MCP_AUTH_TOKEN"

# Without auth:
claude mcp add strava http://YOUR_SERVER_IP:18201/mcp --transport http
```

Or add it to your MCP config JSON manually:

```json
{
  "mcpServers": {
    "strava": {
      "type": "http",
      "url": "http://YOUR_SERVER_IP:18201/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_AUTH_TOKEN"
      }
    }
  }
}
```

> Previously used the HTTP+SSE transport (`/sse` endpoint) which was deprecated in the MCP spec 2025-03-26. Migrated to Streamable HTTP (MCP spec 2025-06-18) in v0.2.0. Existing clients must re-register with the new URL and transport.

**Verify it works:** Restart Claude Code and ask something like "What are my recent Strava activities?" If the MCP connection is healthy, Claude will call the `get_recent_activities` tool and return your data. You can also run `get_cache_stats` to confirm the server is responding.

## Cache Behavior

Each data type has its own TTL, tuned to how often the underlying data changes:

- **Activity lists** refresh every hour (new activities show up)
- **Individual activities** cache for 24 hours (they rarely change after upload)
- **Stream data** (heart rate, GPS, elevation) caches for 7 days (immutable once recorded)
- **Athlete stats** refresh daily (YTD totals update with each activity)

Run `sync_activities` after first setup to pull your recent history into the cache. This makes subsequent queries fast and avoids burning API calls on data you've already fetched.

Cached data persists across container restarts through a Docker volume (`strava-data`). Use `get_cache_stats` to check hit/miss rates and see how much of your API budget remains.

## Is my vault complete?

A routine sync only asks Strava for activities newer than the newest one already
stored, so it can never fill a gap behind that point, and it reports success
either way. Rides deleted on Strava also stay in the vault. To find out, ask:

```
check_vault(after="2026-01-01", sport_types="Ride,MountainBikeRide,GravelRide")
```

It reads Strava's activity list for that window (about one call per 200
activities), changes nothing, and reports each side's count and distance, a
per-month table, every activity on Strava that the vault lacks, every activity
re-dated or re-typed on Strava, and every vault activity missing from Strava's
list. It says the vault matches only when the counts agree and the distances
agree within a metre. Leave out `after` and `before` to check the whole account.

To fix what it found, run `repair_vault` with the same arguments. It stores the
activities through the same update a sync uses, so a location you set with
`set_activity_location` is kept. If Strava answers for a different athlete than
the vault belongs to (a token from another account), it stores and deletes
nothing.

Activities missing from Strava's list are kept. A ride can be absent from the
list because the token cannot see it or because Strava's answer was incomplete,
so `delete_gone=true` deletes one only when all of these hold: the vault knows
its token holds `activity:read_all` (recorded when `strava-mcp-vault-login`
writes to the vault; a token seeded from environment variables, including the
`--env-file` Docker setup, has no record, so deletes are refused), the vault's
stored activities name its Strava athlete, the whole window was read, Strava's side of it is not empty, the number to delete is at most 10
or 5 percent of the window (whichever is larger; `max_delete` changes that for
one run), the ride was not stored as private, and Strava answers 404 when asked
for that ride by id. Anything else keeps the ride and the report says why. The
report always shows the delete limit that applied, and `max_delete` is only
accepted together with `delete_gone`. Sport names are matched without regard to
case, and a window where both sides have nothing says so instead of "matches".

**Back up the vault before using `delete_gone`.** With the shipped
`docker-compose.yml` and the default `VAULT_DB_PATH`, this writes a consistent
copy next to the vault on the `strava-data` volume (SQLite's own backup, which
includes the WAL; do not `docker cp` the live file):

```bash
docker exec strava-mcp-vault python -c "import sqlite3; sqlite3.connect('/app/data/vault.db').backup(sqlite3.connect('/app/data/vault-backup.db'))"
```

If you run it some other way, back up the vault file first.

Strava allows 100 read calls per 15 minutes and 1,000 a day. The check stops
while 5 are still left, says how far it got, and prints the exact call to
continue once the limit resets. `get_cache_stats` shows the read budget and the
last ten syncs, including any that did not finish, so a hole can be dated.

From Python (for a nightly job), the same logic is
`await CacheManager(db, client).check_vault(after="2026-01-01")`, which returns
the report as a dict.

## Development

Running locally without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Edit .env, set VAULT_DB_PATH=./data/vault.db
strava-mcp-vault-login
strava-mcp-vault
```

Requires Python 3.11+.

Run the tests with `pytest`, and the linters with `ruff check .` and
`ruff format --check .`. Both run in CI.

## Releasing and deploying

The image is published to GHCR by CI, not built on the host. `docker-compose.yml`
pins an explicit version so a restart can never silently change the running code.

To cut a release:

1. Bump the image tag in `docker-compose.yml` to the new version and commit it.
   The release workflow refuses to publish if this disagrees with the git tag,
   which stops a release from producing an image the compose does not reference.
2. Tag and push:

   ```bash
   git tag -a v0.3.0 -m "v0.3.0"
   git push origin v0.3.0
   ```

3. `.github/workflows/release.yml` builds `linux/amd64` and `linux/arm64`,
   pushes to `ghcr.io/pete-builds/strava-mcp-vault`, attaches an SBOM and a
   signed provenance attestation, and cuts a GitHub release.

4. On the host:

   ```bash
   docker compose pull && docker compose up -d
   ```

### Verify the deploy, do not trust a green pipeline

This server binds a non-loopback host on purpose. The MCP SDK enables
DNS-rebinding protection automatically when the bind host is loopback, which
makes the server answer `HTTP 421 Invalid Host header` to every LAN client
while every unit test still passes. CI cannot see that failure. After
deploying, confirm against the real listener rather than localhost:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Host: 192.168.86.20:18201' \
  -H 'Authorization: Bearer <token>' \
  http://192.168.86.20:18201/mcp
```

Expect `200`. A `421` means the bind host regressed. See
`tests/test_transport_host.py`, which pins this.

## Troubleshooting

**401 Authorization Error**: Wrong OAuth scopes. You need `activity:read_all`, not just `read`. See the [OAuth Walkthrough](#oauth-walkthrough).

**429 Rate Limit**: Strava caps at 100 requests per 15 minutes, 1,000 per day. Wait and retry. Use `sync_activities` to bulk-cache data and reduce future API calls.

**Container keeps restarting**: Check logs with `docker logs strava-mcp-vault`. Usually a missing or invalid `.env` variable.

**Token expired**: The server refreshes tokens automatically before they expire. If refresh fails (revoked app, changed password), re-run the OAuth flow and update your `.env` with fresh tokens.

## License

MIT
