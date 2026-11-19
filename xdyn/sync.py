from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .avatars import download_avatar, download_banner
from .config import Config
from .db import Database
from .download import DownloadStats, Downloader
from .errors import JobCancelled, describe_error
from .export import export_user
from .extract import extract_timeline_page
from .utils import parse_user_input, slugify_name

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], None]
CancelCheck = Callable[[], None]


def _emit(callback: ProgressCallback | None, payload: dict) -> None:
    if not callback:
        return
    try:
        callback(payload)
    except Exception:
        logger.exception("进度回调失败")


@dataclass
class SyncResult:
    user_id: int
    name: str = ""
    status: str = "ok"
    pages: int = 0
    new_posts: int = 0
    new_media: int = 0
    skipped_posts: int = 0
    download: DownloadStats = field(default_factory=DownloadStats)
    message: str = ""


def _user_dir_name(kind: str, rest_id: str, screen_name: str, name: str) -> str:
    if kind != "user":
        base = f"{kind}_{slugify_name(name or screen_name or 'collection')}"
        return f"{base}_{int(time.time() * 1000) % 100000:05d}"
    if rest_id:
        return f"{rest_id}_{slugify_name(screen_name or name or rest_id)}"
    return f"sn_{slugify_name(screen_name or name or 'user')}"


def _image_dir_for(config: Config, kind: str, rest_id: str, screen_name: str, name: str) -> str:
    folder = _user_dir_name(kind, rest_id, screen_name, name)
    (config.images_dir / folder).mkdir(parents=True, exist_ok=True)
    return str(Path("images") / folder)


def ensure_user(db: Database, config: Config, user: dict, kind: str = "user", source_path: str = "") -> object:
    rest_id = str(user.get("rest_id") or "")
    screen_name = str(user.get("screen_name") or "")
    name = str(user.get("name") or screen_name or rest_id or "archive")
    existing = None
    if rest_id:
        existing = db.get_user_by_rest(rest_id)
    if existing is None and screen_name:
        existing = db.get_user_by_screen(screen_name)
    if existing is None and source_path:
        existing = db.get_user_by_source(source_path)
    image_dir = str(existing["image_dir"] or "") if existing else ""
    if not image_dir:
        image_dir = _image_dir_for(config, kind, rest_id, screen_name, name)
    user_id = db.upsert_user(
        rest_id=rest_id,
        screen_name=screen_name,
        name=name,
        kind=kind,
        bio=str(user.get("bio") or ""),
        location=str(user.get("location") or ""),
        url=str(user.get("url") or ""),
        avatar_url=str(user.get("avatar_url") or ""),
        banner_url=str(user.get("banner_url") or ""),
        image_dir=image_dir,
        enabled=True if kind == "user" else False,
        protected=bool(user.get("protected")),
        verified=bool(user.get("verified")),
        followers_count=int(user.get("followers_count") or 0),
        following_count=int(user.get("following_count") or 0),
        tweet_count=int(user.get("tweet_count") or 0),
        media_count=int(user.get("media_count") or 0),
        source_path=source_path,
    )
    return db.get_user(user_id)


def ensure_collection(
    db: Database,
    config: Config,
    name: str,
    kind: str,
    source_path: str,
) -> object:
    existing = db.get_user_by_source(source_path)
    if existing:
        return existing
    image_dir = _image_dir_for(config, kind, "", "", name)
    user_id = db.upsert_user(
        name=name,
        kind=kind,
        image_dir=image_dir,
        enabled=False,
        source_path=source_path,
    )
    return db.get_user(user_id)


async def resolve_user(
    db: Database,
    config: Config,
    client,
    text: str,
    enabled: bool = True,
) -> object:
    parsed = parse_user_input(text)
    if parsed is None:
        raise ValueError("请输入 @用户名、x.com/用户名 或数字 ID")
    kind, value = parsed
    if kind == "rest_id":
        info = await client.fetch_user_by_id(value)
    else:
        info = await client.fetch_user(value)
    if not info.get("rest_id") or not info.get("screen_name"):
        raise ValueError("用户资料不完整，无法创建归档")
    if kind == "rest_id":
        info["rest_id"] = value
    user = ensure_user(db, config, info)
    db.upsert_user(
        rest_id=str(info.get("rest_id") or ""),
        screen_name=str(info.get("screen_name") or ""),
        name=str(info.get("name") or ""),
        kind="user",
        bio=str(info.get("bio") or ""),
        location=str(info.get("location") or ""),
        url=str(info.get("url") or ""),
        avatar_url=str(info.get("avatar_url") or ""),
        banner_url=str(info.get("banner_url") or ""),
        image_dir=str(user["image_dir"]),
        enabled=enabled,
        protected=bool(info.get("protected")),
        verified=bool(info.get("verified")),
        followers_count=int(info.get("followers_count") or 0),
        following_count=int(info.get("following_count") or 0),
        tweet_count=int(info.get("tweet_count") or 0),
        media_count=int(info.get("media_count") or 0),
    )
    user = db.get_user(int(user["id"]))
    try:
        await download_avatar(db, config, int(user["id"]), str(info.get("avatar_url") or ""))
        await download_banner(db, config, int(user["id"]), str(info.get("banner_url") or ""))
    except Exception:
        logger.warning("下载用户头像/横幅失败 user=%s", user["id"], exc_info=True)
    return db.get_user(int(user["id"]))


async def polite_sleep(config: Config) -> None:
    delay = config.sync.sleep
    if delay <= 0:
        return
    jitter = delay * config.sync.jitter
    await asyncio.sleep(max(0.0, delay + random.uniform(-jitter, jitter)))


async def sync_user(
    db: Database,
    config: Config,
    client,
    downloader: Downloader | None,
    user,
    full: bool = False,
    dry_run: bool = False,
    no_download: bool = False,
    max_pages: int | None = None,
    max_media: int | None = None,
    on_event: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> SyncResult:
    user_id = int(user["id"])
    result = SyncResult(user_id=user_id, name=str(user["name"] or user["screen_name"] or user_id))
    if str(user["kind"]) != "user":
        result.status = "skipped"
        result.message = "导入集合不参与同步"
        return result
    rest_id = str(user["rest_id"] or "")
    if not rest_id:
        info = await client.fetch_user(str(user["screen_name"] or ""))
        rest_id = str(info.get("rest_id") or "")
        if not rest_id:
            raise RuntimeError("用户缺少 rest_id，无法同步")
        db.upsert_user(
            rest_id=rest_id,
            screen_name=str(info.get("screen_name") or user["screen_name"]),
            name=str(info.get("name") or user["name"]),
            image_dir=str(user["image_dir"]),
        )
        user = db.get_user(user_id)
        result.name = str(user["name"] or result.name)

    pages_limit = config.sync.max_pages if max_pages is None else max_pages
    media_budget = config.sync.max_media if max_media is None else max_media
    media_slots = media_budget if media_budget > 0 else None
    run_id = None if dry_run else db.start_sync_run(user_id)
    resumed = False
    if dry_run:
        cursor = ""
        stop_id = 0 if full else int(user["last_tweet_id"] or 0)
        seen_max = stop_id
    elif full:
        cursor = ""
        stop_id = 0
        seen_max = 0
        db.set_sync_session(user_id, "", 0, 0)
    elif bool(user["sync_active"]):
        cursor = str(user["sync_cursor"] or "")
        stop_id = int(user["sync_target_id"] or 0)
        seen_max = max(stop_id, int(user["sync_max_id"] or 0))
        resumed = True
        logger.info(
            "  %s (@%s) 从上次断点继续：target=%s，cursor=%s",
            result.name, user["screen_name"], stop_id, cursor[:16] or "(top)",
        )
    else:
        cursor = ""
        stop_id = int(user["last_tweet_id"] or 0)
        seen_max = stop_id
        db.set_sync_session(user_id, "", stop_id, seen_max)
    seen_cursor = cursor
    seen_cursors: set[str] = {cursor} if cursor else set()
    stopped_by_limit = False
    stopped_by_cursor = False
    stopped_by_anomaly = False
    session_completed = False
    empty_retry = 0
    empty_pages = 0
    logger.info(
        "%s (@%s) 开始%s媒体同步%s%s",
        result.name, user["screen_name"], "全量" if full else "增量",
        "（断点续传）" if resumed else "", "（dry-run）" if dry_run else "",
    )
    _emit(on_event, {"event": "user_start", "user_id": user_id, "name": result.name, "resumed": resumed})

    try:
        while True:
            if cancel_check:
                cancel_check()
            payload = await client.fetch_page(rest_id, cursor, count=config.sync.page_size)
            page = extract_timeline_page(
                payload,
                owner_screen_name=str(user["screen_name"] or ""),
                image_format=config.sync.image_format,
            )
            posts, next_cursor = page.posts, page.cursor
            pages_done = result.pages + 1
            if not posts:
                if page.has_content and not page.cursor_only and not page.terminated:
                    empty_pages += 1
                    if empty_pages >= config.sync.max_empty_pages:
                        result.pages = pages_done
                        stopped_by_anomaly = True
                        logger.warning(
                            "  连续 %d 页有内容但没有解析到推文，疑似接口/解析异常"
                            "（第 %d 页）；已保留断点并停止，不推进同步水位",
                            empty_pages, pages_done,
                        )
                        break
                elif not page.cursor_only and not page.terminated and next_cursor == cursor and empty_retry < 1:
                    empty_retry += 1
                    logger.warning("  第 %d 页为空，可能限流或数据延迟，重试一次", pages_done)
                    await polite_sleep(config)
                    continue
            else:
                empty_pages = 0
                empty_retry = 0

            page_max = 0
            candidates = []
            for post in posts:
                try:
                    page_max = max(page_max, int(post.tweet_id))
                except (TypeError, ValueError):
                    pass
                if not post.media:
                    continue
                if post.is_retweet:
                    continue
                if full or int(post.tweet_id) > stop_id:
                    candidates.append(post)

            if page_max > seen_max:
                seen_max = page_max

            if dry_run:
                result.new_posts += len(candidates)
                result.new_media += sum(len(post.media) for post in candidates)
                logger.info("  第 %d 页：解析 %d 条，新增候选 %d 条，媒体 %d", pages_done, len(posts), len(candidates), sum(len(post.media) for post in candidates))
            else:
                post_ids: list[int] = []
                if candidates:
                    with db.transaction():
                        for post in candidates:
                            post.user_id = user_id
                            existing_id = db.get_post_id(user_id, post.tweet_id)
                            post_id = db.upsert_post(post)
                            db.upsert_media(post_id, post.media)
                            if existing_id is None:
                                result.new_posts += 1
                                result.new_media += len(post.media)
                            post_ids.append(post_id)
                    author_avatar = next(
                        (post.author_avatar for post in candidates if post.author_avatar), ""
                    )
                    if author_avatar and not user["avatar_file"]:
                        try:
                            await download_avatar(db, config, user_id, author_avatar)
                        except Exception:
                            logger.warning("同步时更新头像失败 user=%s", user_id, exc_info=True)
                logger.info(
                    "  第 %d 页：解析 %d 条，新增候选 %d 条，媒体 %d",
                    pages_done, len(posts), len(candidates), result.new_media,
                )
                if post_ids and not no_download and downloader is not None and media_slots != 0:
                    rows = db.get_media_for_posts(post_ids)
                    if media_slots is not None:
                        rows = rows[:media_slots]
                    if rows:
                        def download_progress(done: int, total: int) -> None:
                            _emit(on_event, {"event": "download", "user_id": user_id, "name": result.name, "done": done, "total": total})
                        stats = await downloader.download_rows(
                            rows,
                            on_progress=download_progress,
                            cancel_check=cancel_check,
                        )
                        result.download.merge(stats)
                        if media_slots is not None:
                            media_slots = max(0, media_slots - len(rows))

            result.pages = pages_done
            _emit(on_event, {
                "event": "page", "user_id": user_id, "name": result.name, "page": pages_done,
                "items": len(posts), "new_posts": result.new_posts,
                "new_media": result.new_media, "downloaded": result.download.ok,
            })

            if page.terminated:
                session_completed = True
                logger.info("  第 %d 页收到时间线终止信号（Bottom），视为结束", pages_done)
                break
            if page.cursor_only:
                session_completed = True
                logger.info("  第 %d 页只有 cursor、没有内容，视为时间线结束", pages_done)
                break
            if not full and stop_id > 0 and page_max and page_max <= stop_id:
                session_completed = True
                break
            if not next_cursor:
                session_completed = True
                break
            if next_cursor == seen_cursor or next_cursor in seen_cursors:
                logger.warning("  检测到 cursor 未推进或循环，停止同步（第 %d 页）", pages_done)
                if dry_run:
                    session_completed = True
                else:
                    stopped_by_cursor = True
                break
            seen_cursors.add(next_cursor)
            seen_cursor = next_cursor
            if pages_limit and pages_done >= pages_limit:
                stopped_by_limit = True
                logger.info("  达到 max_pages=%s，保存断点后停止", pages_limit)
                if not dry_run:
                    db.set_sync_session(user_id, next_cursor, stop_id, seen_max)
                cursor = next_cursor
                break
            if not dry_run:
                db.set_sync_session(user_id, next_cursor, stop_id, seen_max)
            cursor = next_cursor
            await polite_sleep(config)

        if not dry_run and session_completed:
            db.set_last_tweet_id(user_id, seen_max, int(time.time()))
            db.clear_sync_session(user_id)
        result.status = "ok"
        if stopped_by_anomaly:
            result.message = "连续空页达到上限，疑似接口/解析异常；断点已保存，下次同步继续"
        elif stopped_by_cursor:
            result.status = "failed"
            result.message = "cursor 未推进，断点已保存；请清除断点或使用全量同步"
        elif stopped_by_limit and not session_completed:
            result.message = "达到 max_pages，断点已保存，下次同步继续"
        if run_id is not None:
            db.finish_sync_run(
                run_id, "failed" if stopped_by_cursor else "ok",
                pages=result.pages, new_posts=result.new_posts,
                new_media=result.new_media, message=result.message[:500],
            )
        if config.sync.export_json and not dry_run:
            export_user(db, config, user_id)
        logger.info(
            "%s (@%s) 完成：%d 页，新增 %d 条，媒体 %d；下载 %d 成功 / %d 跳过 / %d 失败 / %d 失效 / %d 受限%s",
            result.name, user["screen_name"], result.pages, result.new_posts, result.new_media,
            result.download.ok, result.download.skipped, result.download.failed,
            result.download.dead, result.download.restricted,
            "；断点已保存" if (stopped_by_limit or stopped_by_cursor or stopped_by_anomaly) and not session_completed else "",
        )
        return result
    except JobCancelled:
        result.status = "cancelled"
        result.message = "已取消，断点已保存" if not dry_run else "已取消"
        if run_id is not None:
            db.finish_sync_run(run_id, "cancelled", pages=result.pages, new_posts=result.new_posts, new_media=result.new_media, message="cancelled")
        logger.warning("%s (@%s) 同步已取消", result.name, user["screen_name"])
        raise
    except Exception as exc:
        result.status = "failed"
        result.message = describe_error(exc)
        if run_id is not None:
            db.finish_sync_run(run_id, "failed", pages=result.pages, new_posts=result.new_posts, new_media=result.new_media, message=result.message[:500])
        logger.exception("%s (@%s) 同步失败：%s；断点已保留", result.name, user["screen_name"], result.message)
        raise


async def sync_many(
    db: Database,
    config: Config,
    client,
    users: list,
    full: bool = False,
    dry_run: bool = False,
    no_download: bool = False,
    max_pages: int | None = None,
    max_media: int | None = None,
    on_event: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
) -> list[SyncResult]:
    results: list[SyncResult] = []
    total = len(users)
    _emit(on_event, {"event": "total", "total": total})
    async with Downloader(db, config) as downloader:
        for index, user in enumerate(users, 1):
            if cancel_check:
                cancel_check()
            logger.info("[%d/%d] %s (@%s)", index, total, user["name"], user["screen_name"])
            try:
                result = await sync_user(
                    db, config, client, downloader, user,
                    full=full, dry_run=dry_run, no_download=no_download,
                    max_pages=max_pages, max_media=max_media,
                    on_event=on_event, cancel_check=cancel_check,
                )
                results.append(result)
            except JobCancelled:
                raise
            except Exception as exc:
                detail = describe_error(exc)
                results.append(SyncResult(user_id=int(user["id"]), name=str(user["name"]), status="failed", message=detail))
                logger.error("用户 %s (@%s) 失败，继续下一个：%s", user["name"], user["screen_name"], detail)
            _emit(on_event, {
                "event": "user_done", "done": index, "total": total,
                "user_id": int(user["id"]), "name": str(user["name"]),
                "status": results[-1].status if results else "failed",
            })
            if index < total:
                await polite_sleep(config)
    return results


def print_sync_summary(results: list[SyncResult]) -> None:
    if not results:
        return
    ok = sum(1 for result in results if result.status == "ok")
    logger.info("")
    logger.info("同步汇总：成功 %d，失败/跳过 %d", ok, len(results) - ok)
    for result in results:
        marker = "OK " if result.status == "ok" else "ERR"
        logger.info("  [%s] %s (%s) %d 页 / %d 条 / %d 媒体 %s", marker, result.name, result.user_id, result.pages, result.new_posts, result.new_media, result.message)
