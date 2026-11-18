from __future__ import annotations

import logging
from typing import Callable

from .config import Config
from .db import Database
from .download import DownloadStats, Downloader
from .utils import resolve_media_path

logger = logging.getLogger(__name__)


def verify_media(
    db: Database,
    config: Config,
    user_id: int | None = None,
    dry_run: bool = False,
    deep: bool = False,
) -> dict:
    rows = db.get_media_with_user(user_id)
    stats = {"checked": 0, "ok": 0, "missing": 0, "restored": 0, "bytes": 0}
    for row in rows:
        stats["checked"] += 1
        try:
            path = resolve_media_path(config.data_dir, row["image_dir"], row["file_name"])
        except ValueError:
            path = None
        exists = bool(path and path.is_file() and path.stat().st_size > 0)
        if exists and deep:
            try:
                with open(path, "rb") as handle:
                    head = handle.read(16)
                if not head:
                    exists = False
                elif head.lstrip().startswith((b"<html", b"<!DOCTYPE", b"{", b"[")):
                    exists = False
            except OSError:
                exists = False
        size = path.stat().st_size if exists and path else 0
        if exists:
            stats["ok"] += 1
            stats["bytes"] += size
            if row["status"] != "ok":
                stats["restored"] += 1
                if not dry_run:
                    db.update_media_status(int(row["id"]), "ok", local_size=size, http_status=200)
        elif row["status"] == "ok":
            stats["missing"] += 1
            if not dry_run:
                db.update_media_status(int(row["id"]), "missing", local_size=0, error="local file missing")
        elif row["status"] == "missing":
            stats["missing"] += 1
    logger.info(
        "校验完成：检查 %d，存在 %d，缺失 %d，状态修正 %d",
        stats["checked"], stats["ok"], stats["missing"], stats["restored"],
    )
    return stats


async def repair_media(
    db: Database,
    config: Config,
    user_id: int | None = None,
    max_media: int = 0,
    statuses: tuple[str, ...] = ("missing", "failed", "pending"),
    dry_run: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    user_ids: list[int] | None = None,
) -> DownloadStats:
    rows = db.get_repair_media(user_id=user_id, user_ids=user_ids, limit=max_media, statuses=statuses)
    if dry_run:
        logger.info("待补下载 %d 个媒体（dry-run）", len(rows))
        return DownloadStats()
    if not rows:
        logger.info("没有需要补下载的媒体")
        return DownloadStats()
    logger.info("开始补下载 %d 个媒体", len(rows))
    async with Downloader(db, config) as downloader:
        stats = await downloader.download_rows(
            rows, max_media=max_media, on_progress=on_progress, cancel_check=cancel_check
        )
    logger.info(
        "补下载完成：成功 %d，跳过 %d，失败 %d，失效 %d，受限 %d",
        stats.ok, stats.skipped, stats.failed, stats.dead, stats.restricted,
    )
    return stats
