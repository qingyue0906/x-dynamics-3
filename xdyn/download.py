from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .config import Config
from .db import Database
from .errors import JobCancelled, describe_error
from .http import DEFAULT_UA
from .utils import detect_media_kind, extension_for_content_type, media_kind, resolve_media_path

logger = logging.getLogger(__name__)

CHUNK = 64 * 1024
IMAGE_TYPES = ("image/", "application/octet-stream", "binary/octet-stream")
VIDEO_TYPES = ("video/", "application/octet-stream", "binary/octet-stream")


class InvalidMediaError(Exception):
    """A 200 response that is not the expected image/video payload."""


@dataclass
class DownloadStats:
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    dead: int = 0
    restricted: int = 0

    @property
    def total(self) -> int:
        return self.ok + self.skipped + self.failed + self.dead + self.restricted

    def merge(self, other: "DownloadStats") -> None:
        self.ok += other.ok
        self.skipped += other.skipped
        self.failed += other.failed
        self.dead += other.dead
        self.restricted += other.restricted


class Downloader:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self.session: httpx.AsyncClient | None = None
        self._session_lock = asyncio.Lock()

    def _new_session(self) -> httpx.AsyncClient:
        headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": "https://x.com/",
            "Accept": "image/avif,image/webp,image/apng,image/*,video/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        return httpx.AsyncClient(
            proxy=self.config.sync.proxy or None,
            follow_redirects=True,
            timeout=httpx.Timeout(
                self.config.sync.timeout,
                connect=self.config.sync.connect_timeout,
            ),
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=30),
            headers=headers,
        )

    async def _reset_session_if(self, session: httpx.AsyncClient | None) -> None:
        async with self._session_lock:
            if self.session is not session:
                return
            old = self.session
            self.session = self._new_session()
            if old is not None:
                try:
                    await old.aclose()
                except Exception as exc:
                    logger.debug("关闭旧下载连接失败：%s", describe_error(exc))

    async def __aenter__(self) -> "Downloader":
        self.session = self._new_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session:
            await self.session.aclose()

    def _target_path(self, row: dict) -> Path:
        target = resolve_media_path(
            self.config.data_dir,
            row.get("image_dir") or "",
            row.get("file_name") or "media.bin",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    async def download_rows(
        self,
        rows: list[dict],
        force: bool = False,
        max_media: int = 0,
        on_progress: Callable[[int, int], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> DownloadStats:
        if not rows:
            return DownloadStats()
        if max_media and max_media > 0:
            rows = rows[:max_media]
        total = len(rows)
        semaphore = asyncio.Semaphore(self.config.sync.download_concurrency)
        stats = DownloadStats()
        done = 0

        async def worker(row: dict) -> DownloadStats:
            nonlocal done
            if cancel_check:
                cancel_check()
            async with semaphore:
                result = await self._download_one(row, force=force)
            done += 1
            if on_progress:
                try:
                    on_progress(done, total)
                except Exception:
                    logger.exception("媒体下载进度回调失败")
            return result

        results = await asyncio.gather(*(worker(row) for row in rows), return_exceptions=True)
        cancelled = False
        for result in results:
            if isinstance(result, JobCancelled):
                cancelled = True
            elif isinstance(result, DownloadStats):
                stats.merge(result)
            else:
                stats.failed += 1
                logger.error("媒体下载任务异常：%s", result)
        if cancelled:
            raise JobCancelled()
        return stats

    async def _download_one(self, row: dict, force: bool = False) -> DownloadStats:
        stats = DownloadStats()
        media_id = int(row["id"])
        try:
            target = self._target_path(row)
        except Exception as exc:
            logger.error("媒体 %s 路径非法：%s", media_id, exc)
            self.db.update_media_status(media_id, "failed", error=f"invalid path: {exc}", increment_attempts=1)
            stats.failed += 1
            return stats

        if target.is_file() and target.stat().st_size > 0 and not force:
            size = target.stat().st_size
            self.db.update_media_status(media_id, "ok", local_size=size, http_status=200)
            stats.skipped += 1
            return stats

        url = str(row.get("download_url") or row.get("url") or "")
        if not url:
            self.db.update_media_status(media_id, "failed", error="empty url", increment_attempts=1)
            stats.failed += 1
            return stats
        if int(row.get("attempts") or 0) >= 8:
            stats.failed += 1
            return stats

        is_video = media_kind(row.get("media_type", ""), Path(row.get("file_name") or "").suffix) == "video"
        retries = self.config.sync.retries
        last_error = ""
        tries = 0
        part = target.with_name(target.name + ".part")
        try:
            for attempt in range(retries + 1):
                tries += 1
                try:
                    session = self.session
                    if session is None:
                        raise RuntimeError("下载会话未初始化")
                    async with session.stream("GET", url) as response:
                        status = response.status_code
                        if status == 200:
                            content_type = (
                                response.headers.get("Content-Type", "")
                                .split(";", 1)[0]
                                .strip()
                                .lower()
                            )
                            allowed = VIDEO_TYPES if is_video else IMAGE_TYPES
                            if content_type and not content_type.startswith(allowed):
                                raise InvalidMediaError(f"非媒体响应：{content_type}")
                            size = 0
                            with open(part, "wb") as handle:
                                async for chunk in response.aiter_bytes(CHUNK):
                                    handle.write(chunk)
                                    size += len(chunk)
                            if size <= 0:
                                raise InvalidMediaError("空文件")
                            with open(part, "rb") as handle:
                                head = handle.read(32)
                            detected = detect_media_kind(head)
                            expected = "video" if is_video else "image"
                            if detected != expected:
                                label = "视频" if is_video else "图片"
                                raise InvalidMediaError(
                                    f"内容不是{label}（Content-Type={content_type or 'unknown'}）"
                                )
                            ext = extension_for_content_type(content_type)
                            new_name = target.name
                            if ext and target.suffix.lower() != ext and ext in (".jpg", ".png", ".webp", ".gif", ".avif", ".heic", ".mp4", ".webm", ".mov"):
                                new_name = target.stem + ext
                            final_target = target.with_name(new_name)
                            os.replace(part, final_target)
                            if row.get("published_at"):
                                stamp = int(row["published_at"])
                                os.utime(final_target, (stamp, stamp))
                            self.db.update_media_status(
                                media_id,
                                "ok",
                                local_size=size,
                                http_status=200,
                                error=None,
                                downloaded_at=int(time.time()),
                                file_name=new_name,
                            )
                            stats.ok += 1
                            return stats

                        if status in (404, 410):
                            if not is_video and "name=orig" in url and attempt == 0:
                                url = url.replace("name=orig", "name=4096x4096")
                                continue
                            self.db.update_media_status(
                                media_id, "dead", http_status=status,
                                error=f"HTTP {status}", increment_attempts=1,
                            )
                            stats.dead += 1
                            return stats

                        if status in (401, 403):
                            self.db.update_media_status(
                                media_id, "restricted", http_status=status,
                                error=f"HTTP {status}", increment_attempts=1,
                            )
                            stats.restricted += 1
                            return stats

                        if status == 429 or 500 <= status < 600:
                            last_error = f"HTTP {status}"
                            if attempt < retries:
                                wait = min(60.0, 5.0 * (2 ** attempt))
                                logger.warning("媒体 %s 返回 %s，%.0fs 后重试", media_id, status, wait)
                                await asyncio.sleep(wait)
                                continue
                            break

                        last_error = f"HTTP {status}"
                        if attempt < retries:
                            wait = min(30.0, 2.0 * (2 ** attempt)) + random.uniform(0.0, 1.0)
                            await asyncio.sleep(wait)
                            continue
                        break
                except asyncio.CancelledError:
                    raise
                except InvalidMediaError as exc:
                    last_error = str(exc)
                    break
                except Exception as exc:
                    last_error = describe_error(exc)
                    if attempt < retries:
                        if isinstance(exc, httpx.TransportError):
                            await self._reset_session_if(session)
                        wait = min(30.0, 2.0 * (2 ** attempt)) + random.uniform(0.0, 1.0)
                        logger.warning("媒体 %s 下载失败（%s），%.1fs 后重试", media_id, describe_error(exc), wait)
                        await asyncio.sleep(wait)
                        continue
                    break
        finally:
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass

        self.db.update_media_status(
            media_id, "failed", error=last_error or "unknown error",
            increment_attempts=tries,
        )
        stats.failed += 1
        logger.warning("媒体 %s 最终失败：%s", media_id, last_error)
        return stats
