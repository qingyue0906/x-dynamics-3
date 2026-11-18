from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .db import Database
from .utils import canonical_media_key, human_size, resolve_media_path

logger = logging.getLogger(__name__)


@dataclass
class DedupeStats:
    groups: int = 0
    duplicate_rows: int = 0
    removed_rows: int = 0
    updated_keys: int = 0
    files_deleted: int = 0
    bytes_freed: int = 0
    skipped_files: int = 0
    errors: list[str] = field(default_factory=list)


def _canonical(row: dict) -> str:
    return canonical_media_key(
        row.get("url") or "",
        row.get("download_url") or "",
        row.get("media_type") or "",
        row.get("file_name") or "",
    )


def _file_state(config: Config, row: dict) -> tuple[Path | None, int]:
    try:
        path = resolve_media_path(config.data_dir, row.get("image_dir") or "", row.get("file_name") or "")
    except ValueError:
        return None, 0
    try:
        if path.is_file():
            return path, path.stat().st_size
    except OSError:
        pass
    return path, 0


def dedupe_media(
    db: Database,
    config: Config,
    user_id: int | None = None,
    dry_run: bool = True,
    delete_files: bool = False,
) -> DedupeStats:
    """Merge media rows that point at the same X media and optionally remove duplicate files."""
    rows = db.get_media_rows_for_dedupe(user_id=user_id)
    post_groups: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        post_groups[int(row["post_id"])].append(row)

    stats = DedupeStats()
    for post_id, post_rows in post_groups.items():
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in post_rows:
            groups[_canonical(row)].append(row)
        for canonical, group in groups.items():
            if len(group) > 1:
                stats.groups += 1
                stats.duplicate_rows += len(group) - 1
            with_file = []
            for row in group:
                path, size = _file_state(config, row)
                if path and size > 0:
                    with_file.append((row, path, size))
            keeper = min(with_file, key=lambda item: int(item[0]["id"]))[0] if with_file else min(group, key=lambda row: int(row["id"]))
            extras = [row for row in group if int(row["id"]) != int(keeper["id"])]
            if not extras:
                if canonical and keeper.get("media_key") != canonical:
                    if not dry_run:
                        try:
                            db.set_media_key(int(keeper["id"]), canonical)
                            stats.updated_keys += 1
                        except Exception as exc:
                            stats.errors.append(f"post {post_id} media {keeper['id']} key: {exc}")
                    else:
                        stats.updated_keys += 1
                continue

            keeper_path, keeper_size = _file_state(config, keeper)
            for extra in extras:
                if not dry_run:
                    try:
                        db.delete_media(int(extra["id"]))
                        stats.removed_rows += 1
                    except Exception as exc:
                        stats.errors.append(f"post {post_id} media {extra['id']} delete: {exc}")
                        continue
                else:
                    stats.removed_rows += 1
                if delete_files and not dry_run:
                    extra_path, extra_size = _file_state(config, extra)
                    if extra_path and extra_size > 0:
                        if not keeper_path or keeper_size <= 0:
                            stats.skipped_files += 1
                        elif extra_size != keeper_size:
                            stats.skipped_files += 1
                        else:
                            try:
                                extra_path.unlink()
                                stats.files_deleted += 1
                                stats.bytes_freed += extra_size
                            except OSError as exc:
                                stats.errors.append(f"delete file {extra_path}: {exc}")
            if canonical and keeper.get("media_key") != canonical:
                if not dry_run:
                    try:
                        db.set_media_key(int(keeper["id"]), canonical)
                        stats.updated_keys += 1
                    except Exception as exc:
                        stats.errors.append(f"post {post_id} media {keeper['id']} key: {exc}")
                else:
                    stats.updated_keys += 1
        if not dry_run and len(post_rows) > 1:
            db.refresh_post_media_counts(post_id)

    logger.info(
        "%s：重复组 %d，重复行 %d，删除行 %d，更新 key %d，删除文件 %d（%s），跳过文件 %d，错误 %d",
        "去重预演" if dry_run else "去重完成",
        stats.groups,
        stats.duplicate_rows,
        stats.removed_rows,
        stats.updated_keys,
        stats.files_deleted,
        human_size(stats.bytes_freed),
        stats.skipped_files,
        len(stats.errors),
    )
    if stats.errors:
        for error in stats.errors[:20]:
            logger.warning("  %s", error)
    return stats
