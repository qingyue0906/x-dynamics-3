from __future__ import annotations

import logging
from typing import Callable

from .avatars import download_avatar, download_banner
from .config import Config
from .db import Database
from .download import Downloader
from .export import export_user
from .extract import extract_tweet
from .sync import ensure_user
from .utils import parse_tweet_input
from .xclient import XClient

logger = logging.getLogger(__name__)


def _preview_media(media) -> list[dict]:
    return [
        {
            "type": item.media_type,
            "url": item.url,
            "poster": item.poster_url,
            "width": item.width,
            "height": item.height,
            "duration_ms": item.duration_ms,
            "alt": item.alt_text,
        }
        for item in media
    ]


async def preview_tweet(config: Config, text: str) -> dict:
    tweet_id = parse_tweet_input(text)
    if not tweet_id:
        raise ValueError("无法识别推文 ID。请输入 x.com/.../status/... 或纯数字 ID")
    async with XClient(config) as client:
        payload = await client.fetch_tweet(tweet_id)
    post = extract_tweet(payload, tweet_id, image_format=config.sync.image_format)
    if not post:
        raise ValueError("没有解析到推文，可能已删除、受保护或需要重新登录")
    return {
        "tweet_id": post.tweet_id,
        "author": post.author_name,
        "screen_name": post.author_screen_name,
        "text": post.text,
        "published_at": post.published_at,
        "kind": post.kind,
        "media_count": len(post.media),
        "has_video": post.has_video,
        "media": _preview_media(post.media),
        "quoted": {
            "author": post.quoted.author_screen_name,
            "text": post.quoted.text,
            "url": post.quoted.url,
        } if post.quoted else None,
        "favorite_count": post.favorite_count,
        "retweet_count": post.retweet_count,
        "reply_count": post.reply_count,
        "url": post.url or f"https://x.com/i/status/{post.tweet_id}",
    }


async def download_tweet(
    db: Database,
    config: Config,
    text: str,
    add_user: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict:
    tweet_id = parse_tweet_input(text)
    if not tweet_id:
        raise ValueError("无法识别推文 ID")
    async with XClient(config) as client:
        payload = await client.fetch_tweet(tweet_id)
        post = extract_tweet(payload, tweet_id, image_format=config.sync.image_format)
        if not post:
            raise ValueError("没有解析到推文，可能已删除、受保护或需要重新登录")
        info = {}
        if post.author_screen_name:
            try:
                info = await client.fetch_user(post.author_screen_name)
            except Exception as exc:
                logger.warning("获取推文作者资料失败：%s", exc)
    author = {
        "rest_id": post.author_id or str(info.get("rest_id") or ""),
        "screen_name": post.author_screen_name or str(info.get("screen_name") or ""),
        "name": post.author_name or str(info.get("name") or ""),
        "bio": str(info.get("bio") or ""),
        "location": str(info.get("location") or ""),
        "url": str(info.get("url") or ""),
        "avatar_url": str(info.get("avatar_url") or post.author_avatar or ""),
        "banner_url": str(info.get("banner_url") or ""),
        "followers_count": int(info.get("followers_count") or 0),
        "following_count": int(info.get("following_count") or 0),
        "tweet_count": int(info.get("tweet_count") or 0),
        "media_count": int(info.get("media_count") or 0),
        "protected": bool(info.get("protected")),
        "verified": bool(info.get("verified")),
    }
    if not author["screen_name"]:
        raise ValueError("推文缺少作者信息")
    existing = db.get_user_by_rest(author["rest_id"]) if author["rest_id"] else None
    if existing is None:
        existing = db.get_user_by_screen(author["screen_name"])
    user = ensure_user(db, config, author)
    if existing is None:
        db.set_user_enabled(int(user["id"]), bool(add_user))
    elif add_user and not user["enabled"]:
        db.set_user_enabled(int(user["id"]), True)
    if author["avatar_url"]:
        try:
            await download_avatar(db, config, int(user["id"]), author["avatar_url"])
        except Exception:
            logger.warning("下载作者头像失败", exc_info=True)
    if author["banner_url"]:
        try:
            await download_banner(db, config, int(user["id"]), author["banner_url"])
        except Exception:
            logger.warning("下载作者横幅失败", exc_info=True)
    user = db.get_user(int(user["id"]))
    post.user_id = int(user["id"])
    with db.transaction():
        post_id = db.upsert_post(post)
        db.upsert_media(post_id, post.media)
    if config.sync.export_json:
        export_user(db, config, int(user["id"]))
    rows = db.get_media_for_post(post_id)
    if not rows:
        return {
            "tweet_id": post.tweet_id,
            "user_id": int(user["id"]),
            "media_count": len(post.media),
            "downloaded": 0,
            "message": "媒体已全部存在",
        }
    async with Downloader(db, config) as downloader:
        stats = await downloader.download_rows(
            rows, on_progress=on_progress, cancel_check=cancel_check
        )
    return {
        "tweet_id": post.tweet_id,
        "user_id": int(user["id"]),
        "media_count": len(post.media),
        "downloaded": stats.ok,
        "skipped": stats.skipped,
        "failed": stats.failed,
        "dead": stats.dead,
        "restricted": stats.restricted,
        "message": f"已下载 {stats.ok} 个，跳过 {stats.skipped} 个，失败 {stats.failed} 个",
    }
