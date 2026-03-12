# strava-mcp-vault

A Strava MCP server that caches your data locally in SQLite, so you own it.

## Why build this?

Strava's rate limits are tight: 100 requests per 15 minutes, 1,000 per day. Every time Claude asks "how far did I run this week?" it burns API calls. Tokens expire every 6 hours, and if your server doesn't handle refresh, it just breaks.

This server solves all of that:

- **SQLite caching** with configurable TTLs per data type (1 hour to 7 days)
- **Automatic OAuth token refresh** with race-condition-safe async locking
- **Bulk sync** to pull 30+ days of activity history into cache with one command
- **Rate limit awareness** by tracking remaining API budget from Strava's response headers
- **Offline access** for any previously cached data
- **SSE transport** via FastMCP for network-wide access from any machine

## How it differs from other Strava MCP servers

Several Strava MCP implementations exist. They're all thin API wrappers:

- They proxy every request to Strava's API (no caching)
- They don't handle token refresh (tokens expire, server breaks)
- They don't persist data locally

strava-mcp-vault takes a different approach:

- Cache-aside architecture: check SQLite first, hit the API only on cache miss
- Tokens stored in SQLite, refreshed automatically before expiration
- Paginated bulk sync pulls entire activity histories without manual intervention
- Hit/miss tracking so you can see exactly how the cache is performing

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

## Prerequisites

- Docker and Docker Compose
- A Strava account
- A Strava API application (create one at <https://www.strava.com/settings/api>)

## OAuth Walkthrough

This is the hardest part, and Strava's docs don't make it easy. Here's what actually works.

### Set your callback domain

When creating your Strava API app, Strava requires a real domain for the "Authorization Callback Domain." Localhost won't work. Use whatever domain you own (your personal site, anything). It doesn't need to run a web server.

### Build the authorization URL

> **CRITICAL: You MUST include `activity:read_all` in the scope parameter.** The default `read` scope only gives profile access. Without `activity:read_all`, every activity request returns a 401 with `"field": "activity:read_permission", "code": "missing"`. This is the #1 gotcha and it's poorly documented.

```
https://www.strava.com/oauth/authorize?client_id=YOUR_CLIENT_ID&redirect_uri=https://YOUR_DOMAIN&response_type=code&scope=read,activity:read_all
```

### Authorize and grab the code

Open that URL in your browser. Authorize the app. Strava redirects to your callback domain. The page will probably 404 or show your unrelated website. That's fine. Look at the URL bar: it contains `?code=XXXXXXXXXX`. Copy that code.

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

SUGGEST ADDING INSTRUCTI)NS ON WHICH IP TO GET AND HOW TO GET IT.  ADD URL FOR APP HOME TO STARTUP.

Add to your MCP config:

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

Or via CLI:

```bash
claude mcp add strava --transport sse --url http://YOUR_SERVER_IP:18201/sse
```

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
