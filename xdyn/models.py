from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MediaRecord:
    media_key: str
    media_type: str = "photo"          # photo | video | animated_gif
    url: str = ""
    download_url: str = ""
    poster_url: str = ""
    file_name: str = ""
    width: int = 0
    height: int = 0
    duration_ms: int = 0
    alt_text: str = ""
    position: int = 0
    remote_size: int | None = None
    local_size: int | None = None
    status: str = "pending"
    http_status: int | None = None
    attempts: int = 0
    last_error: str = ""
    downloaded_at: int | None = None

    @property
    def is_video(self) -> bool:
        return self.media_type in ("video", "animated_gif")


@dataclass
class QuotedPost:
    tweet_id: str = ""
    author_name: str = ""
    author_screen_name: str = ""
    text: str = ""
    url: str = ""


@dataclass
class PostRecord:
    tweet_id: str
    user_id: int = 0
    author_id: str = ""
    author_name: str = ""
    author_screen_name: str = ""
    kind: str = "tweet"                # tweet | retweet | quote | reply
    text: str = ""
    lang: str = ""
    published_at: int = 0
    favorite_count: int = 0
    retweet_count: int = 0
    reply_count: int = 0
    quote_count: int = 0
    view_count: int = 0
    bookmark_count: int = 0
    is_retweet: bool = False
    retweet_of_id: str = ""
    retweet_of_author: str = ""
    is_quote: bool = False
    quoted: QuotedPost | None = None
    is_reply: bool = False
    reply_to_id: str = ""
    reply_to_author: str = ""
    possibly_sensitive: bool = False
    source: str = ""
    url: str = ""
    is_accessible: bool = True
    is_deleted: bool = False
    media: list[MediaRecord] = field(default_factory=list)
    author_avatar: str = ""

    @property
    def media_count(self) -> int:
        return len(self.media)

    @property
    def has_video(self) -> bool:
        return any(item.is_video for item in self.media)
