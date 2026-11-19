from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .settings import SettingsStore

logger = logging.getLogger(__name__)


@dataclass
class SyncConfig:
    data_dir: Path
    sleep: float = 4.0
    jitter: float = 0.2
    timeout: float = 60.0
    connect_timeout: float = 15.0
    retries: int = 3
    download_concurrency: int = 3
    export_json: bool = True
    avatar_sleep: float = 1.2
    max_pages: int = 0
    max_media: int = 0
    max_empty_pages: int = 3
    page_size: int = 40
    image_format: str = "orig"
    proxy: str = ""
    api_backend: str = "httpx"


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 5001
    page_size: int = 24
    media_page_size: int = 48
    thumbnail_width: int = 400
    default_view: str = "dynamic"
    default_sort: str = "desc"
    default_load: str = "infinite"
    show_empty_posts: bool = True
    show_missing_media: bool = True
    blur_sensitive: bool = False
    auto_open: bool = True
    debug: bool = False


@dataclass
class Config:
    base_dir: Path
    data_dir: Path
    sync: SyncConfig
    web: WebConfig
    settings_store: SettingsStore
    auth_token: str = ""
    ct0: str = ""
    cookies: dict[str, str] | None = None

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "x.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def avatars_dir(self) -> Path:
        return self.data_dir / "avatars"

    @property
    def banners_dir(self) -> Path:
        return self.data_dir / "banners"

    @property
    def thumbnails_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def job_logs_dir(self) -> Path:
        return self.logs_dir / "jobs"

    @property
    def parse_logs_dir(self) -> Path:
        return self.logs_dir / "parse"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def legacy_dir(self) -> Path:
        return self.data_dir / "legacy"

    @property
    def query_cache_path(self) -> Path:
        return self.config_dir / "gql_ops.json"

    @property
    def has_cookie(self) -> bool:
        return bool(self.auth_token and self.ct0)

    @property
    def cookie_map(self) -> dict[str, str]:
        result = dict(self.cookies or {})
        if self.auth_token:
            result["auth_token"] = self.auth_token
        if self.ct0:
            result["ct0"] = self.ct0
        return result

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.config_dir,
            self.images_dir,
            self.avatars_dir,
            self.banners_dir,
            self.thumbnails_dir,
            self.exports_dir,
            self.imports_dir,
            self.logs_dir,
            self.job_logs_dir,
            self.parse_logs_dir,
            self.backups_dir,
            self.legacy_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def require_cookie(self) -> None:
        if not self.has_cookie:
            raise SystemExit(
                "还没有配置 X Cookie。请启动 WebUI，在“设置”页填写 auth_token 和 ct0，"
                "或直接编辑 data/config/cookies.json。"
            )


def load_config(
    base_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
    sleep: float | None = None,
    max_pages: int | None = None,
    max_media: int | None = None,
    host: str | None = None,
    port: int | None = None,
    debug: bool | None = None,
    auto_open: bool | None = None,
) -> Config:
    root = Path(base_dir).resolve() if base_dir else Path(__file__).resolve().parent.parent
    root.mkdir(parents=True, exist_ok=True)

    chosen = data_dir or os.environ.get("XDYN_DATA_DIR") or "data"
    data_path = Path(chosen)
    if not data_path.is_absolute():
        data_path = (root / data_path).resolve()

    store = SettingsStore(data_path)
    sync_values = store.sync()
    web_values = store.web()
    cookies = store.cookies()

    sync = SyncConfig(
        data_dir=data_path,
        sleep=float(sync_values["sleep"] if sleep is None else max(0.0, sleep)),
        jitter=float(sync_values["jitter"]),
        timeout=float(sync_values["timeout"]),
        connect_timeout=float(sync_values["connect_timeout"]),
        retries=int(sync_values["retries"]),
        download_concurrency=int(sync_values["download_concurrency"]),
        export_json=bool(sync_values["export_json"]),
        avatar_sleep=float(sync_values["avatar_sleep"]),
        max_pages=max(0, int(sync_values["max_pages"] if max_pages is None else max_pages)),
        max_media=max(0, int(sync_values["max_media"] if max_media is None else max_media)),
        max_empty_pages=int(sync_values["max_empty_pages"]),
        page_size=int(sync_values["page_size"]),
        image_format=str(sync_values["image_format"]),
        proxy=str(sync_values["proxy"]),
        api_backend=str(sync_values["api_backend"]),
    )
    web = WebConfig(
        host=str(host or web_values["host"] or "127.0.0.1"),
        port=int(port or web_values["port"]),
        page_size=max(1, min(60, int(web_values["page_size"]))),
        media_page_size=max(1, min(120, int(web_values["media_page_size"]))),
        thumbnail_width=max(120, min(1200, int(web_values["thumbnail_width"]))),
        default_view=str(web_values["default_view"]),
        default_sort=str(web_values["default_sort"]),
        default_load=str(web_values["default_load"]),
        show_empty_posts=bool(web_values["show_empty_posts"]),
        show_missing_media=bool(web_values["show_missing_media"]),
        blur_sensitive=bool(web_values["blur_sensitive"]),
        auto_open=bool(web_values["auto_open"] if auto_open is None else auto_open),
        debug=bool(web_values["debug"] if debug is None else debug),
    )

    config = Config(
        base_dir=root,
        data_dir=data_path,
        sync=sync,
        web=web,
        settings_store=store,
        auth_token=cookies["auth_token"],
        ct0=cookies["ct0"],
        cookies=cookies["cookies"],
    )
    config.ensure_dirs()
    return config
