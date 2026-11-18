from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import webbrowser
from pathlib import Path

from .avatars import refresh_avatars
from .config import load_config
from .db import Database
from .dedupe import dedupe_media
from .export import export_all, export_user, import_exports
from .logging_setup import setup_logging
from .migrate import migrate_twitter_download
from .sync import print_sync_summary, resolve_user, sync_many
from .userdata import prepare_runtime
from .utils import human_size
from .verify import repair_media, verify_media
from .xclient import XClient

logger = logging.getLogger(__name__)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", help="数据目录，默认 data")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")


def _add_sync_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sleep", type=float, help="翻页间隔秒数，默认使用 WebUI 设置")
    parser.add_argument("--max-pages", type=int, help="本次最多抓取页数，0 为不限")
    parser.add_argument("--max-media", type=int, help="本次最多下载媒体数，0 为不限")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xdyn",
        description="x-dynamics-3：WebUI 驱动的 X 用户媒体本地归档",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    sync = sub.add_parser("sync", help="增量/全量同步用户")
    _add_common(sync)
    _add_sync_limits(sync)
    sync.add_argument("--all", action="store_true", help="同步所有启用用户（默认行为）")
    sync.add_argument("--user", help="@用户名、x.com/用户名 或数字 ID")
    sync.add_argument("--mode", choices=("media", "timeline", "replies"), help="覆盖本次同步模式")
    sync.add_argument("--full", action="store_true", help="忽略断点，抓取全部历史")
    sync.add_argument("--dry-run", action="store_true", help="只抓取解析，不写库不下载")
    sync.add_argument("--no-download", action="store_true", help="只写元数据，不下载媒体")
    sync.add_argument("--limit-users", type=int, default=0, help="仅处理前 N 个用户")
    sync.add_argument("--include-retweets", action="store_true", help="timeline/replies 模式包含转推")

    retry = sub.add_parser("retry", help="重试失败/缺失/待下载媒体")
    _add_common(retry)
    retry.add_argument("--all", action="store_true", help="处理所有用户（默认行为）")
    retry.add_argument("--user-id", type=int)
    retry.add_argument("--max-media", type=int, default=0)
    retry.add_argument("--dry-run", action="store_true")

    verify = sub.add_parser("verify", help="校验本地媒体与数据库记录")
    _add_common(verify)
    verify.add_argument("--user-id", type=int)
    verify.add_argument("--repair", action="store_true", help="校验后补下载")
    verify.add_argument("--max-media", type=int, default=0)
    verify.add_argument("--deep", action="store_true", help="读取文件头检测损坏/HTML 错误页")
    verify.add_argument("--dry-run", action="store_true")

    migrate = sub.add_parser("migrate", help="迁移 twitter_download 的旧数据")
    _add_common(migrate)
    migrate.add_argument("--src", required=True, help="旧 downloads 目录")
    migrate.add_argument("--mode", choices=("copy", "move", "reference"), default="copy", help="默认 copy")
    migrate.add_argument("--include", default="main,text,tags,replies,profile", help="要迁移的类别，逗号分隔")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--limit", type=int, default=0, help="仅处理前 N 个目录")

    serve = sub.add_parser("serve", help="启动 WebUI")
    _add_common(serve)
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--debug", action="store_true")
    serve.add_argument("--no-open", action="store_true", help="不自动打开浏览器")

    stats = sub.add_parser("stats", help="查看统计")
    _add_common(stats)

    export = sub.add_parser("export", help="导出每用户 JSON")
    _add_common(export)
    export.add_argument("--user-id", type=int)

    import_json = sub.add_parser("import-json", help="从导出 JSON 重建数据库")
    _add_common(import_json)
    import_json.add_argument("--dir", help="导出目录，默认 data/exports")
    import_json.add_argument("--dry-run", action="store_true")

    refresh = sub.add_parser("refresh-ids", help="刷新 GraphQL queryId 和 transaction")
    _add_common(refresh)

    dedupe = sub.add_parser("dedupe-media", help="合并迁移/同步产生的重复媒体记录")
    _add_common(dedupe)
    dedupe.add_argument("--user-id", type=int)
    dedupe.add_argument("--apply", action="store_true", help="实际写数据库（默认只预演）")
    dedupe.add_argument("--dry-run", action="store_true", help="只预演，不修改（默认行为）")
    dedupe.add_argument("--delete-files", action="store_true", help="删除多余且大小相同的本地文件")

    doctor = sub.add_parser("doctor", help="检查 Cookie、transaction、queryId 和 API")
    _add_common(doctor)

    avatars = sub.add_parser("avatars", help="补全头像和横幅")
    _add_common(avatars)
    avatars.add_argument("--all", action="store_true")
    avatars.add_argument("--user-id", type=int, action="append")

    return parser


def _make_config(args):
    return load_config(
        data_dir=getattr(args, "data_dir", None),
        sleep=getattr(args, "sleep", None),
        max_pages=getattr(args, "max_pages", None),
        max_media=getattr(args, "max_media", None),
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        debug=True if getattr(args, "debug", False) else None,
        auto_open=False if getattr(args, "no_open", False) else None,
    )


def _cmd_sync(args, config) -> int:
    config.require_cookie()
    db = Database(config.db_path)
    try:
        async def _run():
            async with XClient(config) as client:
                if args.user:
                    user = await resolve_user(db, config, client, args.user, enabled=True)
                    if args.mode:
                        db.set_user_mode(int(user["id"]), args.mode, include_retweets=args.include_retweets)
                        user = db.get_user(int(user["id"]))
                    targets = [user]
                else:
                    targets = list(db.list_sync_users())
                    if args.mode:
                        for user in targets:
                            db.set_user_mode(int(user["id"]), args.mode, include_retweets=args.include_retweets)
                        targets = list(db.list_sync_users())
                if args.limit_users:
                    targets = targets[: args.limit_users]
                if not targets:
                    logger.error("没有启用的用户。请启动 WebUI，在“用户”页面添加用户。")
                    return []
                return await sync_many(
                    db, config, client, targets,
                    full=args.full, dry_run=args.dry_run, no_download=args.no_download,
                    max_pages=args.max_pages, max_media=args.max_media,
                )
        results = asyncio.run(_run())
        print_sync_summary(results)
        return 1 if any(result.status not in ("ok", "skipped") for result in results) else 0
    finally:
        db.close()


def _cmd_retry(args, config) -> int:
    if not args.dry_run:
        config.require_cookie()
    db = Database(config.db_path)
    try:
        asyncio.run(repair_media(
            db, config, user_id=args.user_id, max_media=args.max_media,
            statuses=("pending", "failed", "missing"), dry_run=args.dry_run,
        ))
        return 0
    finally:
        db.close()


def _cmd_verify(args, config) -> int:
    db = Database(config.db_path)
    try:
        stats = verify_media(db, config, user_id=args.user_id, dry_run=args.dry_run, deep=args.deep)
        logger.info(
            "校验完成：检查 %d，存在 %d，缺失 %d，状态修正 %d，合计 %s",
            stats["checked"], stats["ok"], stats["missing"], stats["restored"], human_size(stats["bytes"]),
        )
        if args.repair:
            if not args.dry_run:
                config.require_cookie()
            asyncio.run(repair_media(
                db, config, user_id=args.user_id, max_media=args.max_media,
                statuses=("missing", "failed", "pending"), dry_run=args.dry_run,
            ))
        return 0
    finally:
        db.close()


def _cmd_migrate(args, config) -> int:
    db = Database(config.db_path)
    try:
        include = {part.strip() for part in str(args.include or "").split(",") if part.strip()}
        stats = migrate_twitter_download(
            db, config, args.src, mode=args.mode, include=include,
            dry_run=args.dry_run, limit=args.limit,
        )
        return 1 if stats.errors else 0
    finally:
        db.close()


def _cmd_serve(args, config) -> int:
    from .web.app import create_app

    app = create_app(config)
    url = f"http://{config.web.host}:{config.web.port}/"
    logger.info("WebUI: %s", url)
    if config.web.auto_open:
        threading.Timer(1.0, lambda: webbrowser.open_new(url)).start()
    if config.web.debug:
        app.run(host=config.web.host, port=config.web.port, debug=True, use_reloader=False)
        return 0
    try:
        from waitress import serve
        serve(app, host=config.web.host, port=config.web.port, threads=8)
    except ImportError:
        logger.warning("未安装 waitress，回退 Flask 开发服务器")
        app.run(host=config.web.host, port=config.web.port, threaded=True, use_reloader=False)
    return 0


def _cmd_stats(args, config) -> int:
    db = Database(config.db_path)
    try:
        stats = db.stats()
        print(f"用户：{stats['users']}（启用 {stats['enabled_users']}），导入集合：{stats['collections']}，头像：{stats['avatars']}")
        print(f"推文：{stats['posts']}（有媒体 {stats['media_posts']}，无媒体 {stats['empty_posts']}）")
        print(f"媒体：{stats['media']}（本地 {stats['ok_media']}，待下载 {stats['pending_media']}，缺失/失败 {stats['missing_media']}，失效 {stats['dead_media']}，受限 {stats['restricted_media']}）")
        print(f"本地媒体占用：{human_size(stats['bytes'])}")
        print("")
        for user in db.list_users_with_counts():
            state = "启用" if user["enabled"] else ("集合" if user["kind"] != "user" else "停用")
            print(f"  [{state}] {user['name']} (@{user['screen_name']}) {user['post_count']} 条 / {user['media_ref_count']} 媒体")
        return 0
    finally:
        db.close()


def _cmd_export(args, config) -> int:
    db = Database(config.db_path)
    try:
        if args.user_id:
            path = export_user(db, config, args.user_id)
            if not path:
                logger.error("用户不存在：%s", args.user_id)
                return 2
            logger.info("已导出 %s", path)
        else:
            paths = export_all(db, config)
            logger.info("已导出 %d 个用户到 %s", len(paths), config.exports_dir)
        return 0
    finally:
        db.close()


def _cmd_import_json(args, config) -> int:
    db = Database(config.db_path)
    try:
        stats = import_exports(db, config, Path(args.dir) if args.dir else None, dry_run=args.dry_run)
        return 1 if stats.skipped else 0
    finally:
        db.close()


def _cmd_refresh(args, config) -> int:
    config.require_cookie()
    async def _run():
        async with XClient(config) as client:
            changed = await client.refresh_ids()
            return changed
    changed = asyncio.run(_run())
    logger.info("刷新完成，变化 %d 个 operation", len(changed))
    return 0


def _cmd_dedupe(args, config) -> int:
    db = Database(config.db_path)
    try:
        stats = dedupe_media(
            db, config, user_id=args.user_id,
            dry_run=args.dry_run or not args.apply, delete_files=args.delete_files,
        )
        return 1 if stats.errors else 0
    finally:
        db.close()


def _cmd_doctor(args, config) -> int:
    from .doctor import run_doctor
    return asyncio.run(run_doctor(config))


def _cmd_avatars(args, config) -> int:
    config.require_cookie()
    db = Database(config.db_path)
    try:
        async def _run():
            async with XClient(config) as client:
                return await refresh_avatars(db, config, client=client, user_ids=args.user_id)
        stats = asyncio.run(_run())
        logger.info("头像补全：成功 %d，跳过 %d，失败 %d", stats["ok"], stats["skipped"], stats["failed"])
        return 0
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if not raw_args:
        raw_args = ["serve"]
    elif raw_args[0].startswith("-") and raw_args[0] not in ("-h", "--help"):
        raw_args = ["serve", *raw_args]
    args = parser.parse_args(raw_args)
    config = _make_config(args)
    setup_logging(config.logs_dir, args.command or "serve", verbose=getattr(args, "verbose", False))
    prepare_runtime(config)
    handlers = {
        "sync": _cmd_sync,
        "retry": _cmd_retry,
        "verify": _cmd_verify,
        "migrate": _cmd_migrate,
        "serve": _cmd_serve,
        "stats": _cmd_stats,
        "export": _cmd_export,
        "import-json": _cmd_import_json,
        "refresh-ids": _cmd_refresh,
        "dedupe-media": _cmd_dedupe,
        "doctor": _cmd_doctor,
        "avatars": _cmd_avatars,
    }
    handler = handlers.get(args.command or "serve")
    if handler is None:
        parser.print_help()
        return 2
    return handler(args, config)
