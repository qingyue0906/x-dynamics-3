from __future__ import annotations

import base64
import copy
import json
import logging
import os
import threading
import time
from pathlib import Path

from .utils import atomic_write_json

logger = logging.getLogger(__name__)

DEFAULT_SYNC = {
    "sleep": 4.0,
    "jitter": 0.2,
    "timeout": 60.0,
    "connect_timeout": 15.0,
    "retries": 3,
    "download_concurrency": 3,
    "export_json": True,
    "avatar_sleep": 1.2,
    "max_pages": 0,
    "max_media": 0,
    "max_empty_pages": 3,
    "page_size": 40,
    "include_retweets": False,
    "download_quoted_media": False,
    "image_format": "orig",
    "proxy": "",
    "api_backend": "httpx",
}

DEFAULT_WEB = {
    "host": "127.0.0.1",
    "port": 5001,
    "page_size": 24,
    "media_page_size": 48,
    "thumbnail_width": 400,
    "default_view": "dynamic",
    "default_sort": "desc",
    "default_load": "infinite",
    "show_empty_posts": True,
    "show_missing_media": True,
    "blur_sensitive": False,
    "auto_open": True,
    "debug": False,
}

DEFAULT_COOKIES = {
    "auth_token": "",
    "ct0": "",
    "cookies": {},
    "updated_at": 0,
}


def _clamp(value, low, high, cast=float, default=0):
    try:
        number = cast(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clean_sync(raw: dict) -> dict:
    data = copy.deepcopy(DEFAULT_SYNC)
    data.update(raw if isinstance(raw, dict) else {})
    return {
        "sleep": _clamp(data.get("sleep"), 0, 300, float, 4.0),
        "jitter": _clamp(data.get("jitter"), 0, 1, float, 0.2),
        "timeout": _clamp(data.get("timeout"), 5, 600, float, 60.0),
        "connect_timeout": _clamp(data.get("connect_timeout"), 1, 120, float, 15.0),
        "retries": _clamp(data.get("retries"), 0, 10, int, 3),
        "download_concurrency": _clamp(data.get("download_concurrency"), 1, 12, int, 3),
        "export_json": bool(data.get("export_json", True)),
        "avatar_sleep": _clamp(data.get("avatar_sleep"), 0, 30, float, 1.2),
        "max_pages": _clamp(data.get("max_pages"), 0, 10000, int, 0),
        "max_media": _clamp(data.get("max_media"), 0, 1000000, int, 0),
        "max_empty_pages": _clamp(data.get("max_empty_pages"), 1, 20, int, 3),
        "page_size": _clamp(data.get("page_size"), 10, 100, int, 40),
        "include_retweets": bool(data.get("include_retweets", False)),
        "download_quoted_media": bool(data.get("download_quoted_media", False)),
        "image_format": str(data.get("image_format") or "orig")
        if str(data.get("image_format") or "orig") in ("orig", "jpg", "png")
        else "orig",
        "proxy": str(data.get("proxy") or "").strip(),
        "api_backend": "curl" if str(data.get("api_backend") or "").lower() == "curl" else "httpx",
    }


def _enum(value, allowed: tuple[str, ...], default: str) -> str:
    value = str(value or "")
    return value if value in allowed else default


def _clean_web(raw: dict) -> dict:
    data = copy.deepcopy(DEFAULT_WEB)
    data.update(raw if isinstance(raw, dict) else {})
    return {
        "host": "127.0.0.1",
        "port": _clamp(data.get("port"), 1, 65535, int, 5001),
        "page_size": _clamp(data.get("page_size"), 1, 60, int, 24),
        "media_page_size": _clamp(data.get("media_page_size"), 1, 120, int, 48),
        "thumbnail_width": _clamp(data.get("thumbnail_width"), 120, 1200, int, 400),
        "default_view": _enum(data.get("default_view"), ("dynamic", "images"), "dynamic"),
        "default_sort": _enum(data.get("default_sort"), ("desc", "asc"), "desc"),
        "default_load": _enum(data.get("default_load"), ("infinite", "pages"), "infinite"),
        "show_empty_posts": bool(data.get("show_empty_posts", True)),
        "show_missing_media": bool(data.get("show_missing_media", True)),
        "blur_sensitive": bool(data.get("blur_sensitive", False)),
        "auto_open": bool(data.get("auto_open", True)),
        "debug": bool(data.get("debug", False)),
    }


def parse_cookie_input(value: str) -> dict[str, str]:
    """Accept a Cookie header, JSON object, or browser-extension style JSON."""
    raw = (value or "").strip()
    if not raw:
        return {}
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        if "=" in decoded or decoded.strip().startswith(("{", "[")):
            raw = decoded
    except Exception:
        pass
    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if isinstance(parsed, dict) and "cookies" in parsed:
        parsed = parsed["cookies"]
    if isinstance(parsed, dict):
        return {str(key): str(val) for key, val in parsed.items()}
    if isinstance(parsed, list):
        result = {}
        for item in parsed:
            if isinstance(item, dict) and item.get("name"):
                result[str(item["name"])] = str(item.get("value") or "")
        return result
    result = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        if key:
            result[key] = val.strip()
    return result


class SettingsStore:
    """User-owned settings and cookies under <data>/config/."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.config_dir = self.data_dir / "config"
        self.settings_path = self.config_dir / "settings.json"
        self.cookies_path = self.config_dir / "cookies.json"
        self._lock = threading.RLock()
        self.ensure()

    def ensure(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not self.settings_path.is_file():
            atomic_write_json(
                self.settings_path,
                {"sync": copy.deepcopy(DEFAULT_SYNC), "web": copy.deepcopy(DEFAULT_WEB)},
            )
        if not self.cookies_path.is_file():
            atomic_write_json(self.cookies_path, copy.deepcopy(DEFAULT_COOKIES))
            self._chmod_private(self.cookies_path)

    @staticmethod
    def _chmod_private(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _read_json(self, path: Path, fallback: dict) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return copy.deepcopy(fallback)
        except Exception as exc:
            logger.error("读取用户配置失败 %s：%s，已使用默认值", path, exc)
            try:
                path.replace(path.with_name(path.name + f".broken-{int(time.time())}"))
            except OSError:
                pass
            return copy.deepcopy(fallback)
        return data if isinstance(data, dict) else copy.deepcopy(fallback)

    def load(self) -> dict:
        with self._lock:
            raw = self._read_json(self.settings_path, {})
            return {
                "sync": _clean_sync(raw.get("sync") if isinstance(raw, dict) else {}),
                "web": _clean_web(raw.get("web") if isinstance(raw, dict) else {}),
            }

    def sync(self) -> dict:
        return self.load()["sync"]

    def web(self) -> dict:
        return self.load()["web"]

    def cookies(self) -> dict:
        with self._lock:
            raw = self._read_json(self.cookies_path, DEFAULT_COOKIES)
            cookie_map = raw.get("cookies")
            if not isinstance(cookie_map, dict):
                cookie_map = {}
            cookie_map = {str(key): str(val) for key, val in cookie_map.items()}
            auth_token = str(raw.get("auth_token") or cookie_map.get("auth_token") or "").strip()
            ct0 = str(raw.get("ct0") or cookie_map.get("ct0") or "").strip()
            if auth_token:
                cookie_map["auth_token"] = auth_token
            if ct0:
                cookie_map["ct0"] = ct0
            return {
                "auth_token": auth_token,
                "ct0": ct0,
                "cookies": cookie_map,
                "updated_at": int(raw.get("updated_at") or 0),
            }

    def has_cookies(self) -> bool:
        cookies = self.cookies()
        return bool(cookies["auth_token"] and cookies["ct0"])

    def masked_cookies(self) -> dict:
        cookies = self.cookies()
        return {
            "auth_token": bool(cookies["auth_token"]),
            "ct0": bool(cookies["ct0"]),
            "cookie_count": len(cookies["cookies"]),
            "updated_at": cookies["updated_at"],
            "configured": bool(cookies["auth_token"] and cookies["ct0"]),
        }

    def save_cookies(
        self,
        auth_token: str,
        ct0: str,
        cookie_map: dict[str, str] | None = None,
    ) -> dict:
        with self._lock:
            full = {str(key): str(val) for key, val in (cookie_map or {}).items()}
            if auth_token:
                full["auth_token"] = auth_token
            if ct0:
                full["ct0"] = ct0
            payload = {
                "auth_token": (auth_token or "").strip(),
                "ct0": (ct0 or "").strip(),
                "cookies": full,
                "updated_at": int(time.time()),
            }
            atomic_write_json(self.cookies_path, payload)
            self._chmod_private(self.cookies_path)
            return payload

    def save_cookie_header(self, value: str) -> dict:
        cookie_map = parse_cookie_input(value)
        if not cookie_map.get("auth_token") or not cookie_map.get("ct0"):
            raise ValueError("没有从 Cookie 中解析到 auth_token 或 ct0")
        return self.save_cookies(
            cookie_map.get("auth_token", ""),
            cookie_map.get("ct0", ""),
            cookie_map,
        )

    def clear_cookies(self) -> None:
        with self._lock:
            atomic_write_json(self.cookies_path, copy.deepcopy(DEFAULT_COOKIES))
            self._chmod_private(self.cookies_path)

    def save_options(self, sync: dict | None = None, web: dict | None = None) -> dict:
        with self._lock:
            current = self.load()
            if sync is not None:
                current["sync"] = _clean_sync({**current["sync"], **sync})
            if web is not None:
                current["web"] = _clean_web({**current["web"], **web})
            atomic_write_json(self.settings_path, current)
            return current
