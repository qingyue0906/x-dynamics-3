from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urljoin

import bs4

from .config import Config
from .errors import XTransactionError
from .gql import extract_script_urls
from .http import HttpClient

logger = logging.getLogger(__name__)

INDICES_RE = re.compile(r"(\(\w{1}\[(\d{1,2})\],\s*16\))+", re.MULTILINE)
JS_REF_RE = re.compile(r'(?:from|import)\s*\(?\s*[`"\']((?:\.{1,2}/)[^`"\']+?\.js)')
PAGE_TTL = 6 * 60 * 60


class TransactionProvider:
    """Loads x.com signing material and generates X-Client-Transaction-Id.

    Signing material is build-wide, not account-specific. X's current x-web
    home/profile pages do not expose ondemand.s, while the public legacy
    /i/embed page still does. We therefore fetch signing material with a clean,
    cookie-free client first, then fall back to the authenticated pages.
    """

    def __init__(self, config: Config, http: HttpClient) -> None:
        self.config = config
        self.http = http
        self._public_http: HttpClient | None = None
        self._ct = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()
        self.last_diagnostics: list[str] = []

    @property
    def loaded(self) -> bool:
        return self._ct is not None

    async def _public_client(self) -> HttpClient:
        if self._public_http is None:
            self._public_http = HttpClient(
                self.config,
                headers={
                    "Referer": "https://x.com/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                cookies={},
            )
        return self._public_http

    async def public_get(self, url: str, **kwargs):
        client = await self._public_client()
        return await client.get(url, **kwargs)

    async def reset_public(self) -> None:
        if self._public_http is not None:
            await self._public_http.close()
            self._public_http = None

    async def close(self) -> None:
        await self.reset_public()

    async def refresh(self) -> None:
        async with self._lock:
            self._ct = None
            self._loaded_at = 0.0
        await self.ensure()

    async def ensure(self) -> None:
        if self._ct is not None and time.time() - self._loaded_at < PAGE_TTL:
            return
        async with self._lock:
            if self._ct is not None and time.time() - self._loaded_at < PAGE_TTL:
                return
            self.last_diagnostics = []
            sources = (
                ("i/embed(public)", "public", "https://x.com/i/embed"),
                ("tesla(auth)", "auth", "https://x.com/tesla"),
                ("home(auth)", "auth", "https://x.com/home"),
            )
            for label, client_kind, page in sources:
                try:
                    client = await self._public_client() if client_kind == "public" else self.http
                    response = await client.get(page, headers={"Referer": "https://x.com/"})
                    if response.status_code != 200:
                        self.last_diagnostics.append(f"{label}: HTTP {response.status_code}")
                        continue
                    soup = bs4.BeautifulSoup(response.text, "html.parser")
                    has_verification = bool(soup.select_one("meta[name='twitter-site-verification']"))
                    anim_count = len(soup.select("[id^='loading-x-anim']"))
                    ondemand = await self._extract_signing(soup, response.text, client)
                    if not ondemand:
                        self.last_diagnostics.append(
                            f"{label}: HTTP 200; verification={'yes' if has_verification else 'no'}; "
                            f"anim={anim_count}; signing JS not found"
                        )
                        continue
                    if not has_verification or anim_count == 0:
                        self.last_diagnostics.append(
                            f"{label}: HTTP 200; signing JS found but verification/anim missing"
                        )
                        continue
                    self._ct = self._build_transaction(soup, ondemand)
                    self._loaded_at = time.time()
                    self.last_diagnostics.append(
                        f"{label}: HTTP 200; verification=yes; anim={anim_count}; transaction=ok"
                    )
                    logger.info("XClientTransaction 已初始化（%s）", label)
                    return
                except Exception as exc:
                    self.last_diagnostics.append(f"{label}: {type(exc).__name__}: {exc}")
                    logger.debug("初始化 XClientTransaction 失败 %s：%s", label, exc)
            detail = "\n".join(f"  - {line}" for line in self.last_diagnostics)
            raise XTransactionError("无法初始化 X-Client-Transaction-Id：\n" + detail)

    def _build_transaction(self, soup, ondemand: str):
        try:
            from x_client_transaction import ClientTransaction
        except Exception as exc:
            raise RuntimeError(
                "未安装 XClientTransaction，请先 pip install -r requirements.txt"
            ) from exc
        return ClientTransaction(soup, ondemand)

    async def _extract_signing(self, soup, html: str, client: HttpClient) -> str:
        try:
            from x_client_transaction.utils import get_ondemand_file_url
            ondemand_url = get_ondemand_file_url(soup)
            response = await client.get(ondemand_url)
            if response.status_code == 200 and response.text:
                return response.text
        except Exception as exc:
            logger.debug("XClientTransaction 默认查找签名脚本失败：%s", exc)
        return await self._find_signing_js(html, client)

    async def _find_signing_js(self, html: str, client: HttpClient) -> str:
        urls = extract_script_urls(html)
        urls = [
            url for url in urls
            if "/i18n/" not in url and "/icons/" not in url
            and "react-syntax-highlighter" not in url
        ]
        semaphore = asyncio.Semaphore(10)

        async def fetch_many(items: list[str]) -> list[tuple[str, str]]:
            async def one(url: str):
                async with semaphore:
                    try:
                        response = await client.get(url)
                    except Exception:
                        return url, ""
                    if response.status_code != 200 or not response.text:
                        return url, ""
                    return url, response.text

            return await asyncio.gather(*(one(url) for url in items), return_exceptions=False)

        seen = set(urls)
        bodies = await fetch_many(urls[:240])
        for _, text in bodies:
            if text and INDICES_RE.search(text):
                return text

        refs: list[str] = []
        for base_url, text in bodies:
            for ref in JS_REF_RE.findall(text or ""):
                url = urljoin(base_url, ref)
                if url not in seen:
                    seen.add(url)
                    refs.append(url)
        if refs:
            bodies = await fetch_many(refs[:200])
            for _, text in bodies:
                if text and INDICES_RE.search(text):
                    return text
        return ""

    async def generate(self, method: str, path: str) -> str:
        await self.ensure()
        assert self._ct is not None
        try:
            return self._ct.generate_transaction_id(method=method.upper(), path=path)
        except Exception as exc:
            logger.warning("XClientTransaction 生成失败，重建签名材料后重试：%s", exc)
            self._ct = None
            self._loaded_at = 0.0
            await self.ensure()
            assert self._ct is not None
            return self._ct.generate_transaction_id(method=method.upper(), path=path)
