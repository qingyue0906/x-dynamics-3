from __future__ import annotations

import asyncio
import logging

from .avatars import refresh_avatars
from .config import load_config
from .db import Database
from .single import download_tweet
from .sync import sync_many
from .tasks import JobContext
from .verify import repair_media, verify_media
from .xclient import XClient

logger = logging.getLogger(__name__)


def _config(ctx: JobContext):
    return load_config(base_dir=ctx.base_dir, data_dir=ctx.data_dir)


def _targets(db: Database, user_ids: list[int] | None):
    if user_ids:
        rows = [db.get_user(user_id) for user_id in user_ids]
        return [row for row in rows if row and row["kind"] == "user"]
    return list(db.list_sync_users())


def run_sync(
    ctx: JobContext,
    user_ids: list[int] | None = None,
    full: bool = False,
    no_download: bool = False,
    max_pages: int | None = None,
    max_media: int | None = None,
) -> None:
    config = _config(ctx)
    config.require_cookie()
    db = Database(config.db_path)
    try:
        targets = _targets(db, user_ids)
        if not targets:
            raise ValueError("没有可同步的用户。请先在“用户”页面添加或启用用户。")
        ctx.update(total=len(targets), done=0, message=f"准备同步 {len(targets)} 个用户")

        def on_event(event: dict) -> None:
            kind = event.get("event")
            if kind == "total":
                ctx.update(total=int(event.get("total") or 0))
            elif kind == "user_start":
                ctx.update(message=f"同步 {event.get('name')}")
            elif kind == "page":
                ctx.update(message=(
                    f"{event.get('name')} 第 {event.get('page')} 页："
                    f"本页 {event.get('items')} 条，新增 {event.get('new_posts')} 条，"
                    f"媒体 {event.get('new_media')}"
                ))
            elif kind == "download":
                ctx.update(message=f"{event.get('name')} 下载媒体 {event.get('done')}/{event.get('total')}")
            elif kind == "user_done":
                ctx.update(done=int(event.get("done") or 0), message=f"{event.get('name')} 完成（{event.get('status')}）")

        async def _run():
            async with XClient(config) as client:
                return await sync_many(
                    db, config, client, targets,
                    full=full, no_download=no_download,
                    max_pages=max_pages, max_media=max_media,
                    on_event=on_event, cancel_check=ctx.check_cancel,
                )

        results = asyncio.run(_run())
        failed = sum(1 for result in results if result.status != "ok")
        ctx.update(done=len(results), message=f"同步结束：{len(results)} 个用户，失败 {failed} 个")
    finally:
        db.close()


def run_retry(ctx: JobContext, user_ids: list[int] | None = None, max_media: int = 0) -> None:
    config = _config(ctx)
    db = Database(config.db_path)
    try:
        rows = db.get_repair_media(user_ids=user_ids, limit=0, statuses=("missing", "failed", "pending"))
        if max_media:
            rows = rows[:max_media]
        if not rows:
            ctx.update(total=0, done=0, message="没有需要重试的媒体")
            return
        ctx.update(total=len(rows), done=0, message=f"准备重试 {len(rows)} 个媒体")

        def on_progress(done: int, total: int) -> None:
            ctx.update(done=done, total=total, message=f"重试媒体 {done}/{total}")

        asyncio.run(repair_media(
            db, config, user_ids=user_ids, max_media=max_media,
            on_progress=on_progress, cancel_check=ctx.check_cancel,
        ))
        ctx.update(message="重试完成")
    finally:
        db.close()


def run_verify(ctx: JobContext, user_id: int | None = None, repair: bool = False, max_media: int = 0, deep: bool = False) -> None:
    config = _config(ctx)
    db = Database(config.db_path)
    try:
        ctx.update(total=0, done=0, message="正在校验本地媒体")
        stats = verify_media(db, config, user_id=user_id, dry_run=False, deep=deep)
        ctx.update(message=(
            f"校验完成：检查 {stats['checked']}，存在 {stats['ok']}，"
            f"缺失 {stats['missing']}，状态修正 {stats['restored']}"
        ))
        if repair:
            rows = db.get_repair_media(user_id=user_id, limit=max_media, statuses=("missing", "failed", "pending"))
            if rows:
                ctx.update(total=len(rows), done=0, message=f"补下 {len(rows)} 个媒体")

                def on_progress(done: int, total: int) -> None:
                    ctx.update(done=done, total=total, message=f"补下媒体 {done}/{total}")

                asyncio.run(repair_media(
                    db, config, user_id=user_id, max_media=max_media,
                    statuses=("missing", "failed", "pending"),
                    on_progress=on_progress, cancel_check=ctx.check_cancel,
                ))
                ctx.update(message="校验与补下完成")
    finally:
        db.close()


def run_avatars(ctx: JobContext, user_ids: list[int] | None = None) -> None:
    config = _config(ctx)
    config.require_cookie()
    db = Database(config.db_path)
    try:
        users = db.list_users_with_counts()
        if user_ids:
            wanted = {int(value) for value in user_ids}
            users = [user for user in users if int(user["id"]) in wanted]
        ctx.update(total=len(users), done=0, message=f"准备补全 {len(users)} 个头像")

        def on_progress(done: int, total: int) -> None:
            ctx.update(done=done, total=total, message=f"补全头像 {done}/{total}")

        async def _run():
            async with XClient(config) as client:
                return await refresh_avatars(
                    db, config, client=client, user_ids=user_ids,
                    on_progress=on_progress, cancel_check=ctx.check_cancel,
                )

        stats = asyncio.run(_run())
        ctx.update(message=f"头像补全完成：成功 {stats['ok']}，跳过 {stats['skipped']}，失败 {stats['failed']}")
    finally:
        db.close()


def run_tweet(ctx: JobContext, text: str, add_user: bool = False) -> None:
    config = _config(ctx)
    config.require_cookie()
    db = Database(config.db_path)
    try:
        ctx.update(total=0, done=0, message="正在获取推文详情")

        def on_progress(done: int, total: int) -> None:
            ctx.update(done=done, total=total, message=f"下载指定推文 {done}/{total}")

        result = asyncio.run(download_tweet(
            db, config, text, add_user=add_user,
            on_progress=on_progress, cancel_check=ctx.check_cancel,
        ))
        ctx.update(done=result.get("downloaded", 0), message=result.get("message", "完成"))
    finally:
        db.close()
