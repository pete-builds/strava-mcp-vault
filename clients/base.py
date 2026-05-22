import httpx


class BaseClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def close(self):
        await self._client.aclose()
