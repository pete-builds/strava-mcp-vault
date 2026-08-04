"""Custom exception hierarchy for strava-mcp-vault.

All vault-specific exceptions inherit from VaultError, which lets
server.py tool functions catch errors with a single except clause.
"""


class VaultError(Exception):
    """Base exception for all vault errors."""


class RateLimitError(VaultError):
    """Strava API rate limit exceeded."""


class StravaAPIError(VaultError):
    """Strava API returned a non-success response."""

    def __init__(self, status_code: int, path: str, detail: str = ""):
        self.status_code = status_code
        self.path = path
        self.detail = detail
        msg = f"Strava API error {status_code} on {path}"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class VaultDatabaseError(VaultError):
    """Error accessing the local vault database."""


class GeocodingError(VaultError):
    """Error resolving a location via geocoding."""
