from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

_WINDOWS_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TIME_RE = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?"
)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_TWEET_URL_RE = re.compile(
    r"(?:^|//)(?:www\.|mobile\.)?(?:x|twitter|vxtwitter|fxtwitter)\.com/"
    r"(?:[^/\s]+/)*status(?:es)?/(\d+)",
    re.I,
)
_TWEET_ID_RE = re.compile(r"^\s*(\d+)\s*$")


def to_int(value, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def strip_query(url: str) -> str:
    if not url:
        return ""
    return url.split("?", 1)[0].split("#", 1)[0]


def normalize_url(url: str) -> str:
    clean = strip_query(url or "").strip()
    if not clean:
        return ""
    if clean.startswith("//"):
        clean = "https:" + clean
    elif clean.startswith("http://"):
        clean = "https://" + clean[7:]
    elif not clean.startswith(("http://", "https://")):
        clean = "https://" + clean
    return clean


def sanitize_filename(name: str, fallback: str = "file") -> str:
    name = unquote(name or "")
    name = _WINDOWS_BAD.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        return fallback
    if len(name) > 170:
        stem, ext = os.path.splitext(name)
        name = stem[:150] + ext
    if name.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "_" + name
    return name


def slugify_name(name: str, max_len: int = 60) -> str:
    value = _WINDOWS_BAD.sub("_", (name or "").strip()).strip().strip(".")
    value = re.sub(r"\s+", " ", value)
    if len(value) > max_len:
        value = value[:max_len].rstrip()
    return value or "archive"


def filename_from_url(url: str, fallback_ext: str = ".jpg") -> str:
    clean = strip_query(url)
    path = urlsplit(clean).path
    name = sanitize_filename(posixpath.basename(path), fallback="")
    if not name:
        digest = hashlib.sha1((url or "").encode("utf-8", "replace")).hexdigest()[:16]
        name = digest + fallback_ext
    if not os.path.splitext(name)[1]:
        name += fallback_ext
    return name


def parse_tweet_input(text: str) -> str | None:
    value = html.unescape((text or "").strip())
    if not value:
        return None
    match = _TWEET_ID_RE.match(value)
    if match:
        return match.group(1)
    match = _TWEET_URL_RE.search(value)
    if match:
        return match.group(1)
    return None


def parse_user_input(text: str) -> tuple[str, str] | None:
    """Return ("screen_name", handle) or ("rest_id", id)."""
    value = html.unescape((text or "").strip())
    if not value:
        return None
    if value.isdigit():
        return ("rest_id", value)
    value = value.lstrip("@")
    if _HANDLE_RE.match(value):
        return ("screen_name", value)
    match = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})", value, re.I)
    if match:
        return ("screen_name", match.group(1))
    return None


def parse_created_at(value) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 10**12 else number
    text = str(value).strip()
    if text.isdigit():
        return parse_created_at(int(text))
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            return int(parsed.timestamp())
        return int(parsed.timestamp())
    except Exception:
        pass
    match = _TIME_RE.search(text)
    if not match:
        return 0
    year, month, day, hour, minute, second = match.groups()
    try:
        return int(
            datetime(
                int(year), int(month), int(day),
                int(hour or 0), int(minute or 0), int(second or 0),
            ).timestamp()
        )
    except (TypeError, ValueError, OverflowError):
        return 0


def parse_old_time(value: str) -> int:
    """Parse the old twitter_download CSV local time (YYYY-MM-DD HH:MM)."""
    text = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H-%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return parse_created_at(text)


def format_ts(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        return ""


def format_duration(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds or 0) // 1000)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}:{seconds % 60:02d}"


def media_kind(media_type: str, ext: str = "") -> str:
    value = str(media_type or "").lower()
    if value in ("video", "animated_gif", "gif"):
        return "video"
    if value == "photo" or value == "image":
        return "image"
    if ext.lower() in (".mp4", ".m4v", ".webm", ".mov"):
        return "video"
    return "image"


def extension_for_content_type(content_type: str) -> str:
    value = (content_type or "").split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
    }.get(value, "")


def is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_media_path(data_dir: Path, image_dir: str, file_name: str) -> Path:
    base = Path(image_dir)
    if not base.is_absolute():
        base = Path(data_dir) / base
    base = base.resolve()
    target = (base / file_name).resolve()
    if not is_within(base, target):
        raise ValueError(f"非法媒体路径：{target}")
    return target


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding=encoding, newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def compact_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def human_size(num_bytes: int) -> str:
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def sha1_text(value: str) -> str:
    return hashlib.sha1((value or "").encode("utf-8", "replace")).hexdigest()


def canonical_media_key(
    url: str = "",
    download_url: str = "",
    media_type: str = "",
    file_name: str = "",
) -> str:
    """Stable identity shared by migrated rows and freshly synced rows.

    Photos are identified by their clean media URL. Videos/GIFs are identified
    by the clean video variant URL because their poster URL and download URL
    differ. Rows without URLs fall back to the file name.
    """
    kind = str(media_type or "").lower()
    target = download_url if kind in ("video", "animated_gif") else (url or download_url)
    target = strip_query(target or "")
    target = normalize_url(target)
    if target:
        return target
    name = (file_name or "").strip().lower()
    return "file:" + sha1_text(name or "media")


def unique_path(directory: Path, file_name: str, size_hint: int | None = None) -> Path:
    """Pick a non-conflicting path, keeping the original name where possible."""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / sanitize_filename(file_name)
    if not candidate.exists():
        return candidate
    if size_hint is not None:
        try:
            if candidate.stat().st_size == size_hint:
                return candidate
        except OSError:
            pass
    stem, ext = os.path.splitext(candidate.name)
    digest = sha1_text(f"{file_name}:{time.time_ns()}")[:6]
    return directory / f"{stem}_{digest}{ext}"
