from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Database
from .models import MediaRecord, PostRecord, QuotedPost
from .utils import atomic_write_json, slugify_name

logger = logging.getLogger(__name__)

USER_FIELDS = (
    "rest_id", "screen_name", "name", "kind", "bio", "location", "url",
    "avatar_url", "banner_url", "avatar_file", "banner_file", "avatar_source",
    "avatar_updated_at", "image_dir", "sync_mode", "include_retweets",
    "last_tweet_id", "last_sync_at", "enabled", "protected", "verified",
    "followers_count", "following_count", "tweet_count", "media_count",
    "source_path",
)
POST_FIELDS = (
    "tweet_id", "author_id", "author_name", "author_screen_name", "kind", "text",
    "lang", "published_at", "favorite_count", "retweet_count", "reply_count",
    "quote_count", "view_count", "bookmark_count", "is_retweet", "retweet_of_id",
    "retweet_of_author", "is_quote", "quoted_id", "quoted_author", "quoted_text",
    "quoted_url", "is_reply", "reply_to_id", "reply_to_author", "possibly_sensitive",
    "source", "url", "is_accessible", "is_deleted", "media_count", "has_video",
)
MEDIA_FIELDS = (
    "media_key", "media_type", "url", "download_url", "poster_url", "file_name",
    "width", "height", "duration_ms", "alt_text", "position", "remote_size",
    "local_size", "status", "http_status",
)


def export_user(db: Database, config: Config, user_id: int) -> Path | None:
    data = db.export_user_data(user_id)
    if not data:
        return None
    user = data["user"]
    payload = {
        "schema": 1,
        "exported_at": int(time.time()),
        "user": {key: user.get(key) for key in USER_FIELDS},
        "posts": [],
    }
    for post in data["posts"]:
        item = {key: post.get(key) for key in POST_FIELDS}
        item["media"] = [
            {key: media.get(key) for key in MEDIA_FIELDS}
            for media in post.get("media", [])
        ]
        payload["posts"].append(item)
    file_name = f"{int(user_id)}_{slugify_name(str(user['name'] or user['screen_name'] or user_id))}.json"
    path = config.exports_dir / file_name
    atomic_write_json(path, payload)
    logger.debug("已导出 %s", path)
    return path


def export_all(db: Database, config: Config) -> list[Path]:
    paths = []
    for user in db.list_users_with_counts():
        path = export_user(db, config, int(user["id"]))
        if path:
            paths.append(path)
    return paths


@dataclass
class ImportStats:
    users: int = 0
    posts: int = 0
    media: int = 0
    skipped: int = 0


def import_exports(db: Database, config: Config, directory: Path | None = None, dry_run: bool = False) -> ImportStats:
    source = Path(directory) if directory else config.exports_dir
    stats = ImportStats()
    if not source.is_dir():
        raise SystemExit(f"导出目录不存在：{source}")
    for path in sorted(source.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("读取导出失败 %s：%s", path, exc)
            stats.skipped += 1
            continue
        user = data.get("user") or {}
        if not isinstance(user, dict):
            stats.skipped += 1
            continue
        if not dry_run:
            user_id = db.upsert_user(
                rest_id=str(user.get("rest_id") or ""),
                screen_name=str(user.get("screen_name") or ""),
                name=str(user.get("name") or ""),
                kind=str(user.get("kind") or "user"),
                bio=str(user.get("bio") or ""),
                location=str(user.get("location") or ""),
                url=str(user.get("url") or ""),
                avatar_url=str(user.get("avatar_url") or ""),
                banner_url=str(user.get("banner_url") or ""),
                image_dir=str(user.get("image_dir") or ""),
                sync_mode=str(user.get("sync_mode") or "media"),
                include_retweets=bool(user.get("include_retweets")),
                enabled=bool(user.get("enabled", 1)),
                protected=bool(user.get("protected")),
                verified=bool(user.get("verified")),
                followers_count=int(user.get("followers_count") or 0),
                following_count=int(user.get("following_count") or 0),
                tweet_count=int(user.get("tweet_count") or 0),
                media_count=int(user.get("media_count") or 0),
                source_path=str(user.get("source_path") or ""),
            )
            if user.get("avatar_file"):
                db.set_user_avatar(user_id, str(user["avatar_file"]), str(user.get("avatar_source") or ""), user.get("avatar_updated_at"))
            if user.get("banner_file"):
                db.set_user_banner(user_id, str(user["banner_file"]))
            if user.get("last_tweet_id"):
                db.set_last_tweet_id(user_id, str(user["last_tweet_id"]), synced_at=user.get("last_sync_at"))
        else:
            user_id = 0
        posts = data.get("posts")
        if not isinstance(posts, list):
            stats.skipped += 1
            continue
        for item in posts:
            if not isinstance(item, dict):
                continue
            media_records = []
            for media in item.get("media") or []:
                if not isinstance(media, dict):
                    continue
                media_records.append(MediaRecord(
                    media_key=str(media.get("media_key") or ""),
                    media_type=str(media.get("media_type") or "photo"),
                    url=str(media.get("url") or ""),
                    download_url=str(media.get("download_url") or ""),
                    poster_url=str(media.get("poster_url") or ""),
                    file_name=str(media.get("file_name") or ""),
                    width=int(media.get("width") or 0),
                    height=int(media.get("height") or 0),
                    duration_ms=int(media.get("duration_ms") or 0),
                    alt_text=str(media.get("alt_text") or ""),
                    position=int(media.get("position") or 0),
                    remote_size=media.get("remote_size"),
                    local_size=media.get("local_size"),
                    status=str(media.get("status") or "pending"),
                    http_status=media.get("http_status"),
                ))
            post = PostRecord(
                tweet_id=str(item.get("tweet_id") or ""),
                user_id=user_id,
                author_id=str(item.get("author_id") or ""),
                author_name=str(item.get("author_name") or ""),
                author_screen_name=str(item.get("author_screen_name") or ""),
                kind=str(item.get("kind") or "tweet"),
                text=str(item.get("text") or ""),
                lang=str(item.get("lang") or ""),
                published_at=int(item.get("published_at") or 0),
                favorite_count=int(item.get("favorite_count") or 0),
                retweet_count=int(item.get("retweet_count") or 0),
                reply_count=int(item.get("reply_count") or 0),
                quote_count=int(item.get("quote_count") or 0),
                view_count=int(item.get("view_count") or 0),
                bookmark_count=int(item.get("bookmark_count") or 0),
                is_retweet=bool(item.get("is_retweet")),
                retweet_of_id=str(item.get("retweet_of_id") or ""),
                retweet_of_author=str(item.get("retweet_of_author") or ""),
                is_quote=bool(item.get("is_quote")),
                quoted=QuotedPost(
                    tweet_id=str(item.get("quoted_id") or ""),
                    author_screen_name=str(item.get("quoted_author") or ""),
                    text=str(item.get("quoted_text") or ""),
                    url=str(item.get("quoted_url") or ""),
                ) if item.get("quoted_id") else None,
                is_reply=bool(item.get("is_reply")),
                reply_to_id=str(item.get("reply_to_id") or ""),
                reply_to_author=str(item.get("reply_to_author") or ""),
                possibly_sensitive=bool(item.get("possibly_sensitive")),
                source=str(item.get("source") or ""),
                url=str(item.get("url") or ""),
                is_accessible=bool(item.get("is_accessible", 1)),
                is_deleted=bool(item.get("is_deleted")),
                media=media_records,
            )
            if not post.tweet_id:
                continue
            if not dry_run:
                try:
                    with db.transaction():
                        post_id = db.upsert_post(post)
                        db.upsert_media(post_id, post.media)
                except Exception as exc:
                    logger.exception("导入推文 %s 失败：%s", post.tweet_id, exc)
                    stats.skipped += 1
                    continue
            stats.posts += 1
            stats.media += len(media_records)
        stats.users += 1
    logger.info(
        "导入%s：用户 %d，推文 %d，媒体 %d，跳过 %d",
        "预演" if dry_run else "完成", stats.users, stats.posts, stats.media, stats.skipped,
    )
    return stats
