from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from io import BytesIO
from pathlib import Path
from typing import Callable

import httpx
from PIL import Image, ImageOps

from .config import Config
from .db import Database
from .http import DEFAULT_UA
from .utils import strip_query

logger = logging.getLogger(__name__)

AVATAR_SIZE = 160
BANNER_WIDTH = 1500
HEADERS = {"User-Agent": DEFAULT_UA, "Referer": "https://x.com/"}


def avatar_path(config: Config, file_name: str) -> Path:
    return config.avatars_dir / Path(file_name).name


def banner_path(config: Config, file_name: str) -> Path:
    return config.banners_dir / Path(file_name).name


async def _fetch(url: str, config: Config) -> bytes:
    timeout = httpx.Timeout(60, connect=15)
    async with httpx.AsyncClient(
        proxy=config.sync.proxy or None,
        timeout=timeout,
        headers=HEADERS,
        follow_redirects=True,
    ) as client:
        response = await client.get(url)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        return response.content


async def download_avatar(
    db: Database,
    config: Config,
    user_id: int,
    url: str,
    force: bool = False,
) -> Path | None:
    url = strip_query(url or "")
    if not url:
        return None
    user = db.get_user(user_id)
    if not user:
        return None
    existing = str(user["avatar_file"] or "")
    if existing and not force and str(user["avatar_source"] or "") == url:
        path = avatar_path(config, existing)
        if path.is_file() and path.stat().st_size > 0:
            return path

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    file_name = f"{int(user_id)}_{digest}.webp"
    target = config.avatars_dir / file_name
    tmp = target.with_name(target.name + ".part")
    try:
        payload = await _fetch(url, config)
        if not payload:
            return None
        with Image.open(BytesIO(payload)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image = ImageOps.fit(image, (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
            image.save(tmp, "WEBP", quality=88, method=4)
        tmp.replace(target)
        db.set_user_avatar(user_id, file_name, url, int(time.time()))
        return target
    except Exception as exc:
        logger.warning("头像处理失败 user=%s：%s", user_id, exc)
        return None
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


async def download_banner(
    db: Database,
    config: Config,
    user_id: int,
    url: str,
    force: bool = False,
) -> Path | None:
    url = strip_query(url or "")
    if not url:
        return None
    user = db.get_user(user_id)
    if not user:
        return None
    existing = str(user["banner_file"] or "")
    if existing and not force:
        path = banner_path(config, existing)
        if path.is_file() and path.stat().st_size > 0:
            return path
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    file_name = f"{int(user_id)}_{digest}.webp"
    target = config.banners_dir / file_name
    tmp = target.with_name(target.name + ".part")
    try:
        payload = await _fetch(url, config)
        if not payload:
            return None
        with Image.open(BytesIO(payload)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            if image.width > BANNER_WIDTH:
                height = int(image.height * BANNER_WIDTH / image.width)
                image = image.resize((BANNER_WIDTH, height), Image.Resampling.LANCZOS)
            image.save(tmp, "WEBP", quality=84, method=4)
        tmp.replace(target)
        db.set_user_banner(user_id, file_name)
        return target
    except Exception as exc:
        logger.warning("横幅处理失败 user=%s：%s", user_id, exc)
        return None
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


async def refresh_avatars(
    db: Database,
    config: Config,
    client=None,
    user_ids: list[int] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict[str, int]:
    users = db.list_users_with_counts()
    if user_ids:
        wanted = {int(value) for value in user_ids}
        users = [user for user in users if int(user["id"]) in wanted]
    total = len(users)
    stats = {"ok": 0, "skipped": 0, "failed": 0, "total": total}
    for index, user in enumerate(users, 1):
        if cancel_check:
            cancel_check()
        user_id = int(user["id"])
        url = str(user["avatar_url"] or "")
        banner = str(user["banner_url"] or "")
        if (not url or not banner) and client is not None and user["kind"] == "user" and user["screen_name"]:
            try:
                info = await client.fetch_user(str(user["screen_name"]))
                url = url or str(info.get("avatar_url") or "")
                banner = banner or str(info.get("banner_url") or "")
                if info:
                    db.set_user_profile(
                        user_id,
                        name=str(info.get("name") or ""),
                        bio=str(info.get("bio") or ""),
                        location=str(info.get("location") or ""),
                        url=str(info.get("url") or ""),
                        avatar_url=url,
                        banner_url=banner,
                        protected=info.get("protected"),
                        verified=info.get("verified"),
                        followers_count=int(info.get("followers_count") or 0),
                        following_count=int(info.get("following_count") or 0),
                        tweet_count=int(info.get("tweet_count") or 0),
                        media_count=int(info.get("media_count") or 0),
                    )
            except Exception as exc:
                logger.warning("获取头像资料失败 user=%s：%s", user_id, exc)
        ok = False
        if url:
            ok = bool(await download_avatar(db, config, user_id, url))
        if banner:
            await download_banner(db, config, user_id, banner)
        if ok:
            stats["ok"] += 1
        elif url:
            stats["failed"] += 1
        else:
            stats["skipped"] += 1
        if on_progress:
            on_progress(index, total)
        if client is not None and config.sync.avatar_sleep > 0 and index < total:
            await asyncio.sleep(config.sync.avatar_sleep)
    logger.info(
        "头像补全完成：成功 %d，跳过 %d，失败 %d，共 %d",
        stats["ok"], stats["skipped"], stats["failed"], total,
    )
    return stats
