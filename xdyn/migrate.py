from __future__ import annotations

import csv
import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageOps

from .config import Config
from .db import Database
from .models import MediaRecord, PostRecord
from .sync import ensure_collection, ensure_user
from .utils import (
    atomic_write_json,
    canonical_media_key,
    media_kind,
    normalize_url,
    parse_old_time,
    parse_tweet_input,
    sanitize_filename,
    strip_query,
    to_int,
    unique_path,
)

logger = logging.getLogger(__name__)

MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm"}
CSV_META = {"tweet range", "save path", "run time"}
REPLY_FILE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2} \d{2}-\d{2})_(?P<user>@.+?)_(?P<hash>[0-9a-fA-F]{4})_reply\.(?P<ext>mp4|png|jpg|jpeg|webp)$",
    re.I,
)


@dataclass
class MigrateStats:
    users: int = 0
    collections: int = 0
    posts: int = 0
    media_refs: int = 0
    ok_media: int = 0
    pending_media: int = 0
    missing_media: int = 0
    copied: int = 0
    moved: int = 0
    bytes: int = 0
    profiles: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    unmatched_rows: list[str] = field(default_factory=list)
    unmatched_files: list[str] = field(default_factory=list)
    used_files: set[str] = field(default_factory=set, repr=False)


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _csv_rows(path: Path) -> tuple[list[str], list[dict]]:
    text = _read_text(path)
    if not text:
        return [], []
    reader = list(csv.reader(text.splitlines()))
    header_index = -1
    header: list[str] = []
    for index, row in enumerate(reader[:8]):
        cells = [cell.strip() for cell in row]
        lowered = [cell.lower() for cell in cells]
        joined = " ".join(lowered)
        if joined in CSV_META:
            continue
        if "tweet date" in lowered or "reply date" in lowered or "tweet url" in lowered or "reply url" in lowered:
            header_index = index
            header = cells
            break
    if header_index < 0:
        return [], []
    rows = []
    for raw in reader[header_index + 1:]:
        if not any(cell.strip() for cell in raw):
            continue
        rows.append({header[i]: (raw[i].strip() if i < len(raw) else "") for i in range(len(header))})
    return header, rows


def _csv_kind(header: list[str]) -> str:
    keys = set(header)
    if "Reply URL" in keys and "Parent Tweet URL" in keys:
        return "reply"
    if "Media URL" in keys and "Saved Path" in keys:
        return "tag_media"
    if "Media URL" in keys and "Saved Filename" in keys:
        return "main_media"
    if "Tweet URL" in keys and "Tweet Content" in keys:
        return "text"
    return ""


def _looks_like_tag_folder(folder: Path) -> bool:
    for path in folder.glob("*.csv"):
        text = _read_text(path).lstrip("\ufeff").strip()
        if text.lower().startswith("run time"):
            return True
    return False


def _find_file(folder: Path, value: str) -> Path | None:
    raw = unquote((value or "").strip().strip('"'))
    if not raw:
        return None
    name = os.path.basename(raw.replace("\\", "/"))
    candidate = folder / name
    if candidate.is_file():
        return candidate
    for path in folder.iterdir():
        if path.is_file() and path.name == name:
            return path
    return None


def _copy_or_move(source: Path, destination_dir: Path, mode: str) -> tuple[Path | None, str]:
    destination_dir.mkdir(parents=True, exist_ok=True)
    try:
        if source.resolve() == (destination_dir / sanitize_filename(source.name)).resolve():
            return source, "kept"
    except OSError:
        pass
    target = destination_dir / sanitize_filename(source.name)
    if target.exists():
        try:
            if target.stat().st_size == source.stat().st_size:
                return target, "kept"
        except OSError:
            pass
        target = unique_path(destination_dir, source.name, source.stat().st_size)
    try:
        if mode == "move":
            shutil.move(str(source), str(target))
            return target, "moved"
        shutil.copy2(source, target)
        return target, "copied"
    except Exception as exc:
        logger.warning("复制媒体失败 %s -> %s：%s", source, target, exc)
        return None, "failed"


def _media_from_row(
    folder: Path,
    row: dict,
    kind: str,
    tweet_id: str,
    index: int,
) -> MediaRecord | None:
    media_type = str(row.get("Media Type") or "").strip()
    media_url = normalize_url(str(row.get("Media URL") or ""))
    saved = str(row.get("Saved Filename") or row.get("Saved Path") or "").strip()
    if not saved and kind != "reply":
        return None
    source = _find_file(folder, saved) if saved else None
    file_name = sanitize_filename(os.path.basename((saved or "").replace("\\", "/")))
    if not file_name:
        file_name = f"{tweet_id}_{index}.bin"
    ext = Path(file_name).suffix
    is_video = media_kind(media_type, ext) == "video"
    if not media_type:
        media_type = "video" if is_video else "photo"
    if is_video:
        media_type = "video"
        file_name = str(Path(file_name).with_suffix(".mp4")) if ext.lower() not in (".mp4", ".mov", ".webm") else file_name
    else:
        media_type = "photo"
    download_url = media_url
    if media_url and not is_video and "name=" not in media_url:
        download_url = f"{strip_query(media_url)}?name=orig"
    record = MediaRecord(
        media_key=canonical_media_key(media_url, download_url, media_type, file_name),
        media_type=media_type,
        url=media_url,
        download_url=download_url,
        file_name=file_name,
        position=index,
        status="pending" if download_url else "missing",
    )
    if source and source.is_file() and source.stat().st_size > 0:
        record.status = "ok"
        record.local_size = source.stat().st_size
    return record


def _new_group(owner_id: int, tweet_id: str, url: str) -> dict:
    return {
        "owner_id": owner_id,
        "tweet_id": tweet_id,
        "url": url,
        "post": None,
        "media": [],
        "source_files": [],
    }


def _post_from_fields(
    owner_id: int,
    tweet_id: str,
    url: str,
    row: dict,
    kind: str,
    is_retweet: bool = False,
    is_reply: bool = False,
) -> PostRecord:
    if kind == "reply":
        name = str(row.get("Replier Display Name") or "")
        screen = str(row.get("Replier User Name") or "").lstrip("@")
        published = parse_old_time(str(row.get("Reply Date") or ""))
        text = str(row.get("Reply Content") or "")
        favorite = to_int(row.get("Reply Favorite Count"))
        retweet = to_int(row.get("Reply Retweet Count"))
        reply_count = to_int(row.get("Reply Reply Count"))
    else:
        name = str(row.get("Display Name") or "")
        screen = str(row.get("User Name") or "").lstrip("@")
        published = parse_old_time(str(row.get("Tweet Date") or ""))
        text = str(row.get("Tweet Content") or "")
        favorite = to_int(row.get("Favorite Count"))
        retweet = to_int(row.get("Retweet Count"))
        reply_count = to_int(row.get("Reply Count"))
    post_kind = "retweet" if is_retweet else ("reply" if is_reply else "tweet")
    post = PostRecord(
        tweet_id=tweet_id,
        user_id=owner_id,
        author_name=name,
        author_screen_name=screen,
        kind=post_kind,
        text=text,
        published_at=published,
        favorite_count=favorite,
        retweet_count=retweet,
        reply_count=reply_count,
        is_retweet=is_retweet,
        retweet_of_author=screen if is_retweet else "",
        is_reply=is_reply,
        reply_to_id=parse_tweet_input(str(row.get("Parent Tweet URL") or "")) or "",
        reply_to_author=str(row.get("Parent Tweet URL") or ""),
        url=url,
    )
    if is_reply:
        parent = str(row.get("Parent Tweet URL") or "")
        match = re.search(r"(?:x|twitter)\.com/([^/\s]+)/status", parent, re.I)
        post.reply_to_author = match.group(1) if match else parent
    return post


def _write_group(
    db: Database,
    config: Config,
    stats: MigrateStats,
    group: dict,
    folder: Path,
    user,
    mode: str,
    dry_run: bool,
) -> None:
    post: PostRecord = group["post"]
    media: list[MediaRecord] = group["media"]
    if not post.tweet_id:
        return
    user_id = 0 if dry_run else int(user["id"])
    post.user_id = user_id
    dest_dir = config.data_dir / str(user["image_dir"]) if user["image_dir"] else config.images_dir / f"import_{user_id}"
    if dry_run:
        for item in media:
            source = group["source_files"].pop(0) if group["source_files"] else None
            if source and source.is_file() and source.stat().st_size > 0:
                stats.used_files.add(str(source.resolve()))
                item.file_name = sanitize_filename(source.name)
                item.local_size = source.stat().st_size
                item.status = "ok"
            elif item.download_url:
                item.status = "pending"
            else:
                item.status = "missing"
        stats.posts += 1
        stats.media_refs += len(media)
        stats.ok_media += sum(1 for item in media if item.status == "ok")
        stats.pending_media += sum(1 for item in media if item.status == "pending")
        stats.missing_media += sum(1 for item in media if item.status == "missing")
        return
    for item in media:
        source = group["source_files"].pop(0) if group["source_files"] else None
        if source:
            stats.used_files.add(str(source.resolve()))
        if source and source.is_file() and source.stat().st_size > 0:
            if mode == "reference":
                item.file_name = sanitize_filename(source.name)
                item.local_size = source.stat().st_size
                item.status = "ok"
                stats.bytes += item.local_size
            else:
                target, action = _copy_or_move(source, dest_dir, mode)
                if target:
                    item.file_name = target.name
                    item.local_size = target.stat().st_size if target.exists() else None
                    item.status = "ok"
                    stats.bytes += item.local_size or 0
                    if action == "copied":
                        stats.copied += 1
                    elif action == "moved":
                        stats.moved += 1
                else:
                    item.status = "pending" if item.download_url else "missing"
    with db.transaction():
        post_id = db.upsert_post(post)
        db.upsert_media(post_id, media)
    stats.posts += 1
    stats.media_refs += len(media)
    stats.ok_media += sum(1 for item in media if item.status == "ok")
    stats.pending_media += sum(1 for item in media if item.status == "pending")
    stats.missing_media += sum(1 for item in media if item.status == "missing")


def _migrate_main_folder(
    db: Database,
    config: Config,
    stats: MigrateStats,
    folder: Path,
    mode: str,
    dry_run: bool,
) -> None:
    csv_files = sorted(folder.glob("*.csv"))
    main_files = []
    for path in csv_files:
        header, rows = _csv_rows(path)
        kind = _csv_kind(header)
        if kind in ("main_media", "text"):
            main_files.append((path, kind, header, rows))
    if not main_files:
        return
    first_rows = []
    for _, kind, header, rows in main_files:
        if kind == "main_media" and rows:
            first_rows = rows
            break
    screen = folder.name
    name = folder.name
    if first_rows:
        screen = str(first_rows[0].get("User Name") or screen).lstrip("@")
        name = str(first_rows[0].get("Display Name") or name)
    info = {"screen_name": screen, "name": name}
    if dry_run:
        user = {"id": 0, "image_dir": ""}
    else:
        user = ensure_user(db, config, info)
        if mode == "reference":
            db.set_user_image_dir(int(user["id"]), str(folder.resolve()))
            user = db.get_user(int(user["id"]))
        stats.users += 1
    used_ids: set[str] = set()
    for path, kind, header, rows in main_files:
        groups: dict[str, dict] = {}
        for row in rows:
            tweet_url = normalize_url(str(row.get("Tweet URL") or ""))
            tweet_id = parse_tweet_input(tweet_url)
            if not tweet_id:
                stats.unmatched_rows.append(f"{path.name}: {tweet_url or '(no url)'}")
                continue
            text = str(row.get("Tweet Content") or "")
            key = tweet_id if kind == "main_media" else f"{tweet_id}:{hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()[:8]}"
            if kind == "text" and tweet_id in used_ids:
                key = f"legacy-{hashlib.sha1(key.encode()).hexdigest()[:20]}"
            group = groups.setdefault(key, _new_group(int(user["id"]), tweet_id, tweet_url))
            if group["post"] is None:
                filename = str(row.get("Saved Filename") or "")
                is_retweet = "-retweet" in filename.lower()
                group["post"] = _post_from_fields(
                    int(user["id"]), tweet_id, tweet_url, row, kind,
                    is_retweet=is_retweet,
                )
                if is_retweet:
                    group["post"].retweet_of_author = str(row.get("User Name") or "").lstrip("@")
            if kind == "main_media":
                media = _media_from_row(folder, row, kind, tweet_id, len(group["media"]))
                if media:
                    if all(item.media_key != media.media_key for item in group["media"]):
                        group["media"].append(media)
                        source = _find_file(folder, str(row.get("Saved Filename") or ""))
                        if source:
                            group["source_files"].append(source)
            used_ids.add(tweet_id)
        for group in groups.values():
            if group["post"] is not None:
                _write_group(db, config, stats, group, folder, user, mode, dry_run)


def _migrate_collection_folder(
    db: Database,
    config: Config,
    stats: MigrateStats,
    folder: Path,
    mode: str,
    dry_run: bool,
    kind: str,
    collection_kind: str,
) -> None:
    csv_files = sorted(folder.glob("*.csv"))
    files = []
    for path in csv_files:
        header, rows = _csv_rows(path)
        file_kind = _csv_kind(header)
        if kind == "reply" and file_kind == "reply":
            files.append((path, file_kind, header, rows))
        elif kind == "tag" and file_kind in ("tag_media", "text"):
            files.append((path, file_kind, header, rows))
    if not files:
        return
    collection_name = folder.name if kind == "tag" else f"回复：{folder.name}"
    if dry_run:
        collection = {"id": 0, "image_dir": ""}
    else:
        collection = ensure_collection(
            db, config, collection_name, collection_kind,
            source_path=f"{collection_kind}:{folder.name}",
        )
        if mode == "reference":
            db.set_user_image_dir(int(collection["id"]), str(folder.resolve()))
            collection = db.get_user(int(collection["id"]))
        stats.collections += 1
    reply_files = []
    if kind == "reply":
        for path in folder.iterdir():
            match = REPLY_FILE_RE.match(path.name)
            if match and path.is_file():
                reply_files.append((match, path))
    groups: dict[str, dict] = {}
    used_file_paths: set[Path] = set()
    for path, file_kind, header, rows in files:
        for row in rows:
            if file_kind == "reply":
                reply_url = normalize_url(str(row.get("Reply URL") or ""))
                tweet_id = parse_tweet_input(reply_url) or f"legacy-{hashlib.sha1(reply_url.encode()).hexdigest()[:20]}"
                parent_url = normalize_url(str(row.get("Parent Tweet URL") or ""))
                author = str(row.get("Replier User Name") or "").lstrip("@")
                date = str(row.get("Reply Date") or "")
                key = tweet_id
                group = groups.setdefault(key, _new_group(int(collection["id"]), tweet_id, reply_url))
                if group["post"] is None:
                    group["post"] = _post_from_fields(
                        int(collection["id"]), tweet_id, reply_url, row, "reply", is_reply=True
                    )
                    group["post"].reply_to_id = parse_tweet_input(parent_url) or ""
                    group["post"].reply_to_author = parent_url
                for match, file_path in reply_files:
                    if file_path in used_file_paths:
                        continue
                    if match.group("date") != date.replace(":", "-") or match.group("user").lstrip("@").lower() != author.lower():
                        continue
                    used_file_paths.add(file_path)
                    group["source_files"].append(file_path)
                    group["media"].append(MediaRecord(
                        media_key=str(file_path),
                        media_type="video" if match.group("ext").lower() == "mp4" else "photo",
                        file_name=sanitize_filename(file_path.name),
                        position=len(group["media"]),
                        status="ok",
                        local_size=file_path.stat().st_size,
                    ))
            else:
                tweet_url = normalize_url(str(row.get("Tweet URL") or ""))
                text = str(row.get("Tweet Content") or "")
                key = f"{tweet_url}:{hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()[:10]}"
                tweet_id = f"legacy-{hashlib.sha1(key.encode()).hexdigest()[:20]}"
                group = groups.setdefault(key, _new_group(int(collection["id"]), tweet_id, tweet_url))
                if group["post"] is None:
                    group["post"] = _post_from_fields(
                        int(collection["id"]), tweet_id, tweet_url, row, "tag"
                    )
                if file_kind == "tag_media":
                    media = _media_from_row(folder, row, "tag_media", tweet_id, len(group["media"]))
                    if media and all(item.media_key != media.media_key for item in group["media"]):
                        group["media"].append(media)
                        source = _find_file(folder, str(row.get("Saved Path") or ""))
                        if source:
                            group["source_files"].append(source)
    for group in groups.values():
        if group["post"] is not None:
            _write_group(db, config, stats, group, folder, collection, mode, dry_run)
    for match, file_path in reply_files:
        if file_path not in used_file_paths:
            stats.unmatched_files.append(str(file_path))


def _migrate_markdown_folder(
    db: Database,
    config: Config,
    stats: MigrateStats,
    folder: Path,
    mode: str,
    dry_run: bool,
) -> None:
    # Minimal fallback: recover posts when CSV files are missing.
    md_files = sorted(folder.glob("*.md"))
    if not md_files:
        return
    if dry_run:
        user = {"id": 0, "image_dir": ""}
    else:
        user = ensure_user(db, config, {"screen_name": folder.name, "name": folder.name})
        if mode == "reference":
            db.set_user_image_dir(int(user["id"]), str(folder.resolve()))
            user = db.get_user(int(user["id"]))
    post_re = re.compile(r"^(?P<name>.+?) (?P<handle>@[A-Za-z0-9_]+) · (?P<date>.+?) \[src\]\((?P<url>[^)]+)\)$")
    current = None
    text_lines: list[str] = []
    for path in md_files:
        for line in _read_text(path).splitlines():
            match = post_re.match(line.strip())
            if match:
                if current:
                    _write_markdown_post(db, config, stats, folder, user, current, text_lines, mode, dry_run)
                url = normalize_url(match.group("url"))
                current = {
                    "tweet_id": parse_tweet_input(url) or f"legacy-{hashlib.sha1(url.encode()).hexdigest()[:20]}",
                    "url": url,
                    "name": match.group("name"),
                    "screen": match.group("handle").lstrip("@"),
                    "date": match.group("date"),
                }
                text_lines = []
                continue
            if current is None:
                continue
            text_lines.append(line)
    if current:
        _write_markdown_post(db, config, stats, folder, user, current, text_lines, mode, dry_run)


def _write_markdown_post(db, config, stats, folder, user, current, lines, mode, dry_run) -> None:
    text_parts = []
    media = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("## "):
            continue
        if stripped.endswith("Likes, 0 Retweets, 0 Replies") or re.search(r"\d+ Likes, \d+ Retweets, \d+ Replies", stripped):
            continue
        if stripped.startswith("[![]("):
            match = re.search(r"\[!\[\]\(([^)]+)\)\]\(([^)]+)\)", stripped)
            if match:
                source = _find_file(folder, match.group(1))
                media.append(MediaRecord(
                    media_key=match.group(2),
                    media_type="photo",
                    url=normalize_url(match.group(2)),
                    file_name=sanitize_filename(source.name) if source else sanitize_filename(os.path.basename(match.group(1))),
                    position=len(media),
                    status="ok" if source else "pending",
                    local_size=source.stat().st_size if source else None,
                ))
                if source:
                    current.setdefault("source_files", []).append(source)
        elif stripped.startswith("<video src="):
            match = re.search(r'<video src="([^"]+)"', stripped)
            if match:
                source = _find_file(folder, match.group(1))
                media.append(MediaRecord(
                    media_key=match.group(1),
                    media_type="video",
                    file_name=sanitize_filename(source.name) if source else sanitize_filename(os.path.basename(match.group(1))),
                    position=len(media),
                    status="ok" if source else "missing",
                    local_size=source.stat().st_size if source else None,
                ))
                if source:
                    current.setdefault("source_files", []).append(source)
        else:
            text_parts.append(stripped)
    post = PostRecord(
        tweet_id=current["tweet_id"],
        user_id=int(user["id"]),
        author_name=current["name"],
        author_screen_name=current["screen"],
        kind="tweet",
        text="\n".join(text_parts),
        published_at=parse_old_time(current["date"]),
        url=current["url"],
    )
    group = {
        "post": post,
        "media": media,
        "source_files": current.get("source_files", []),
        "tweet_id": post.tweet_id,
    }
    _write_group(db, config, stats, group, folder, user, mode, dry_run)


def _migrate_profile(
    db: Database,
    config: Config,
    stats: MigrateStats,
    profile_dir: Path,
    mode: str,
    dry_run: bool,
) -> None:
    if not profile_dir.is_dir():
        return
    if dry_run:
        stats.profiles += len(list(profile_dir.glob("*_avatar.*")))
        return
    for avatar in profile_dir.glob("*_avatar.*"):
        screen = avatar.stem.rsplit("_avatar", 1)[0]
        if not screen:
            continue
        user = db.get_user_by_screen(screen)
        if user is None:
            user_id = db.upsert_user(screen_name=screen, name=screen, enabled=False)
            user = db.get_user(user_id)
        user_id = int(user["id"])
        try:
            if not dry_run and (not user["avatar_file"]):
                with Image.open(avatar) as image:
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    image = ImageOps.fit(image, (160, 160), Image.Resampling.LANCZOS)
                    name = f"{user_id}_{hashlib.sha1(str(avatar).encode()).hexdigest()[:8]}.webp"
                    image.save(config.avatars_dir / name, "WEBP", quality=88, method=4)
                    db.set_user_avatar(user_id, name, str(avatar), int(time.time()))
            stats.profiles += 1
        except Exception as exc:
            stats.errors.append(f"profile avatar {avatar.name}: {exc}")
    for banner in profile_dir.glob("*_banner.*"):
        screen = banner.stem.rsplit("_banner", 1)[0]
        user = db.get_user_by_screen(screen)
        if not user:
            continue
        try:
            if not dry_run and not user["banner_file"]:
                with Image.open(banner) as image:
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    if image.width > 1500:
                        image = image.resize((1500, int(image.height * 1500 / image.width)), Image.Resampling.LANCZOS)
                    name = f"{int(user['id'])}_{hashlib.sha1(str(banner).encode()).hexdigest()[:8]}.webp"
                    image.save(config.banners_dir / name, "WEBP", quality=84, method=4)
                    db.set_user_banner(int(user["id"]), name)
        except Exception as exc:
            stats.errors.append(f"profile banner {banner.name}: {exc}")
    for desc in profile_dir.glob("*_description.txt"):
        screen = desc.stem.rsplit("_description", 1)[0]
        user = db.get_user_by_screen(screen)
        if not user:
            continue
        bio = _read_text(desc).strip()
        if bio and not dry_run:
            db.set_user_profile(int(user["id"]), bio=bio)


def migrate_twitter_download(
    db: Database,
    config: Config,
    src: str | Path,
    mode: str = "copy",
    include: set[str] | None = None,
    dry_run: bool = False,
    limit: int = 0,
) -> MigrateStats:
    source = Path(src).resolve()
    if not source.is_dir():
        raise SystemExit(f"迁移目录不存在：{source}")
    if source.name.lower() == "downloads":
        # The old script may keep profile data next to downloads.
        pass
    include = include or {"main", "text", "tags", "replies", "profile"}
    stats = MigrateStats()
    folders = [entry for entry in sorted(source.iterdir(), key=lambda p: p.name.lower()) if entry.is_dir()]
    if limit:
        folders = folders[:limit]
    logger.info("开始迁移 %d 个目录：%s（模式：%s）", len(folders), source, mode)
    for folder in folders:
        if folder.name.startswith((".", "__")) or folder.name.lower() == "profile":
            continue
        try:
            csv_files = list(folder.glob("*.csv"))
            kinds = set()
            for path in csv_files:
                header, _ = _csv_rows(path)
                kind = _csv_kind(header)
                if kind:
                    kinds.add(kind)
            if "main_media" in kinds and "main" in include:
                _migrate_main_folder(db, config, stats, folder, mode, dry_run)
            elif "text" in kinds and "tag_media" not in kinds and "main" in include and not _looks_like_tag_folder(folder):
                _migrate_main_folder(db, config, stats, folder, mode, dry_run)
            elif "tag_media" in kinds and "tags" in include:
                _migrate_collection_folder(db, config, stats, folder, mode, dry_run, "tag", "tag")
            elif "text" in kinds and _looks_like_tag_folder(folder) and "tags" in include:
                _migrate_collection_folder(db, config, stats, folder, mode, dry_run, "tag", "tag")
            elif "reply" in kinds and "replies" in include:
                _migrate_collection_folder(db, config, stats, folder, mode, dry_run, "reply", "reply")
            elif "main" in include:
                _migrate_markdown_folder(db, config, stats, folder, mode, dry_run)
            for path in folder.iterdir():
                if path.is_file() and path.suffix.lower() in MEDIA_EXTS and str(path.resolve()) not in stats.used_files:
                    stats.unmatched_files.append(str(path))
        except Exception as exc:
            stats.errors.append(f"{folder.name}: {exc}")
            logger.exception("迁移目录失败 %s：%s", folder, exc)
    if "profile" in include:
        for profile_dir in (source / "profile", source.parent / "profile"):
            if profile_dir.is_dir():
                _migrate_profile(db, config, stats, profile_dir, mode, dry_run)
                break
    stats.unmatched_files = list(dict.fromkeys(stats.unmatched_files))
    stats.unmatched_rows = list(dict.fromkeys(stats.unmatched_rows))
    report_dir = config.imports_dir / time.strftime("%Y%m%d-%H%M%S")
    if not dry_run:
        report_dir.mkdir(parents=True, exist_ok=True)
        report_stats = {key: value for key, value in stats.__dict__.items() if key != "used_files"}
        atomic_write_json(report_dir / "report.json", {
            "source": str(source),
            "mode": mode,
            "dry_run": dry_run,
            "stats": report_stats,
        })
    logger.info(
        "迁移%s：用户 %d，集合 %d，推文 %d，媒体引用 %d（本地 %d，待下载 %d，缺失 %d），复制 %d，移动 %d，字节 %d，错误 %d",
        "预演" if dry_run else "完成", stats.users, stats.collections, stats.posts,
        stats.media_refs, stats.ok_media, stats.pending_media, stats.missing_media,
        stats.copied, stats.moved, stats.bytes, len(stats.errors),
    )
    if stats.unmatched_rows:
        logger.warning("未匹配的 CSV 行：%d（详见导入报告）", len(stats.unmatched_rows))
    if stats.unmatched_files:
        logger.warning("未匹配的媒体文件：%d（详见导入报告）", len(stats.unmatched_files))
    return stats
