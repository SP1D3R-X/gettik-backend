"""
Shared Asynchronous HTTP Client with Connection Pooling & Keep-Alive
Optimized for high-concurrency, low-latency TikTok API & web requests.
"""

import httpx
from typing import Optional

_client: Optional[httpx.AsyncClient] = None

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}


def get_async_http_client() -> httpx.AsyncClient:
    """Get or create singleton httpx.AsyncClient with connection pooling."""
    global _client
    if _client is None or _client.is_closed:
        limits = httpx.Limits(
            max_keepalive_connections=25,
            max_connections=60,
            keepalive_expiry=30.0
        )
        transport = httpx.AsyncHTTPTransport(
            limits=limits,
            retries=1
        )
        _client = httpx.AsyncClient(
            transport=transport,
            headers=DEFAULT_HEADERS,
            timeout=httpx.Timeout(6.0, connect=3.0),
            follow_redirects=True
        )
    return _client


async def close_async_http_client():
    """Gracefully close HTTP client."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None

