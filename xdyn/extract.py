from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .models import MediaRecord, PostRecord, QuotedPost
from .utils import (
    as_dict,
    as_list,
    canonical_media_key,
    first,
    normalize_url,
    parse_created_at,
    strip_query,
    to_int,
)

logger = logging.getLogger(__name__)

TWEET_TYPES = {"Tweet", "TweetWithVisibilityResults"}
UNAVAILABLE_TYPES = {"TweetTombstone", "TweetUnavailable"}
ENTRY_TWEET_RE = re.compile(r"(?:^|-)tweet-(\d+)(?:-|$)")


def _merge_base(obj: dict) -> dict:
    legacy = obj.get("legacy")
    base = dict(legacy) if isinstance(legacy, dict) else {}
    for key, value in obj.items():
        if key == "legacy":
            continue
        if value is not None:
            base[key] = value
    return base


def _unwrap_tweet(obj: Any) -> dict | None:
    if not isinstance(obj, dict):
        return None
    if obj.get("__typename") in UNAVAILABLE_TYPES:
        return None
    if "tweet_results" in obj:
        return _unwrap_tweet(obj["tweet_results"].get("result"))
    if obj.get("__typename") == "TweetWithVisibilityResults":
        return _unwrap_tweet(obj.get("tweet"))
    if "result" in obj and not obj.get("rest_id") and isinstance(obj.get("result"), dict):
        return _unwrap_tweet(obj["result"])
    return obj


def _user_obj_from_tweet(obj: dict) -> dict:
    for path in (
        ("core", "user_results", "result"),
        ("author_results", "result"),
        ("user_results", "result"),
    ):
        node: Any = obj
        for key in path:
            node = as_dict(node).get(key)
        if isinstance(node, dict) and node.get("rest_id"):
            return node
    return {}


def normalize_user(obj: Any) -> dict:
    obj = as_dict(obj)
    if obj.get("__typename") == "UserUnavailable":
        return {}
    if "result" in obj and not obj.get("rest_id"):
        obj = as_dict(obj.get("result"))
    if not obj:
        return {}
    base = _merge_base(obj)
    core = as_dict(obj.get("core"))
    rest_id = str(first(obj.get("rest_id"), base.get("rest_id"), base.get("id_str"), "") or "")
    screen_name = str(first(core.get("screen_name"), base.get("screen_name"), "") or "")
    name = str(first(core.get("name"), base.get("name"), "") or "")
    avatar = ""
    avatar_obj = as_dict(obj.get("avatar"))
    if avatar_obj:
        avatar = str(avatar_obj.get("image_url") or "")
    if not avatar:
        avatar = str(base.get("profile_image_url_https") or "")
    avatar = re.sub(r"_(normal|bigger|200x200|400x400)(\.[A-Za-z0-9]+)$", r"_400x400\2", avatar)
    banner = str(base.get("profile_banner_url") or "")
    banner_obj = as_dict(obj.get("banner"))
    if not banner and banner_obj:
        banner = str(banner_obj.get("image_url") or "")
    profile_bio = as_dict(obj.get("profile_bio"))
    bio = str(first(base.get("description"), profile_bio.get("description"), "") or "")
    location = str(first(base.get("location"), as_dict(obj.get("location")).get("location"), "") or "")
    relationship = as_dict(obj.get("relationship_counts"))
    tweets = as_dict(obj.get("tweet_counts"))
    privacy = as_dict(obj.get("privacy"))
    verification = as_dict(obj.get("verification"))
    return {
        "rest_id": rest_id,
        "screen_name": screen_name,
        "name": name,
        "bio": bio,
        "location": location,
        "url": str(base.get("url") or obj.get("url") or ""),
        "avatar_url": avatar,
        "banner_url": banner,
        "protected": bool(first(privacy.get("protected"), base.get("protected"), False)),
        "verified": bool(first(verification.get("verified"), base.get("verified"), False)),
        "followers_count": to_int(first(relationship.get("followers"), base.get("followers_count"))),
        "following_count": to_int(first(relationship.get("following"), base.get("friends_count"))),
        "tweet_count": to_int(first(tweets.get("tweets"), base.get("statuses_count"))),
        "media_count": to_int(first(tweets.get("media_tweets"), base.get("media_count"))),
        "created_at": parse_created_at(first(core.get("created_at"), base.get("created_at"))),
    }


def _best_video_url(media: dict) -> str:
    info = as_dict(media.get("video_info"))
    variants = as_list(info.get("variants"))
    best_url = ""
    best_bitrate = -1
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        url = str(variant.get("url") or "")
        if not url:
            continue
        content_type = str(variant.get("content_type") or "")
        bitrate = to_int(variant.get("bitrate"), -1)
        if "mp4" not in content_type.lower() and not url.lower().split("?", 1)[0].endswith(".mp4"):
            continue
        if bitrate > best_bitrate:
            best_bitrate = bitrate
            best_url = url
    if best_url:
        return best_url
    for variant in variants:
        if isinstance(variant, dict) and variant.get("url"):
            return str(variant["url"])
    return ""


def _media_records(tweet_id: str, media: list, image_format: str = "orig") -> list[MediaRecord]:
    result: list[MediaRecord] = []
    for index, raw in enumerate(media):
        if not isinstance(raw, dict):
            continue
        media_type = str(raw.get("type") or "photo")
        url = normalize_url(str(raw.get("media_url_https") or raw.get("media_url") or ""))
        if not url:
            continue
        original = as_dict(raw.get("original_info"))
        width = to_int(original.get("width"))
        height = to_int(original.get("height"))
        duration = to_int(as_dict(raw.get("video_info")).get("duration_millis"))
        alt = str(raw.get("ext_alt_text") or "")
        if media_type in ("video", "animated_gif"):
            download_url = _best_video_url(raw)
            poster_url = f"{url}?name=small" if "?" not in url else f"{strip_query(url)}?name=small"
            file_name = f"{tweet_id}_{index}.mp4"
        else:
            if image_format == "jpg":
                download_url = f"{strip_query(url)}?format=jpg&name=4096x4096"
                file_name = f"{tweet_id}_{index}.jpg"
            elif image_format == "png":
                download_url = f"{strip_query(url)}?format=png&name=4096x4096"
                file_name = f"{tweet_id}_{index}.png"
            else:
                download_url = f"{strip_query(url)}?name=orig"
                ext = ".png" if strip_query(url).lower().endswith(".png") else ".jpg"
                file_name = f"{tweet_id}_{index}{ext}"
            poster_url = ""
        media_key = canonical_media_key(url, download_url, media_type, file_name)
        result.append(
            MediaRecord(
                media_key=media_key,
                media_type=media_type,
                url=url,
                download_url=download_url,
                poster_url=poster_url,
                file_name=file_name,
                width=width,
                height=height,
                duration_ms=duration,
                alt_text=alt,
                position=index,
            )
        )
    return result


def _tweet_media(obj: dict) -> list:
    extended = as_dict(obj.get("extended_entities"))
    if extended:
        return as_list(extended.get("media"))
    entities = as_dict(obj.get("entities"))
    return as_list(entities.get("media"))


def _tweet_text(obj: dict) -> str:
    note = as_dict(obj.get("note_tweet"))
    note_result = as_dict(as_dict(note.get("note_tweet_results")).get("result"))
    if note_result.get("text"):
        return str(note_result["text"])
    return str(obj.get("full_text") or obj.get("text") or "")


def _ref_id(obj: dict, key: str) -> str:
    result = _unwrap_tweet(as_dict(obj.get(key)))
    if result:
        return str(result.get("rest_id") or as_dict(result.get("legacy")).get("id_str") or "")
    return ""


def _normalize_tweet(obj: Any, owner_screen_name: str = "", image_format: str = "orig") -> PostRecord | None:
    obj = _unwrap_tweet(obj)
    if not obj:
        return None
    base = _merge_base(obj)
    tweet_id = str(first(obj.get("rest_id"), base.get("rest_id"), base.get("id_str"), "") or "")
    if not tweet_id.isdigit():
        return None
    author_obj = _user_obj_from_tweet(obj)
    author = normalize_user(author_obj) if author_obj else {}
    if not author and owner_screen_name:
        author = {"screen_name": owner_screen_name, "name": owner_screen_name, "rest_id": ""}
    published_at = parse_created_at(first(base.get("created_at"), base.get("tweet_created_at")))
    text = _tweet_text(base)
    lang = str(base.get("lang") or "")
    counts = {
        "favorite_count": to_int(base.get("favorite_count")),
        "retweet_count": to_int(base.get("retweet_count")),
        "reply_count": to_int(base.get("reply_count")),
        "quote_count": to_int(base.get("quote_count")),
        "bookmark_count": to_int(base.get("bookmark_count")),
    }
    views = as_dict(base.get("views")).get("count")
    if views is None:
        views = as_dict(base.get("ext_views")).get("count")
    view_count = to_int(views)
    possibly_sensitive = bool(base.get("possibly_sensitive"))

    retweet_inner = _unwrap_tweet(as_dict(base.get("retweeted_status_result")).get("result"))
    quoted_inner = _unwrap_tweet(as_dict(base.get("quoted_status_result")).get("result"))
    quoted = None
    if quoted_inner:
        quoted_user = normalize_user(_user_obj_from_tweet(quoted_inner))
        quoted_base = _merge_base(quoted_inner)
        quoted_id = str(first(quoted_inner.get("rest_id"), quoted_base.get("id_str"), "") or "")
        quoted = QuotedPost(
            tweet_id=quoted_id,
            author_name=str(quoted_user.get("name") or ""),
            author_screen_name=str(quoted_user.get("screen_name") or ""),
            text=_tweet_text(quoted_base),
            url=f"https://x.com/{quoted_user.get('screen_name') or 'i'}/status/{quoted_id}" if quoted_id else "",
        )

    if retweet_inner:
        inner = _normalize_tweet(retweet_inner, owner_screen_name=owner_screen_name, image_format=image_format)
        if inner:
            original_id = inner.tweet_id
            original_author = inner.author_screen_name
            inner.tweet_id = tweet_id
            inner.kind = "retweet"
            inner.is_retweet = True
            inner.retweet_of_id = original_id
            inner.retweet_of_author = original_author
            inner.published_at = published_at or inner.published_at
            inner.url = inner.url or f"https://x.com/i/status/{original_id}"
            inner.author_avatar = inner.author_avatar or str(author.get("avatar_url") or "")
            return inner

    media = _media_records(tweet_id, _tweet_media(base), image_format)
    kind = "tweet"
    reply_id = str(first(base.get("in_reply_to_status_id_str"), base.get("in_reply_to_status_id"), "") or "")
    if quoted:
        kind = "quote"
    elif reply_id:
        kind = "reply"
    if retweet_inner:
        kind = "retweet"
    url = f"https://x.com/{author.get('screen_name') or 'i'}/status/{tweet_id}"
    return PostRecord(
        tweet_id=tweet_id,
        author_id=str(author.get("rest_id") or ""),
        author_name=str(author.get("name") or ""),
        author_screen_name=str(author.get("screen_name") or ""),
        kind=kind,
        text=text,
        lang=lang,
        published_at=published_at,
        favorite_count=counts["favorite_count"],
        retweet_count=counts["retweet_count"],
        reply_count=counts["reply_count"],
        quote_count=counts["quote_count"],
        view_count=view_count,
        bookmark_count=counts["bookmark_count"],
        is_retweet=bool(retweet_inner),
        retweet_of_id=_ref_id(base, "retweeted_status_result") if retweet_inner else "",
        retweet_of_author="",
        is_quote=bool(quoted),
        quoted=quoted,
        is_reply=bool(reply_id),
        reply_to_id=reply_id,
        reply_to_author=str(base.get("in_reply_to_screen_name") or ""),
        possibly_sensitive=possibly_sensitive,
        source=str(base.get("source") or ""),
        url=url,
        media=media,
        author_avatar=str(author.get("avatar_url") or ""),
    )


def extract_post(obj: Any, owner_screen_name: str = "", image_format: str = "orig") -> PostRecord | None:
    try:
        return _normalize_tweet(obj, owner_screen_name=owner_screen_name, image_format=image_format)
    except Exception:
        logger.warning("解析推文失败", exc_info=True)
        return None


@dataclass
class TimelinePage:
    posts: list[PostRecord] = field(default_factory=list)
    cursor: str = ""
    terminated: bool = False
    content_entry_count: int = 0
    module_item_count: int = 0

    @property
    def has_content(self) -> bool:
        return bool(self.posts or self.content_entry_count or self.module_item_count)

    @property
    def cursor_only(self) -> bool:
        """True for X's terminal page: cursors exist but no timeline content."""
        return bool(self.cursor) and not self.has_content


def _walk(node: Any, tweets: list[dict], entry_ids: list[str], state: dict) -> None:
    if isinstance(node, dict):
        typename = node.get("__typename")
        if typename in TWEET_TYPES and ("rest_id" in node or "legacy" in node):
            tweets.append(node)
        entry_id = node.get("entryId")
        if isinstance(entry_id, str):
            entry_ids.append(entry_id)
        if (
            str(node.get("type") or "") == "TimelineTerminateTimeline"
            and str(node.get("direction") or "").lower() == "bottom"
        ):
            state["terminated"] = True
        module_items = node.get("moduleItems")
        if isinstance(module_items, list):
            state["module_item_count"] += len(module_items)
        for value in node.values():
            _walk(value, tweets, entry_ids, state)
    elif isinstance(node, list):
        for value in node:
            _walk(value, tweets, entry_ids, state)


def extract_timeline_page(
    payload: Any,
    owner_screen_name: str = "",
    image_format: str = "orig",
) -> TimelinePage:
    tweets: list[dict] = []
    entry_ids: list[str] = []
    state = {"terminated": False, "module_item_count": 0}
    _walk(payload, tweets, entry_ids, state)
    standalone = set()
    for entry_id in entry_ids:
        match = ENTRY_TWEET_RE.search(entry_id)
        if match:
            standalone.add(match.group(1))
    referenced: set[str] = set()
    for raw in tweets:
        base = _merge_base(_unwrap_tweet(raw) or {})
        for key in ("retweeted_status_result", "quoted_status_result"):
            ref = _ref_id(base, key)
            if ref:
                referenced.add(ref)
    posts: list[PostRecord] = []
    seen: set[str] = set()
    for raw in tweets:
        normalized = extract_post(raw, owner_screen_name=owner_screen_name, image_format=image_format)
        if not normalized or normalized.tweet_id in seen:
            continue
        if normalized.tweet_id in referenced and normalized.tweet_id not in standalone:
            continue
        if normalized.tweet_id in seen:
            continue
        seen.add(normalized.tweet_id)
        posts.append(normalized)

    cursor = ""
    def find_cursor(node: Any) -> None:
        nonlocal cursor
        if isinstance(node, dict):
            entry_id = str(node.get("entryId") or "")
            cursor_type = str(node.get("cursorType") or as_dict(node.get("content")).get("cursorType") or "")
            if "cursor-bottom" in entry_id or cursor_type.lower() == "bottom":
                value = first(node.get("value"), as_dict(node.get("content")).get("value"))
                if value:
                    cursor = str(value)
            for value in node.values():
                find_cursor(value)
        elif isinstance(node, list):
            for value in node:
                find_cursor(value)
    find_cursor(payload)
    content_entry_count = sum(
        1 for entry_id in entry_ids if not entry_id.lower().startswith("cursor")
    )
    return TimelinePage(
        posts=posts,
        cursor=cursor,
        terminated=bool(state["terminated"]),
        content_entry_count=content_entry_count,
        module_item_count=int(state["module_item_count"]),
    )


def extract_timeline(
    payload: Any,
    owner_screen_name: str = "",
    image_format: str = "orig",
) -> tuple[list[PostRecord], str]:
    page = extract_timeline_page(
        payload,
        owner_screen_name=owner_screen_name,
        image_format=image_format,
    )
    return page.posts, page.cursor


def extract_tweet(payload: Any, tweet_id: str = "", image_format: str = "orig") -> PostRecord | None:
    posts, _ = extract_timeline(payload, image_format=image_format)
    if tweet_id:
        for post in posts:
            if post.tweet_id == str(tweet_id):
                return post
    return posts[0] if posts else None


def extract_user(payload: Any) -> dict:
    data = as_dict(as_dict(payload).get("data"))
    for key in ("user", "user_result_by_screen_name", "user_result_by_rest_id"):
        node = as_dict(data.get(key))
        result = node.get("result") if isinstance(node, dict) else None
        if isinstance(result, dict):
            user = normalize_user(result)
            if user:
                return user
    return {}
