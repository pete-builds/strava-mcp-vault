"""strava-mcp-vault: a caching MCP server for Strava activity data.

Wraps the Strava v3 API behind an MCP tool surface, with a local SQLite vault
so repeated queries do not spend rate-limit budget, automatic OAuth token
refresh, and encrypted token storage at rest.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
