import httpx


class BaseClient:
    def __init__(self, base_url: str, api_key: str = None):
        self.base_url = base_url.rstrip("/")
        headers = {}
        if api_key:
            headers["X-Api-Key"] = api_key
        self._client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers=headers,
        )

    async def _get(self, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        for attempt in range(2):
            try:
                resp = await self._client.get(url, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except (httpx.RemoteProtocolError, httpx.ConnectError):
                if attempt == 0:
                    continue
                raise

    async def close(self):
        await self._client.aclose()
