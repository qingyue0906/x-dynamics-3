from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .models import MediaRecord, PostRecord
from .utils import canonical_media_key, format_ts

logger = logging.getLogger(__name__)


class Database:
    """Small SQLite wrapper with WAL and page transactions."""

    def __init__(self, path: str | Path, init: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path), timeout=30, isolation_level=None, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        if init:
            self.init_schema()

    def init_schema(self) -> None:
        has_users = bool(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
        )
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if has_users and version < 1:
            self._backup_before_upgrade(version)
        schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        self.conn.executescript(schema)
        self.conn.execute("PRAGMA user_version = 1")

    def _backup_before_upgrade(self, version: int) -> None:
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"x-before-v1-{time.strftime('%Y%m%d-%H%M%S')}.db"
        try:
            destination = sqlite3.connect(str(target))
            try:
                self.conn.backup(destination)
            finally:
                destination.close()
            logger.warning("数据库升级前已备份：%s（旧版本 %s）", target, version)
        except Exception as exc:
            logger.error("数据库备份失败：%s", exc)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---------------- users ----------------

    def get_user(self, user_id: int):
        return self.conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()

    def get_user_by_screen(self, screen_name: str):
        if not screen_name:
            return None
        return self.conn.execute(
            "SELECT * FROM users WHERE screen_name = ? COLLATE NOCASE", (screen_name,)
        ).fetchone()

    def get_user_by_rest(self, rest_id: str):
        if not rest_id:
            return None
        return self.conn.execute(
            "SELECT * FROM users WHERE rest_id = ?", (str(rest_id),)
        ).fetchone()

    def get_user_by_source(self, source_path: str):
        if not source_path:
            return None
        return self.conn.execute(
            "SELECT * FROM users WHERE source_path = ?", (source_path,)
        ).fetchone()

    def upsert_user(
        self,
        rest_id: str = "",
        screen_name: str = "",
        name: str = "",
        kind: str = "user",
        bio: str = "",
        location: str = "",
        url: str = "",
        avatar_url: str = "",
        banner_url: str = "",
        image_dir: str = "",
        sync_mode: str = "media",
        include_retweets: bool = False,
        enabled: bool | None = None,
        protected: bool = False,
        verified: bool = False,
        followers_count: int = 0,
        following_count: int = 0,
        tweet_count: int = 0,
        media_count: int = 0,
        source_path: str = "",
        sort_order: int = 0,
    ) -> int:
        now = int(time.time())
        rest_id = str(rest_id or "").strip()
        screen_name = str(screen_name or "").strip()
        row = None
        if rest_id:
            row = self.get_user_by_rest(rest_id)
        if row is None and screen_name:
            row = self.get_user_by_screen(screen_name)
        if row is None and source_path:
            row = self.get_user_by_source(source_path)
        if row is None:
            cursor = self.conn.execute(
                """
                INSERT INTO users(
                    rest_id, screen_name, name, kind, bio, location, url,
                    avatar_url, banner_url, image_dir, sync_mode, include_retweets,
                    enabled, protected, verified, followers_count, following_count,
                    tweet_count, media_count, source_path, sort_order,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rest_id, screen_name, name or screen_name or rest_id or "archive", kind,
                    bio, location, url, avatar_url, banner_url, image_dir, sync_mode,
                    1 if include_retweets else 0, 1 if (enabled is None or enabled) else 0,
                    1 if protected else 0, 1 if verified else 0, int(followers_count),
                    int(following_count), int(tweet_count), int(media_count), source_path,
                    int(sort_order), now, now,
                ),
            )
            return int(cursor.lastrowid)

        user_id = int(row["id"])
        enabled_value = row["enabled"] if enabled is None else (1 if enabled else 0)
        self.conn.execute(
            """
            UPDATE users SET
                rest_id = CASE WHEN ? <> '' THEN ? ELSE rest_id END,
                screen_name = CASE WHEN ? <> '' THEN ? ELSE screen_name END,
                name = CASE WHEN ? <> '' THEN ? ELSE name END,
                kind = CASE WHEN ? <> '' THEN ? ELSE kind END,
                bio = CASE WHEN ? <> '' THEN ? ELSE bio END,
                location = CASE WHEN ? <> '' THEN ? ELSE location END,
                url = CASE WHEN ? <> '' THEN ? ELSE url END,
                avatar_url = CASE WHEN ? <> '' THEN ? ELSE avatar_url END,
                banner_url = CASE WHEN ? <> '' THEN ? ELSE banner_url END,
                image_dir = CASE WHEN ? <> '' THEN ? ELSE image_dir END,
                sync_mode = CASE WHEN ? <> '' THEN ? ELSE sync_mode END,
                include_retweets = ?,
                enabled = ?,
                protected = ?,
                verified = ?,
                followers_count = CASE WHEN ? > 0 THEN ? ELSE followers_count END,
                following_count = CASE WHEN ? > 0 THEN ? ELSE following_count END,
                tweet_count = CASE WHEN ? > 0 THEN ? ELSE tweet_count END,
                media_count = CASE WHEN ? > 0 THEN ? ELSE media_count END,
                source_path = CASE WHEN ? <> '' THEN ? ELSE source_path END,
                sort_order = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                rest_id, rest_id, screen_name, screen_name, name, name, kind, kind,
                bio, bio, location, location, url, url, avatar_url, avatar_url,
                banner_url, banner_url, image_dir, image_dir, sync_mode, sync_mode,
                1 if include_retweets else int(row["include_retweets"]),
                enabled_value, 1 if protected else int(row["protected"]),
                1 if verified else int(row["verified"]),
                int(followers_count), int(followers_count),
                int(following_count), int(following_count),
                int(tweet_count), int(tweet_count), int(media_count), int(media_count),
                source_path, source_path, int(sort_order), now, user_id,
            ),
        )
        return user_id

    def set_user_enabled(self, user_id: int, enabled: bool) -> None:
        self.conn.execute(
            "UPDATE users SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, int(time.time()), int(user_id)),
        )

    def set_user_mode(self, user_id: int, mode: str, include_retweets: bool | None = None) -> None:
        if mode not in ("media", "timeline", "replies", "none"):
            mode = "media"
        if include_retweets is None:
            self.conn.execute(
                "UPDATE users SET sync_mode = ?, updated_at = ? WHERE id = ?",
                (mode, int(time.time()), int(user_id)),
            )
        else:
            self.conn.execute(
                "UPDATE users SET sync_mode = ?, include_retweets = ?, updated_at = ? WHERE id = ?",
                (mode, 1 if include_retweets else 0, int(time.time()), int(user_id)),
            )

    def set_user_image_dir(self, user_id: int, image_dir: str) -> None:
        self.conn.execute(
            "UPDATE users SET image_dir = ?, updated_at = ? WHERE id = ?",
            (image_dir, int(time.time()), int(user_id)),
        )

    def set_user_avatar(self, user_id: int, avatar_file: str, source: str, updated_at: int | None = None) -> None:
        self.conn.execute(
            """
            UPDATE users SET avatar_file = ?, avatar_source = ?, avatar_updated_at = ?,
                updated_at = ? WHERE id = ?
            """,
            (avatar_file or "", source or "", int(updated_at or time.time()), int(time.time()), int(user_id)),
        )

    def set_user_banner(self, user_id: int, banner_file: str) -> None:
        self.conn.execute(
            "UPDATE users SET banner_file = ?, updated_at = ? WHERE id = ?",
            (banner_file or "", int(time.time()), int(user_id)),
        )

    def set_user_profile(
        self,
        user_id: int,
        name: str = "",
        bio: str = "",
        location: str = "",
        url: str = "",
        avatar_url: str = "",
        banner_url: str = "",
        protected: bool | None = None,
        verified: bool | None = None,
        followers_count: int = 0,
        following_count: int = 0,
        tweet_count: int = 0,
        media_count: int = 0,
    ) -> None:
        row = self.get_user(user_id)
        if not row:
            return
        self.conn.execute(
            """
            UPDATE users SET
                name = ?, bio = ?, location = ?, url = ?, avatar_url = ?, banner_url = ?,
                protected = ?, verified = ?, followers_count = ?, following_count = ?,
                tweet_count = ?, media_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                name or row["name"], bio or row["bio"], location or row["location"],
                url or row["url"], avatar_url or row["avatar_url"],
                banner_url or row["banner_url"],
                int(row["protected"] if protected is None else (1 if protected else 0)),
                int(row["verified"] if verified is None else (1 if verified else 0)),
                int(followers_count or row["followers_count"]),
                int(following_count or row["following_count"]),
                int(tweet_count or row["tweet_count"]),
                int(media_count or row["media_count"]),
                int(time.time()), int(user_id),
            ),
        )

    def set_last_tweet_id(self, user_id: int, tweet_id: str | int, synced_at: int | None = None) -> None:
        value = str(tweet_id or "0")
        ts = int(synced_at) if synced_at is not None else None
        self.conn.execute(
            """
            UPDATE users SET
                last_tweet_id = CASE
                    WHEN CAST(? AS INTEGER) > CAST(last_tweet_id AS INTEGER) THEN ?
                    ELSE last_tweet_id
                END,
                last_sync_at = CASE WHEN ? IS NOT NULL THEN ? ELSE last_sync_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (value, value, ts, ts, int(time.time()), int(user_id)),
        )

    def list_users_with_counts(self):
        return self.conn.execute(
            """
            SELECT u.*,
                   COUNT(p.id) AS post_count,
                   COALESCE(SUM(CASE WHEN p.media_count > 0 THEN 1 ELSE 0 END), 0) AS media_post_count,
                   COALESCE(SUM(p.media_count), 0) AS media_ref_count,
                   (SELECT COUNT(*) FROM media m JOIN posts p2 ON p2.id = m.post_id
                    WHERE p2.user_id = u.id AND m.status = 'ok') AS ok_media_count
            FROM users u
            LEFT JOIN posts p ON p.user_id = u.id
            GROUP BY u.id
            ORDER BY CASE u.kind WHEN 'user' THEN 0 ELSE 1 END, u.sort_order, u.name COLLATE NOCASE, u.id
            """
        ).fetchall()

    def get_user_with_counts(self, user_id: int):
        return self.conn.execute(
            """
            SELECT u.*,
                   COUNT(p.id) AS post_count,
                   COALESCE(SUM(CASE WHEN p.media_count > 0 THEN 1 ELSE 0 END), 0) AS media_post_count,
                   COALESCE(SUM(p.media_count), 0) AS media_ref_count,
                   (SELECT COUNT(*) FROM media m JOIN posts p2 ON p2.id = m.post_id
                    WHERE p2.user_id = u.id AND m.status = 'ok') AS ok_media_count
            FROM users u
            LEFT JOIN posts p ON p.user_id = u.id
            WHERE u.id = ?
            GROUP BY u.id
            """,
            (int(user_id),),
        ).fetchone()

    def list_sync_users(self):
        return self.conn.execute(
            """
            SELECT * FROM users
            WHERE enabled = 1 AND kind = 'user' AND sync_mode <> 'none'
            ORDER BY sort_order, name COLLATE NOCASE, id
            """
        ).fetchall()

    # ---------------- posts ----------------

    def get_post_id(self, user_id: int, tweet_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM posts WHERE user_id = ? AND tweet_id = ?",
            (int(user_id), str(tweet_id)),
        ).fetchone()
        return int(row["id"]) if row else None

    def upsert_post(self, post: PostRecord) -> int:
        now = int(time.time())
        quoted = post.quoted
        search_text = "\n".join(
            part for part in (
                post.text,
                post.author_name,
                post.author_screen_name,
                quoted.text if quoted else "",
                quoted.author_name if quoted else "",
            ) if part
        ).lower()
        row = self.conn.execute(
            "SELECT id, media_count, has_video FROM posts WHERE user_id = ? AND tweet_id = ?",
            (int(post.user_id), post.tweet_id),
        ).fetchone()
        values = (
            post.author_id, post.author_name, post.author_screen_name, post.kind, post.text,
            post.lang, int(post.published_at or 0), now, int(post.favorite_count or 0),
            int(post.retweet_count or 0), int(post.reply_count or 0), int(post.quote_count or 0),
            int(post.view_count or 0), int(post.bookmark_count or 0),
            1 if post.is_retweet else 0, post.retweet_of_id, post.retweet_of_author,
            1 if post.is_quote else 0, quoted.tweet_id if quoted else "",
            quoted.author_screen_name if quoted else "", quoted.text if quoted else "",
            quoted.url if quoted else "", 1 if post.is_reply else 0, post.reply_to_id,
            post.reply_to_author, 1 if post.possibly_sensitive else 0, post.source, post.url,
            1 if post.is_accessible else 0, 1 if post.is_deleted else 0,
            max(int(post.media_count), int(row["media_count"]) if row else 0),
            1 if post.has_video or (row and row["has_video"]) else 0,
            search_text,
        )
        if row is None:
            cursor = self.conn.execute(
                """
                INSERT INTO posts(
                    user_id, tweet_id, author_id, author_name, author_screen_name, kind,
                    text, lang, published_at, collected_at, favorite_count, retweet_count,
                    reply_count, quote_count, view_count, bookmark_count, is_retweet,
                    retweet_of_id, retweet_of_author, is_quote, quoted_id, quoted_author,
                    quoted_text, quoted_url, is_reply, reply_to_id, reply_to_author,
                    possibly_sensitive, source, url, is_accessible, is_deleted,
                    media_count, has_video, search_text
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(post.user_id), post.tweet_id, *values),
            )
            return int(cursor.lastrowid)
        self.conn.execute(
            """
            UPDATE posts SET
                author_id = ?, author_name = ?, author_screen_name = ?, kind = ?, text = ?,
                lang = ?, published_at = ?, collected_at = ?, favorite_count = ?,
                retweet_count = ?, reply_count = ?, quote_count = ?, view_count = ?,
                bookmark_count = ?, is_retweet = ?, retweet_of_id = ?, retweet_of_author = ?,
                is_quote = ?, quoted_id = ?, quoted_author = ?, quoted_text = ?,
                quoted_url = ?, is_reply = ?, reply_to_id = ?, reply_to_author = ?,
                possibly_sensitive = ?, source = ?, url = ?, is_accessible = ?, is_deleted = ?,
                media_count = ?, has_video = ?, search_text = ?
            WHERE id = ?
            """,
            (*values, int(row["id"])),
        )
        return int(row["id"])

    def upsert_media(self, post_id: int, media: list[MediaRecord]) -> list[int]:
        ids: list[int] = []
        existing = [
            dict(row) for row in self.conn.execute(
                "SELECT * FROM media WHERE post_id = ? ORDER BY id", (int(post_id),)
            ).fetchall()
        ]
        for index, item in enumerate(media):
            canonical = canonical_media_key(
                item.url, item.download_url, item.media_type, item.file_name
            )
            row = next(
                (
                    candidate for candidate in existing
                    if candidate["media_key"] == item.media_key
                    or canonical_media_key(
                        candidate.get("url", ""),
                        candidate.get("download_url", ""),
                        candidate.get("media_type", ""),
                        candidate.get("file_name", ""),
                    ) == canonical
                ),
                None,
            )
            position = int(item.position if item.position else index)
            if row is None:
                media_key = canonical or item.media_key
                cursor = self.conn.execute(
                    """
                    INSERT INTO media(
                        post_id, media_key, media_type, url, download_url, poster_url,
                        file_name, width, height, duration_ms, alt_text, position,
                        remote_size, local_size, status, attempts, http_status, last_error,
                        downloaded_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(post_id), media_key, item.media_type, item.url, item.download_url,
                        item.poster_url, item.file_name, int(item.width or 0),
                        int(item.height or 0), int(item.duration_ms or 0), item.alt_text,
                        position, item.remote_size, item.local_size, item.status,
                        int(item.attempts or 0), item.http_status, item.last_error,
                        item.downloaded_at,
                    ),
                )
                media_id = int(cursor.lastrowid)
                ids.append(media_id)
                existing.append({
                    "id": media_id,
                    "post_id": int(post_id),
                    "media_key": media_key,
                    "media_type": item.media_type,
                    "url": item.url,
                    "download_url": item.download_url,
                    "file_name": item.file_name,
                    "status": item.status,
                })
                continue
            status = row["status"]
            if status in ("dead", "restricted"):
                pass
            elif status == "ok":
                status = "ok"
            else:
                status = item.status or status
            file_name = row["file_name"] or item.file_name
            self.conn.execute(
                """
                UPDATE media SET
                    media_type = ?, url = ?, download_url = ?, poster_url = ?, file_name = ?,
                    width = ?, height = ?, duration_ms = ?, alt_text = ?, position = ?,
                    remote_size = COALESCE(?, remote_size), local_size = COALESCE(?, local_size),
                    status = ?, http_status = COALESCE(?, http_status)
                WHERE id = ?
                """,
                (
                    item.media_type, item.url, item.download_url, item.poster_url, file_name,
                    int(item.width or 0), int(item.height or 0), int(item.duration_ms or 0),
                    item.alt_text, position, item.remote_size, item.local_size, status,
                    item.http_status, int(row["id"]),
                ),
            )
            new_key = canonical or item.media_key
            if new_key and new_key != row["media_key"] and not any(
                other["id"] != row["id"] and other["media_key"] == new_key
                for other in existing
            ):
                self.conn.execute(
                    "UPDATE media SET media_key = ? WHERE id = ?",
                    (new_key, int(row["id"])),
                )
                row["media_key"] = new_key
            row["status"] = status
            row["file_name"] = file_name
            ids.append(int(row["id"]))
        self._refresh_post_media_counts(post_id)
        return ids

    def _refresh_post_media_counts(self, post_id: int) -> None:
        self.conn.execute(
            """
            UPDATE posts SET
                media_count = (SELECT COUNT(*) FROM media WHERE post_id = ?),
                has_video = (SELECT COUNT(*) FROM media WHERE post_id = ? AND media_type IN ('video', 'animated_gif'))
            WHERE id = ?
            """,
            (int(post_id), int(post_id), int(post_id)),
        )

    @staticmethod
    def _post_where(user_id=None, q=None, include_empty=False, kind=None):
        where = ["1=1"] if include_empty else ["p.media_count > 0"]
        params: list = []
        if user_id:
            where.append("p.user_id = ?")
            params.append(int(user_id))
        if kind:
            where.append("p.kind = ?")
            params.append(kind)
        if q:
            where.append("p.search_text LIKE ?")
            params.append(f"%{q.strip().lower()}%")
        return " AND ".join(where), params

    @staticmethod
    def _media_status_label(status: str) -> str:
        return {
            "missing": "本地缺失",
            "dead": "X 已删除",
            "failed": "下载失败",
            "restricted": "无权限",
            "pending": "待下载",
            "ok": "已下载",
        }.get(status, "媒体失效")

    def list_posts_with_media(
        self,
        limit: int = 24,
        offset: int = 0,
        user_id: int | None = None,
        q: str | None = None,
        sort: str = "desc",
        include_empty: bool = False,
    ) -> tuple[list[dict], bool]:
        where, params = self._post_where(user_id=user_id, q=q, include_empty=include_empty)
        direction = "ASC" if str(sort).lower() == "asc" else "DESC"
        rows = self.conn.execute(
            f"""
            SELECT p.*, u.name AS owner_name, u.screen_name AS owner_screen_name,
                   u.avatar_updated_at AS owner_avatar_updated_at, u.image_dir, u.kind AS owner_kind
            FROM posts p JOIN users u ON u.id = p.user_id
            WHERE {where}
            ORDER BY p.published_at {direction}, p.id {direction}
            LIMIT ? OFFSET ?
            """,
            (*params, int(limit) + 1, int(offset)),
        ).fetchall()
        has_more = len(rows) > int(limit)
        rows = rows[: int(limit)]
        if not rows:
            return [], False
        post_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in post_ids)
        media_rows = self.conn.execute(
            f"SELECT * FROM media WHERE post_id IN ({placeholders}) ORDER BY post_id, position, id",
            post_ids,
        ).fetchall()
        media_by_post: dict[int, list[dict]] = {}
        for item in media_rows:
            media = dict(item)
            media["ok"] = media.get("status") == "ok"
            media["status_label"] = self._media_status_label(media.get("status", ""))
            media["is_video"] = media.get("media_type") in ("video", "animated_gif")
            media["duration"] = format_ts(int(media.get("duration_ms") or 0) // 1000, "%M:%S") if media.get("duration_ms") else ""
            media_by_post.setdefault(int(media["post_id"]), []).append(media)
        result = []
        for row in rows:
            item = dict(row)
            item["media"] = media_by_post.get(int(row["id"]), [])
            item["time_str"] = format_ts(item.get("published_at"))
            item["x_url"] = item.get("url") or f"https://x.com/i/status/{item['tweet_id']}"
            item["display_name"] = item.get("author_name") or item.get("owner_name") or item.get("author_screen_name")
            item["display_screen"] = item.get("author_screen_name") or item.get("owner_screen_name")
            name = item.get("display_name") or ""
            item["initial"] = name[:1] if name else "?"
            item["ok_media"] = sum(1 for media in item["media"] if media.get("ok"))
            result.append(item)
        return result, has_more

    def count_posts(
        self,
        user_id: int | None = None,
        q: str | None = None,
        include_empty: bool = False,
    ) -> int:
        where, params = self._post_where(user_id=user_id, q=q, include_empty=include_empty)
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM posts p WHERE {where}", params
        ).fetchone()
        return int(row["n"])

    @staticmethod
    def _media_where(user_id=None, q=None, include_missing=False):
        where = ["1=1"] if include_missing else ["m.status = 'ok'"]
        params: list = []
        if user_id:
            where.append("p.user_id = ?")
            params.append(int(user_id))
        if q:
            where.append("p.search_text LIKE ?")
            params.append(f"%{q.strip().lower()}%")
        return " AND ".join(where), params

    def list_media_with_posts(
        self,
        limit: int = 48,
        offset: int = 0,
        user_id: int | None = None,
        q: str | None = None,
        sort: str = "desc",
        include_missing: bool = False,
    ) -> tuple[list[dict], bool]:
        where, params = self._media_where(user_id=user_id, q=q, include_missing=include_missing)
        direction = "ASC" if str(sort).lower() == "asc" else "DESC"
        rows = self.conn.execute(
            f"""
            SELECT m.*, p.tweet_id, p.text AS post_text, p.author_name, p.author_screen_name,
                   p.published_at, p.user_id, u.name AS owner_name, u.screen_name AS owner_screen_name,
                   u.avatar_updated_at AS owner_avatar_updated_at
            FROM media m
            JOIN posts p ON p.id = m.post_id
            JOIN users u ON u.id = p.user_id
            WHERE {where}
            ORDER BY p.published_at {direction}, p.id {direction}, m.position ASC, m.id ASC
            LIMIT ? OFFSET ?
            """,
            (*params, int(limit) + 1, int(offset)),
        ).fetchall()
        has_more = len(rows) > int(limit)
        rows = rows[: int(limit)]
        items = []
        for row in rows:
            item = dict(row)
            item["time_str"] = format_ts(item.get("published_at"))
            item["x_url"] = f"https://x.com/i/status/{item['tweet_id']}"
            item["ok"] = item.get("status") == "ok"
            item["status_label"] = self._media_status_label(item.get("status", ""))
            item["is_video"] = item.get("media_type") in ("video", "animated_gif")
            item["display_name"] = item.get("author_name") or item.get("owner_name")
            items.append(item)
        return items, has_more

    def count_media_with_posts(
        self,
        user_id: int | None = None,
        q: str | None = None,
        include_missing: bool = False,
    ) -> int:
        where, params = self._media_where(user_id=user_id, q=q, include_missing=include_missing)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM media m
            JOIN posts p ON p.id = m.post_id
            JOIN users u ON u.id = p.user_id
            WHERE {where}
            """,
            params,
        ).fetchone()
        return int(row["n"])

    def update_media_status(
        self,
        media_id: int,
        status: str,
        local_size: int | None = None,
        http_status: int | None = None,
        error: str | None = None,
        increment_attempts: int = 0,
        downloaded_at: int | None = None,
        file_name: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE media SET
                status = ?, local_size = COALESCE(?, local_size),
                http_status = COALESCE(?, http_status), last_error = ?,
                attempts = attempts + ?, downloaded_at = COALESCE(?, downloaded_at),
                file_name = COALESCE(?, file_name)
            WHERE id = ?
            """,
            (
                status, local_size, http_status, error, int(increment_attempts),
                downloaded_at, file_name, int(media_id),
            ),
        )

    def get_media_for_posts(self, post_ids: list[int]) -> list[dict]:
        if not post_ids:
            return []
        placeholders = ",".join("?" for _ in post_ids)
        return [dict(row) for row in self.conn.execute(
            f"""
            SELECT m.*, u.image_dir, u.id AS user_id, p.published_at, p.tweet_id
            FROM media m JOIN posts p ON p.id = m.post_id JOIN users u ON u.id = p.user_id
            WHERE m.post_id IN ({placeholders}) AND m.status = 'pending'
            ORDER BY m.id
            """,
            post_ids,
        ).fetchall()]

    def get_media_for_post(self, post_id: int) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            """
            SELECT m.*, u.image_dir, u.id AS user_id, p.published_at, p.tweet_id
            FROM media m JOIN posts p ON p.id = m.post_id JOIN users u ON u.id = p.user_id
            WHERE m.post_id = ? AND m.status IN ('pending', 'failed', 'missing')
            ORDER BY m.id
            """,
            (int(post_id),),
        ).fetchall()]

    def get_repair_media(
        self,
        user_id: int | None = None,
        limit: int = 0,
        statuses: tuple[str, ...] = ("pending", "failed", "missing"),
        max_attempts: int = 8,
        user_ids: list[int] | None = None,
    ) -> list[dict]:
        placeholders = ",".join("?" for _ in statuses)
        sql = f"""
            SELECT m.*, u.image_dir, u.id AS user_id, p.published_at, p.tweet_id
            FROM media m
            JOIN posts p ON p.id = m.post_id
            JOIN users u ON u.id = p.user_id
            WHERE m.status IN ({placeholders}) AND m.attempts < ?
        """
        params: list = [*statuses, int(max_attempts)]
        if user_ids:
            marks = ",".join("?" for _ in user_ids)
            sql += f" AND p.user_id IN ({marks})"
            params.extend(int(value) for value in user_ids)
        elif user_id:
            sql += " AND p.user_id = ?"
            params.append(int(user_id))
        sql += " ORDER BY m.id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def get_retry_counts(self, user_id: int | None = None) -> dict[str, int]:
        sql = """
            SELECT m.status, COUNT(*) AS n FROM media m
            JOIN posts p ON p.id = m.post_id WHERE 1=1
        """
        params: list = []
        if user_id:
            sql += " AND p.user_id = ?"
            params.append(int(user_id))
        sql += " GROUP BY m.status"
        return {row["status"]: int(row["n"]) for row in self.conn.execute(sql, params)}

    def get_media_with_user(self, user_id: int | None = None) -> list[dict]:
        sql = """
            SELECT m.id, m.post_id, m.file_name, m.status, m.local_size, m.url,
                   m.attempts, u.id AS user_id, u.image_dir, u.name, p.published_at, p.tweet_id
            FROM media m
            JOIN posts p ON p.id = m.post_id
            JOIN users u ON u.id = p.user_id
        """
        params: list = []
        if user_id:
            sql += " WHERE u.id = ?"
            params.append(int(user_id))
        sql += " ORDER BY m.id"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def get_media_with_user_by_id(self, media_id: int):
        return self.conn.execute(
            """
            SELECT m.*, u.id AS user_id, u.image_dir, u.name, p.published_at, p.tweet_id
            FROM media m
            JOIN posts p ON p.id = m.post_id
            JOIN users u ON u.id = p.user_id
            WHERE m.id = ?
            """,
            (int(media_id),),
        ).fetchone()

    def get_media_rows_for_dedupe(self, user_id: int | None = None) -> list[dict]:
        sql = """
            SELECT m.*, p.tweet_id, p.user_id, u.image_dir, u.screen_name
            FROM media m
            JOIN posts p ON p.id = m.post_id
            JOIN users u ON u.id = p.user_id
        """
        params: list = []
        if user_id:
            sql += " WHERE p.user_id = ?"
            params.append(int(user_id))
        sql += " ORDER BY m.post_id, m.id"
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def set_media_key(self, media_id: int, media_key: str) -> None:
        self.conn.execute(
            "UPDATE media SET media_key = ? WHERE id = ?",
            (media_key, int(media_id)),
        )

    def delete_media(self, media_id: int) -> None:
        self.conn.execute("DELETE FROM media WHERE id = ?", (int(media_id),))

    def refresh_post_media_counts(self, post_id: int) -> None:
        self._refresh_post_media_counts(post_id)

    # ---------------- jobs ----------------

    def create_job(self, kind: str, target: str = "") -> int:
        row = self.conn.execute(
            "INSERT INTO jobs(kind, target, status, created_at) VALUES(?, ?, 'queued', ?) RETURNING id",
            (kind, target or "", int(time.time())),
        ).fetchone()
        return int(row["id"])

    def update_job(self, job_id: int, **fields) -> None:
        allowed = {
            "status", "started_at", "finished_at", "total", "done",
            "message", "log_path", "error",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self.conn.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?",
            (*updates.values(), int(job_id)),
        )

    def get_job(self, job_id: int):
        return self.conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()

    def list_jobs(self, limit: int = 50) -> list:
        return self.conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()

    def active_job(self):
        return self.conn.execute(
            """
            SELECT * FROM jobs WHERE status IN ('queued', 'running', 'cancelling')
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()

    def mark_interrupted_jobs(self) -> int:
        rows = self.conn.execute(
            """
            UPDATE jobs SET status = 'interrupted', finished_at = ?,
                message = '服务重启，任务被中断'
            WHERE status IN ('queued', 'running', 'cancelling') RETURNING id
            """,
            (int(time.time()),),
        ).fetchall()
        return len(rows)

    # ---------------- stats / runs ----------------

    def stats(self) -> dict:
        row = self.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users WHERE kind = 'user') AS users,
                (SELECT COUNT(*) FROM users WHERE kind = 'user' AND enabled = 1) AS enabled_users,
                (SELECT COUNT(*) FROM users WHERE kind <> 'user') AS collections,
                (SELECT COUNT(*) FROM users WHERE avatar_file <> '') AS avatars,
                (SELECT COUNT(*) FROM posts) AS posts,
                (SELECT COUNT(*) FROM posts WHERE media_count > 0) AS media_posts,
                (SELECT COUNT(*) FROM posts WHERE media_count = 0) AS empty_posts,
                (SELECT COUNT(*) FROM media) AS media,
                (SELECT COUNT(*) FROM media WHERE status = 'ok') AS ok_media,
                (SELECT COUNT(*) FROM media WHERE status IN ('missing', 'failed')) AS missing_media,
                (SELECT COUNT(*) FROM media WHERE status = 'dead') AS dead_media,
                (SELECT COUNT(*) FROM media WHERE status = 'restricted') AS restricted_media,
                (SELECT COUNT(*) FROM media WHERE status = 'pending') AS pending_media,
                (SELECT COUNT(*) FROM media WHERE status IN ('missing', 'failed', 'dead', 'restricted', 'pending')) AS placeholder_media,
                (SELECT COALESCE(SUM(local_size), 0) FROM media) AS bytes
            """
        ).fetchone()
        return dict(row)

    def start_sync_run(self, user_id: int) -> int:
        row = self.conn.execute(
            "INSERT INTO sync_runs(user_id, started_at) VALUES(?, ?) RETURNING id",
            (int(user_id), int(time.time())),
        ).fetchone()
        return int(row["id"])

    def finish_sync_run(
        self,
        run_id: int,
        status: str,
        pages: int = 0,
        new_posts: int = 0,
        new_media: int = 0,
        message: str = "",
    ) -> None:
        self.conn.execute(
            """
            UPDATE sync_runs SET finished_at = ?, status = ?, pages = ?,
                new_posts = ?, new_media = ?, message = ? WHERE id = ?
            """,
            (int(time.time()), status, int(pages), int(new_posts), int(new_media), message, int(run_id)),
        )

    def export_user_data(self, user_id: int) -> dict | None:
        user = self.get_user(user_id)
        if not user:
            return None
        posts = self.conn.execute(
            "SELECT * FROM posts WHERE user_id = ? ORDER BY published_at DESC, id DESC",
            (int(user_id),),
        ).fetchall()
        media = self.conn.execute(
            """
            SELECT m.*, p.tweet_id FROM media m JOIN posts p ON p.id = m.post_id
            WHERE p.user_id = ? ORDER BY m.id
            """,
            (int(user_id),),
        ).fetchall()
        media_by_post: dict[str, list[dict]] = {}
        for item in media:
            media_by_post.setdefault(str(item["tweet_id"]), []).append(dict(item))
        post_list = []
        for post in posts:
            item = dict(post)
            item.pop("id", None)
            item["media"] = media_by_post.get(str(post["tweet_id"]), [])
            post_list.append(item)
        return {"user": dict(user), "posts": post_list}
