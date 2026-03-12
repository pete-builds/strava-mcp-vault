# strava-mcp-vault

An unofficial, custom-built MCP server that lets your AI assistant talk to your Strava data. Connect it to Claude Code (or any MCP-compatible client) and ask questions like "how far did I run this week?" or "show me my ride stats for January." It pulls your activities, stats, and streams from Strava's API and stores everything in a local SQLite vault so you're not hitting the API every time.

This is not affiliated with or endorsed by Strava. It's a personal project built to scratch an itch.

## What it does

- Connects your AI to Strava through the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
- Caches your activity data locally in SQLite so repeat queries are instant
- Handles OAuth token refresh automatically (Strava tokens expire every 6 hours)
- Formats output with sport-specific stats, emoji labels, and markdown tables
- Supports bulk sync to pull your full activity history into the local vault
- Runs as a Docker container with SSE transport for network-wide access

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
| `get_cache_stats` | Cache hit/miss rates and API rate limit status | none |
| `sync_activities` | Bulk-sync recent activities into cache | varies |
| `query_vault` | Filter and aggregate cached activities by date, sport type | none |

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
- A Strava API application (create one at <https://www.strava.com/settings/api>)

## OAuth Walkthrough

This is the hardest part, and Strava's docs don't make it easy. Here's what actually works.

### Set your callback domain

When creating your Strava API app, Strava requires a real domain for the "Authorization Callback Domain." Localhost won't work. Use any domain you own, even if it has nothing to do with this project. A personal site, a business site, anything. It doesn't need to run a web server or have any special endpoint set up. You're only using it as a redirect target so you can grab the authorization code from the URL (explained below).

### Build the authorization URL

> **CRITICAL: You MUST include `activity:read_all` in the scope parameter.** The default `read` scope only gives profile access. Without `activity:read_all`, every activity request returns a 401 with `"field": "activity:read_permission", "code": "missing"`. This is the #1 gotcha and it's poorly documented.

```
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=https://YOUR_DOMAIN&response_type=code&scope=read,activity:read_all
```

### Authorize and grab the code

Open that URL in your browser. Authorize the app. Strava will redirect to your callback domain.

**Here's the trick:** The redirect page will 404 (or show your unrelated website). This is expected and totally fine. You don't need a working web server at that domain. The only thing you need is the **authorization code in your browser's address bar**.

After the redirect, your browser URL will look something like:

```
https://yourdomain.com/?state=&code=abc123def456ghi789&scope=read,activity:read_all
```

Copy the value between `code=` and `&scope` (in this example, `abc123def456ghi789`). That's your one-time authorization code for the next step.

### Exchange the code for tokens

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
# Edit .env with your credentials (see OAuth Walkthrough above)
docker compose up -d
```

The server starts on port 18201 by default. Change it with `STRAVA_MCP_PORT` in your `.env`.

## Connecting to Claude Code

Once the container is running, you need to register it as an MCP server so Claude Code can use the tools. The SSE endpoint is `http://YOUR_SERVER_IP:18201/sse`.

**Which IP to use:** Use the IP of the machine running the Docker container, not `localhost` (unless Claude Code runs on the same machine). If you're on a Tailscale network, use the Tailscale IP. If running everything on one machine, `localhost` or `127.0.0.1` works.

Via CLI (recommended):

```bash
claude mcp add strava --transport sse --url http://YOUR_SERVER_IP:18201/sse
```

Or add it to your MCP config JSON manually:

```json
{
  "mcpServers": {
    "strava": {
      "type": "sse",
      "url": "http://YOUR_SERVER_IP:18201/sse"
    }
  }
}
```

**Verify it works:** Restart Claude Code and ask something like "What are my recent Strava activities?" If the MCP connection is healthy, Claude will call the `get_recent_activities` tool and return your data. You can also run `get_cache_stats` to confirm the server is responding.

## Cache Behavior

Each data type has its own TTL, tuned to how often the underlying data changes:

- **Activity lists** refresh every hour (new activities show up)
- **Individual activities** cache for 24 hours (they rarely change after upload)
- **Stream data** (heart rate, GPS, elevation) caches for 7 days (immutable once recorded)
- **Athlete stats** refresh daily (YTD totals update with each activity)

Run `sync_activities` after first setup to pull your recent history into the cache. This makes subsequent queries fast and avoids burning API calls on data you've already fetched.

Cached data persists across container restarts through a Docker volume (`strava-data`). Use `get_cache_stats` to check hit/miss rates and see how much of your API budget remains.

## Development

Running locally without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env, set VAULT_DB_PATH=./data/vault.db
python server.py
```

Requires Python 3.13+.

## Troubleshooting

**401 Authorization Error**: Wrong OAuth scopes. You need `activity:read_all`, not just `read`. See the [OAuth Walkthrough](#oauth-walkthrough).

**429 Rate Limit**: Strava caps at 100 requests per 15 minutes, 1,000 per day. Wait and retry. Use `sync_activities` to bulk-cache data and reduce future API calls.

**Container keeps restarting**: Check logs with `docker logs strava-mcp-vault`. Usually a missing or invalid `.env` variable.

**Token expired**: The server refreshes tokens automatically before they expire. If refresh fails (revoked app, changed password), re-run the OAuth flow and update your `.env` with fresh tokens.

## License

MIT
