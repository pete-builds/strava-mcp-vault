import asyncio
import logging
import time

import httpx

from clients.base import BaseClient

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    pass


class StravaClient(BaseClient):
    """Strava API v3 client with OAuth token management and rate limit tracking."""

    def __init__(self, client_id: str, client_secret: str, cache_db):
        super().__init__("https://www.strava.com/api/v3")
        self.client_id = client_id
        self.client_secret = client_secret
        self._cache_db = cache_db
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: int = 0
        self._rate_limit_usage: str | None = None  # "usage,limit" from header
        self._rate_limit_limit: str | None = None

    async def init_tokens(self):
        """Load tokens from cache_db.

        If None is returned, this is the first boot and the caller must seed
        tokens from environment variables.
        """
        tokens = await self._cache_db.get_tokens()
        if tokens is None:
            logger.info("No cached tokens found; caller must seed from env vars")
            return
        self._access_token = tokens["access_token"]
        self._refresh_token = tokens["refresh_token"]
        self._expires_at = tokens["expires_at"]
        logger.info("Loaded tokens from cache (expires_at=%d)", self._expires_at)

    async def _ensure_valid_token(self):
        """Refresh the access token if it expires within 5 minutes.

        Uses a lock so only one coroutine refreshes at a time.
        """
        if self._expires_at - time.time() > 300:
            return
        async with self._token_lock:
            # Double-check after acquiring the lock; another coroutine may
            # have already refreshed while we waited.
            if self._expires_at - time.time() > 300:
                return
            await self._refresh_token_request()

    async def _refresh_token_request(self):
        """POST to Strava's OAuth endpoint to get a fresh access token.

        Strava may return a new refresh_token with every refresh, so we
        always persist whatever comes back.
        """
        logger.info("Refreshing Strava access token")
        resp = await self._client.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._expires_at = data["expires_at"]

        await self._cache_db.set_tokens(
            self._access_token,
            self._refresh_token,
            self._expires_at,
        )
        logger.info("Token refreshed, new expires_at=%d", self._expires_at)

    async def _get(self, path: str, **kwargs) -> dict | list:
        """GET with automatic token refresh, auth header injection, and rate
        limit tracking.

        Retries once on transient connection errors (same pattern as
        BaseClient).
        """
        await self._ensure_valid_token()

        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token}"

        for attempt in range(2):
            try:
                resp = await self._client.get(url, headers=headers, **kwargs)

                # Track rate limit headers regardless of status code
                usage = resp.headers.get("X-RateLimit-Usage")
                limit = resp.headers.get("X-RateLimit-Limit")
                if usage:
                    self._rate_limit_usage = usage
                if limit:
                    self._rate_limit_limit = limit

                if resp.status_code == 429:
                    raise RateLimitError(
                        f"Strava rate limit exceeded (usage: {usage}, limit: {limit})"
                    )

                resp.raise_for_status()
                return resp.json()
            except (httpx.RemoteProtocolError, httpx.ConnectError):
                if attempt == 0:
                    continue
                raise

    @property
    def rate_limit_remaining(self) -> dict | None:
        """Return parsed rate limit info, or None if no data is available yet.

        Returns a dict with short-term and long-term usage and limits:
            {
                "short": {"usage": int, "limit": int},
                "long":  {"usage": int, "limit": int},
            }
        """
        if self._rate_limit_usage is None or self._rate_limit_limit is None:
            return None
        try:
            usage_parts = self._rate_limit_usage.split(",")
            limit_parts = self._rate_limit_limit.split(",")
            return {
                "short": {
                    "usage": int(usage_parts[0]),
                    "limit": int(limit_parts[0]),
                },
                "long": {
                    "usage": int(usage_parts[1]),
                    "limit": int(limit_parts[1]),
                },
            }
        except (IndexError, ValueError):
            return None

    # ── Strava API methods ──────────────────────────────────────────────

    async def get_athlete(self) -> dict:
        """GET /athlete - returns the authenticated athlete's profile."""
        return await self._get("/athlete")

    async def get_activities(
        self,
        page: int = 1,
        per_page: int = 30,
        after: int | None = None,
    ) -> list:
        """GET /athlete/activities - returns a list of the athlete's activities."""
        params = {"page": page, "per_page": per_page}
        if after is not None:
            params["after"] = after
        return await self._get("/athlete/activities", params=params)

    async def get_activity(self, activity_id: int) -> dict:
        """GET /activities/{id} - returns a single activity by ID."""
        return await self._get(f"/activities/{activity_id}")

    async def get_activity_streams(
        self,
        activity_id: int,
        stream_types: list[str],
    ) -> dict:
        """GET /activities/{id}/streams - returns time-series data streams.

        stream_types: list of stream keys, e.g. ["time", "heartrate", "latlng"]
        """
        params = {
            "keys": ",".join(stream_types),
            "key_type": "time",
        }
        return await self._get(f"/activities/{activity_id}/streams", params=params)

    async def get_athlete_stats(self, athlete_id: int) -> dict:
        """GET /athletes/{id}/stats - returns the athlete's aggregate stats."""
        return await self._get(f"/athletes/{athlete_id}/stats")
