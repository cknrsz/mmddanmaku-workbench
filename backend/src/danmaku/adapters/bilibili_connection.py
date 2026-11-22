import asyncio

import httpx

from danmaku.adapters.bilibili_sender import NAV_URL


async def probe_bilibili(client: httpx.AsyncClient) -> bool:
    """Check HTTPS reachability without credentials or downloading a response body."""
    try:
        async with asyncio.timeout(1):
            async with client.stream("HEAD", NAV_URL):
                return True
    except (httpx.RequestError, TimeoutError):
        return False
