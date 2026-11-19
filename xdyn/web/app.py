from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import threading
import warnings
from io import BytesIO
from pathlib import Path

import httpx
from flask import Flask, abort, g, jsonify, render_template, request, send_file, url_for
from PIL import Image, ImageOps

from ..config import Config, load_config
from ..db import Database
from ..http import DEFAULT_UA
from ..jobs import run_avatars, run_retry, run_sync, run_tweet, run_verify
from ..single import preview_tweet
from ..sync import resolve_user
from ..tasks import TaskManager, read_log_tail
from ..utils import format_ts, human_size, resolve_media_path
from ..xclient import XClient

logger = logging.getLogger(__name__)

PLACEHOLDER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400">
<rect width="400" height="400" fill="#f1f2f3"/>
<circle cx="200" cy="176" r="42" fill="#d9dce0"/>
<path d="M130 300l58-72 44 50 34-36 54 58z" fill="#c7cbd0"/>
<text x="200" y="352" text-anchor="middle" font-family="sans-serif" font-size="18" fill="#9499a0">媒体缺失</text>
</svg>"""

VIDEO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400" viewBox="0 0 400 400">
<rect width="400" height="400" fill="#101214"/>
<circle cx="200" cy="200" r="52" fill="rgba(255,255,255,.18)"/>
<path d="M186 174l58 26-58 26z" fill="#fff"/>
<text x="200" y="300" text-anchor="middle" font-family="sans-serif" font-size="16" fill="#c7cbd0">视频</text>
</svg>"""

_thumb_locks: dict[int, threading.Lock] = {}
_thumb_guard = threading.Lock()


def _lock_for(media_id: int) -> threading.Lock:
    with _thumb_guard:
        if media_id not in _thumb_locks:
            _thumb_locks[media_id] = threading.Lock()
        return _thumb_locks[media_id]


def _fetch_remote_image(config: Config, url: str) -> bytes:
    headers = {"User-Agent": DEFAULT_UA, "Referer": "https://x.com/"}
    timeout = httpx.Timeout(20.0, connect=10.0)
    with httpx.Client(
        proxy=config.sync.proxy or None,
        follow_redirects=True,
        timeout=timeout,
        headers=headers,
    ) as client:
        response = client.get(url)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        return response.content


def _video_thumb(config: Config, row, media_id: int, width: int):
    poster_url = str(row["poster_url"] or "")
    if not poster_url:
        return _placeholder(video=True)
    digest = hashlib.sha1(poster_url.encode("utf-8")).hexdigest()[:8]
    cache_path = config.thumbnails_dir / f"{media_id}_poster_{digest}_{width}.webp"
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        return send_file(cache_path, mimetype="image/webp", conditional=True, max_age=31536000)
    lock = _lock_for(media_id)
    with lock:
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            return send_file(cache_path, mimetype="image/webp", conditional=True, max_age=31536000)
        try:
            payload = _fetch_remote_image(config, poster_url)
            _make_thumbnail(BytesIO(payload), cache_path, width)
        except Exception as exc:
            logger.warning("生成视频封面失败 media=%s：%s", media_id, exc)
            return _placeholder(video=True)
    return send_file(cache_path, mimetype="image/webp", conditional=True, max_age=31536000)


class AppState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.settings = config.settings_store
        self.tasks = TaskManager(config)

    def reload(self) -> None:
        self.config = load_config(base_dir=self.config.base_dir, data_dir=self.config.data_dir)
        self.settings = self.config.settings_store


def create_app(config: Config) -> Flask:
    state = AppState(config)
    package_dir = Path(__file__).resolve().parent
    app = Flask(
        __name__,
        template_folder=str(package_dir / "templates"),
        static_folder=str(package_dir / "static"),
        static_url_path="/static",
    )
    app.config["XDYN_STATE"] = state

    def get_db() -> Database:
        if "xdyn_db" not in g:
            g.xdyn_db = Database(state.config.db_path, init=False)
        return g.xdyn_db

    @app.teardown_appcontext
    def close_db(_exc=None):
        db = g.pop("xdyn_db", None)
        if db is not None:
            db.close()

    @app.template_filter("timestamp_time")
    def _timestamp_time(value):
        return format_ts(value)

    @app.context_processor
    def inject_globals():
        db = get_db()
        return {
            "users": db.list_users_with_counts(),
            "stats": db.stats(),
            "web_settings": state.settings.web(),
            "active_job": state.tasks.active_job(),
            "settings_store": state.settings,
            "cookie_status": state.settings.masked_cookies(),
            "human_size": human_size,
        }

    @app.before_request
    def _csrf_guard():
        if (
            request.method in ("POST", "PUT", "PATCH", "DELETE")
            and request.path.startswith("/api/")
            and request.headers.get("X-Xdyn") != "1"
        ):
            abort(403)

    @app.after_request
    def add_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def _page_args():
        try:
            offset = max(0, int(request.args.get("offset", "0")))
        except ValueError:
            offset = 0
        try:
            limit = int(request.args.get("limit", str(state.config.web.page_size)))
        except ValueError:
            limit = state.config.web.page_size
        limit = max(1, min(120, limit))
        return offset, limit

    def _filters():
        raw_user = request.args.get("user_id")
        try:
            user_id = int(raw_user) if raw_user else None
        except ValueError:
            user_id = None
        q = (request.args.get("q") or "").strip()
        sort = "asc" if (request.args.get("sort") or "").lower() == "asc" else "desc"
        return user_id, q, sort

    def _feed_state():
        web = state.config.web
        mode = request.args.get("mode") or web.default_view
        if mode not in ("dynamic", "images"):
            mode = "dynamic"
        sort = request.args.get("sort") or web.default_sort
        if sort not in ("desc", "asc"):
            sort = "desc"
        load_mode = request.args.get("load") or web.default_load
        if load_mode not in ("infinite", "pages"):
            load_mode = "infinite"
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            page = 1
        page = max(1, page)
        if load_mode == "infinite":
            page = 1
        return mode, sort, load_mode, page

    def _feed_url(endpoint: str, path_args: dict | None = None, include_user: bool = False, **overrides):
        keys = ("q", "user_id", "sort", "mode", "load")
        args = {key: request.args.get(key) for key in keys if request.args.get(key) not in (None, "")}
        if not include_user:
            args.pop("user_id", None)
        args.update({key: value for key, value in overrides.items() if value is not None})
        if "page" not in overrides:
            args.pop("page", None)
        return url_for(endpoint, **(path_args or {}), **args)

    def _pagination(endpoint: str, path_args, include_user, page: int, total: int, limit: int):
        total_pages = max(1, (total + limit - 1) // limit) if total else 1
        page = min(max(1, page), total_pages)
        pages = []
        start = max(1, page - 3)
        end = min(total_pages, start + 6)
        start = max(1, end - 6)
        for number in range(start, end + 1):
            pages.append((number, _feed_url(endpoint, path_args=path_args, include_user=include_user, page=number), number == page))
        prev_url = _feed_url(endpoint, path_args=path_args, include_user=include_user, page=page - 1) if page > 1 else None
        next_url = _feed_url(endpoint, path_args=path_args, include_user=include_user, page=page + 1) if page < total_pages else None
        return {
            "page": page, "total_pages": total_pages, "page_links": pages,
            "prev_url": prev_url, "next_url": next_url,
        }

    def _render_feed(template, endpoint, path_args, include_user, user_id, q, page_title, **extra):
        db = get_db()
        mode, sort, load_mode, page = _feed_state()
        web = state.config.web
        limit = web.media_page_size if mode == "images" else web.page_size
        include_empty = bool(web.show_empty_posts)
        include_missing = bool(web.show_missing_media)
        if mode == "images":
            total = db.count_media_with_posts(user_id=user_id, q=q or None, include_missing=include_missing)
        else:
            total = db.count_posts(user_id=user_id, q=q or None, include_empty=include_empty)
        if load_mode == "infinite":
            page = 1
        else:
            total_pages = max(1, (total + limit - 1) // limit) if total else 1
            page = min(page, total_pages)
        offset = (page - 1) * limit
        if mode == "images":
            items, has_more = db.list_media_with_posts(
                limit=limit, offset=offset, user_id=user_id, q=q or None,
                sort=sort, include_missing=include_missing,
            )
        else:
            items, has_more = db.list_posts_with_media(
                limit=limit, offset=offset, user_id=user_id, q=q or None,
                sort=sort, include_empty=include_empty,
            )
        page_data = _pagination(endpoint, path_args, include_user, page, total, limit)
        page = page_data["page"]
        if mode == "images" and page_title == "全部动态":
            page_title = "全部媒体"
        return render_template(
            template,
            posts=items if mode == "dynamic" else [],
            album_items=items if mode == "images" else [],
            mode=mode, sort=sort, load_mode=load_mode, has_more=has_more,
            next_offset=(offset + len(items)) if has_more else 0,
            limit=limit, total=total, current_user_id=user_id, query=q,
            page_title=page_title,
            feed_url=lambda **kwargs: _feed_url(endpoint, path_args=path_args, include_user=include_user, **kwargs),
            **page_data, **extra,
        )

    @app.route("/")
    def index():
        return _render_feed("index.html", "index", None, False, None, "", "全部动态", active_page="home")

    @app.route("/user/<int:user_id>")
    def user_page(user_id: int):
        user = get_db().get_user_with_counts(user_id)
        if not user:
            abort(404)
        return _render_feed("user.html", "user_page", {"user_id": user_id}, False, user_id, "", str(user["name"] or user["screen_name"] or user_id), active_page="home", user=user)

    @app.route("/search")
    def search():
        user_id, q, sort = _filters()
        return _render_feed("search.html", "search", None, True, user_id, q, f"搜索：{q}" if q else "搜索", active_page="home")

    @app.route("/api/posts")
    def api_posts():
        db = get_db()
        offset, limit = _page_args()
        user_id, q, sort = _filters()
        posts, has_more = db.list_posts_with_media(
            limit=limit, offset=offset, user_id=user_id, q=q or None,
            sort=sort, include_empty=bool(state.config.web.show_empty_posts),
        )
        return jsonify({
            "html": render_template("partials/post_list.html", posts=posts),
            "has_more": has_more,
            "next_offset": (offset + len(posts)) if has_more else 0,
        })

    @app.route("/api/media")
    def api_media():
        db = get_db()
        offset, limit = _page_args()
        user_id, q, sort = _filters()
        items, has_more = db.list_media_with_posts(
            limit=limit, offset=offset, user_id=user_id, q=q or None,
            sort=sort, include_missing=bool(state.config.web.show_missing_media),
        )
        return jsonify({
            "html": render_template("partials/album_list.html", album_items=items),
            "has_more": has_more,
            "next_offset": (offset + len(items)) if has_more else 0,
        })

    @app.route("/media/<int:media_id>")
    def media(media_id: int):
        row = get_db().get_media_with_user_by_id(media_id)
        if not row:
            abort(404)
        try:
            path = resolve_media_path(state.config.data_dir, row["image_dir"], row["file_name"])
        except ValueError:
            abort(404)
        if not path.is_file():
            abort(404)
        return send_file(
            path, conditional=True, max_age=86400,
            as_attachment=request.args.get("download") == "1",
            download_name=path.name,
        )

    @app.route("/thumb/<int:media_id>")
    def thumb(media_id: int):
        row = get_db().get_media_with_user_by_id(media_id)
        if not row:
            return _placeholder()
        try:
            width = int(request.args.get("w", str(state.config.web.thumbnail_width)))
        except ValueError:
            width = state.config.web.thumbnail_width
        width = max(160, min(1200, width))
        if row["media_type"] in ("video", "animated_gif"):
            return _video_thumb(state.config, row, media_id, width)
        try:
            source = resolve_media_path(state.config.data_dir, row["image_dir"], row["file_name"])
        except ValueError:
            return _placeholder()
        if not source.is_file() or source.stat().st_size <= 0:
            return _placeholder()
        try:
            mtime = int(source.stat().st_mtime)
        except OSError:
            return _placeholder()
        cache_path = state.config.thumbnails_dir / f"{media_id}_{mtime}_{width}.webp"
        if cache_path.is_file() and cache_path.stat().st_size > 0:
            return send_file(cache_path, mimetype="image/webp", conditional=True, max_age=31536000)
        lock = _lock_for(media_id)
        with lock:
            if cache_path.is_file() and cache_path.stat().st_size > 0:
                return send_file(cache_path, mimetype="image/webp", conditional=True, max_age=31536000)
            try:
                _make_thumbnail(source, cache_path, width)
            except Exception as exc:
                logger.warning("生成缩略图失败 %s：%s", source, exc)
                return _placeholder()
        return send_file(cache_path, mimetype="image/webp", conditional=True, max_age=31536000)

    @app.route("/avatar/<int:user_id>")
    def avatar(user_id: int):
        db = get_db()
        user = db.get_user(user_id)
        if user and user["avatar_file"]:
            path = state.config.avatars_dir / Path(user["avatar_file"]).name
            if path.is_file() and path.stat().st_size > 0:
                return send_file(path, mimetype="image/webp", conditional=True, max_age=31536000)
        name = str((user["name"] if user else "") or "?")
        initial = name[:1] or "?"
        hue = int(user_id) % 360
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">
<rect width="160" height="160" rx="80" fill="hsl({hue} 58% 60%)"/>
<text x="80" y="101" text-anchor="middle" font-family="sans-serif" font-size="64" font-weight="600" fill="#fff">{html.escape(initial)}</text>
</svg>"""
        return svg, 200, {"Content-Type": "image/svg+xml; charset=utf-8", "Cache-Control": "public, max-age=3600"}

    @app.route("/banner/<int:user_id>")
    def banner(user_id: int):
        user = get_db().get_user(user_id)
        if user and user["banner_file"]:
            path = state.config.banners_dir / Path(user["banner_file"]).name
            if path.is_file() and path.stat().st_size > 0:
                return send_file(path, mimetype="image/webp", conditional=True, max_age=31536000)
        return "", 404

    # ---------------- pages ----------------

    @app.route("/users")
    def users_page():
        db = get_db()
        return render_template(
            "users.html", user_rows=db.list_users_with_counts(),
            retry_counts=db.get_retry_counts(), page_title="用户",
            active_page="users", current_user_id=None, query="",
        )

    @app.route("/jobs")
    def jobs_page():
        return render_template(
            "jobs.html", jobs=get_db().list_jobs(60), page_title="任务",
            active_page="jobs", current_user_id=None, query="",
        )

    @app.route("/settings")
    def settings_page():
        return render_template(
            "settings.html", sync=state.settings.sync(), web=state.settings.web(),
            cookie=state.settings.masked_cookies(), data_dir=str(state.config.data_dir),
            page_title="设置", active_page="settings", current_user_id=None, query="",
        )

    @app.route("/tweet")
    def tweet_page():
        return render_template(
            "tweet.html", page_title="指定推文", active_page="tweet",
            current_user_id=None, query="",
        )

    # ---------------- user APIs ----------------

    @app.post("/api/users/add")
    def api_users_add():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("input") or "")
        if not text.strip():
            return jsonify(ok=False, error="请输入 @用户名、链接或数字 ID"), 400
        config = state.config
        db = get_db()
        try:
            async def _run():
                async with XClient(config) as client:
                    return await resolve_user(db, config, client, text, enabled=bool(payload.get("enabled", True)))
            user = asyncio.run(_run())
        except SystemExit as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 400
        return jsonify(ok=True, user_id=int(user["id"]), name=str(user["name"]), screen_name=str(user["screen_name"]))

    @app.post("/api/users/<int:user_id>/toggle")
    def api_user_toggle(user_id: int):
        payload = request.get_json(silent=True) or {}
        db = get_db()
        if not db.get_user(user_id):
            return jsonify(ok=False, error="用户不存在"), 404
        enabled = bool(payload.get("enabled"))
        db.set_user_enabled(user_id, enabled)
        return jsonify(ok=True, user_id=user_id, enabled=enabled)

    @app.post("/api/users/<int:user_id>/reset-sync")
    def api_user_reset_sync(user_id: int):
        db = get_db()
        if not db.get_user(user_id):
            return jsonify(ok=False, error="用户不存在"), 404
        db.clear_sync_session(user_id)
        return jsonify(ok=True)

    # ---------------- job helpers ----------------

    def _submit(kind: str, target: str, func) -> int:
        return state.tasks.submit(kind, target, func)

    @app.post("/api/sync")
    def api_sync():
        payload = request.get_json(silent=True) or {}
        raw_ids = payload.get("user_ids")
        user_ids = [int(value) for value in raw_ids] if isinstance(raw_ids, list) and raw_ids else None
        job_id = _submit("sync", "selected" if user_ids else "all", lambda ctx: run_sync(
            ctx, user_ids=user_ids,
            full=bool(payload.get("full")),
            no_download=bool(payload.get("no_download")),
            max_pages=int(payload.get("max_pages") or 0) or None,
            max_media=int(payload.get("max_media") or 0) or None,
        ))
        return jsonify(ok=True, job_id=job_id)

    @app.post("/api/retry")
    def api_retry():
        payload = request.get_json(silent=True) or {}
        raw_ids = payload.get("user_ids")
        user_ids = [int(value) for value in raw_ids] if isinstance(raw_ids, list) and raw_ids else None
        job_id = _submit("retry", "selected" if user_ids else "all", lambda ctx: run_retry(
            ctx, user_ids=user_ids, max_media=int(payload.get("max_media") or 0)
        ))
        return jsonify(ok=True, job_id=job_id)

    @app.post("/api/verify")
    def api_verify():
        payload = request.get_json(silent=True) or {}
        user_id = int(payload.get("user_id")) if payload.get("user_id") else None
        job_id = _submit("verify", str(user_id or "all"), lambda ctx: run_verify(
            ctx, user_id=user_id, repair=bool(payload.get("repair")),
            max_media=int(payload.get("max_media") or 0), deep=bool(payload.get("deep")),
        ))
        return jsonify(ok=True, job_id=job_id)

    @app.post("/api/avatars/refresh")
    def api_avatars_refresh():
        payload = request.get_json(silent=True) or {}
        raw_ids = payload.get("user_ids")
        user_ids = [int(value) for value in raw_ids] if isinstance(raw_ids, list) and raw_ids else None
        job_id = _submit("avatars", "selected" if user_ids else "all", lambda ctx: run_avatars(ctx, user_ids=user_ids))
        return jsonify(ok=True, job_id=job_id)

    # ---------------- tweet APIs ----------------

    @app.post("/api/tweet/preview")
    def api_tweet_preview():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("input") or "")
        try:
            result = asyncio.run(preview_tweet(state.config, text))
        except SystemExit as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 400
        return jsonify(ok=True, post=result)

    @app.post("/api/tweet/download")
    def api_tweet_download():
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("input") or "")
        from ..utils import parse_tweet_input
        if not parse_tweet_input(text):
            return jsonify(ok=False, error="无法识别推文 ID"), 400
        add_user = bool(payload.get("add_user"))
        job_id = _submit("tweet", text, lambda ctx: run_tweet(ctx, text, add_user=add_user))
        return jsonify(ok=True, job_id=job_id)

    # ---------------- job APIs ----------------

    def _job_dict(row) -> dict:
        item = dict(row)
        total = int(item.get("total") or 0)
        done = int(item.get("done") or 0)
        item["percent"] = int(done * 100 / total) if total else (100 if item.get("status") == "succeeded" else 0)
        item["active"] = item.get("status") in ("queued", "running", "cancelling")
        return item

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify(ok=True, jobs=[_job_dict(row) for row in get_db().list_jobs(60)])

    @app.get("/api/jobs/<int:job_id>")
    def api_job(job_id: int):
        row = get_db().get_job(job_id)
        if not row:
            return jsonify(ok=False, error="任务不存在"), 404
        item = _job_dict(row)
        item["log"] = read_log_tail(Path(row["log_path"]), limit=100) if row["log_path"] else []
        return jsonify(ok=True, job=item)

    @app.post("/api/jobs/<int:job_id>/cancel")
    def api_job_cancel(job_id: int):
        ok = state.tasks.cancel(job_id)
        return jsonify(ok=ok, error="" if ok else "任务无法取消"), (200 if ok else 400)

    # ---------------- settings APIs ----------------

    @app.get("/api/settings")
    def api_settings_get():
        return jsonify(
            ok=True, sync=state.settings.sync(), web=state.settings.web(),
            cookies=state.settings.masked_cookies(), data_dir=str(state.config.data_dir),
        )

    @app.post("/api/settings/options")
    def api_settings_options():
        payload = request.get_json(silent=True) or {}
        try:
            state.settings.save_options(
                sync=payload.get("sync") if isinstance(payload.get("sync"), dict) else None,
                web=payload.get("web") if isinstance(payload.get("web"), dict) else None,
            )
        except Exception as exc:
            return jsonify(ok=False, error=str(exc)), 400
        state.reload()
        return jsonify(ok=True, sync=state.settings.sync(), web=state.settings.web())

    @app.post("/api/settings/cookies")
    def api_settings_cookies():
        payload = request.get_json(silent=True) or {}
        current = state.settings.cookies()
        cookie_header = str(payload.get("cookie") or "").strip()
        if cookie_header:
            try:
                state.settings.save_cookie_header(cookie_header)
            except ValueError as exc:
                return jsonify(ok=False, error=str(exc)), 400
        else:
            auth_token = str(payload.get("auth_token") or "").strip() or current["auth_token"]
            ct0 = str(payload.get("ct0") or "").strip() or current["ct0"]
            if not auth_token or not ct0:
                return jsonify(ok=False, error="auth_token 和 ct0 不能为空"), 400
            state.settings.save_cookies(auth_token, ct0, current["cookies"])
        state.reload()
        if not state.config.has_cookie:
            return jsonify(ok=False, error="没有解析到 auth_token 或 ct0，请粘贴完整 Cookie"), 400
        try:
            async def _test():
                async with XClient(state.config) as client:
                    return await client.auth_check()
            valid, message = asyncio.run(_test())
        except Exception as exc:
            valid, message = False, str(exc)
        return jsonify(ok=True, message=message, valid=valid, cookies=state.settings.masked_cookies())

    @app.post("/api/settings/cookies/test")
    def api_settings_cookies_test():
        if not state.config.has_cookie:
            return jsonify(ok=False, error="还没有配置 Cookie"), 400
        try:
            async def _test():
                async with XClient(state.config) as client:
                    return await client.auth_check()
            valid, message = asyncio.run(_test())
        except Exception as exc:
            valid, message = False, str(exc)
        return jsonify(ok=valid, message=message), (200 if valid else 400)

    @app.post("/api/settings/cookies/clear")
    def api_settings_cookies_clear():
        state.settings.clear_cookies()
        state.reload()
        return jsonify(ok=True, cookies=state.settings.masked_cookies())

    @app.route("/health")
    def health():
        return jsonify({"ok": True, **get_db().stats()})

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template(
            "error.html", code=404, message="页面不存在",
            current_user_id=None, query="", page_title="404", active_page="",
        ), 404

    return app


def _placeholder(video: bool = False):
    return (
        VIDEO_SVG if video else PLACEHOLDER_SVG,
        200,
        {"Content-Type": "image/svg+xml; charset=utf-8", "Cache-Control": "no-store"},
    )


def _make_thumbnail(source: Path, destination: Path, width: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".tmp")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                if getattr(image, "is_animated", False):
                    try:
                        image.seek(0)
                    except Exception:
                        pass
                image.thumbnail((width, width * 2), Image.Resampling.LANCZOS)
                if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGB", rgba.size, (255, 255, 255))
                    background.paste(rgba, mask=rgba.split()[-1])
                    image = background
                else:
                    image = image.convert("RGB")
                image.save(tmp, "WEBP", quality=80, method=4)
        os.replace(tmp, destination)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
