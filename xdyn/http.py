from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Config

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)

BASE_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class CurlResponse:
    """Small adapter so XClient can use curl_cffi and httpx interchangeably."""

    def __init__(self, response: Any) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return int(self._response.status_code)

    @property
    def text(self) -> str:
        return self._response.text

    @property
    def content(self) -> bytes:
        return self._response.content

    @property
    def headers(self):
        return self._response.headers

    @property
    def url(self):
        return self._response.url

    def json(self):
        return self._response.json()


class HttpClient:
    def __init__(
        self,
        config: Config,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        self.config = config
        self.backend = (config.sync.api_backend or "httpx").lower()
        self._client = None
        self._curl = None
        all_headers = {**BASE_HEADERS, **(headers or {})}
        if self.backend == "curl":
            try:
                from curl_cffi.requests import AsyncSession
            except Exception as exc:
                raise RuntimeError(
                    "api_backend=curl 需要安装 curl-cffi：pip install curl-cffi"
                ) from exc
            self._curl = AsyncSession(
                impersonate="chrome",
                proxy=config.sync.proxy or None,
                headers=all_headers,
                cookies=cookies or {},
                timeout=timeout or config.sync.timeout,
                allow_redirects=True,
            )
        else:
            self._client = httpx.AsyncClient(
                proxy=config.sync.proxy or None,
                follow_redirects=True,
                timeout=httpx.Timeout(
                    timeout or config.sync.timeout,
                    connect=config.sync.connect_timeout,
                ),
                headers=all_headers,
                cookies=cookies or {},
            )

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._curl is not None:
            await self._curl.close()

    @property
    def cookies(self):
        return self._curl.cookies if self._curl is not None else self._client.cookies

    @property
    def headers(self):
        return self._curl.headers if self._curl is not None else self._client.headers

    async def get(self, url: str, **kwargs):
        if self._curl is not None:
            return CurlResponse(await self._curl.get(url, **kwargs))
        return await self._client.get(url, **kwargs)

    async def post(self, url: str, **kwargs):
        if self._curl is not None:
            return CurlResponse(await self._curl.post(url, **kwargs))
        return await self._client.post(url, **kwargs)

    async def request(self, method: str, url: str, **kwargs):
        if self._curl is not None:
            return CurlResponse(await self._curl.request(method, url, **kwargs))
        return await self._client.request(method, url, **kwargs)

    async def stream(self, method: str, url: str, **kwargs):
        if self._curl is not None:
            raise RuntimeError("curl 后端不用于媒体下载")
        return self._client.stream(method, url, **kwargs)
