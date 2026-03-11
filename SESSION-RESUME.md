# strava-mcp-vault: Session Resume

## What This Is
Custom Strava MCP server with SQLite caching. Built with Python FastMCP + httpx + aiosqlite.

## Architecture
- `server.py`: FastMCP app with 7 tools
- `clients/strava.py`: Strava API v3 client with auto token refresh
- `cache/manager.py`: Cache-aside orchestration with TTL per category
- `cache/db.py`: SQLite storage for cached data and OAuth tokens

## Key Files
- Port: 18201 (SSE transport)
- Database: /app/data/vault.db (Docker volume)
- Tokens: seeded from .env on first boot, then managed in SQLite

## Deploy Target
nix1 (pete@192.168.86.20), directory: ~/docker/strava-mcp-vault/

## GitHub
pete-builds/strava-mcp-vault
