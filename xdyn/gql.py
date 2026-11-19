from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from .utils import atomic_write_json

logger = logging.getLogger(__name__)

GQL_BASE = "https://x.com/i/api/graphql"

SEED_OPS = {
    "UserByScreenName": "AMIBMjtxEEATh4z8V9GtRg",
    "UserByRestId": "mxaYWwt52eAb6OAzYHMVzg",
    "UserMedia": "iTSlWbsmD00dAE7Sk9xvIw",
    "TweetDetail": "dQDdKtt882nl-Km98FJGSQ",
    "TweetResultByRestId": "LkId5Akr61BS6BmOIcffRg",
    "SearchTimeline": "BYppgupFFS2yqVmmRNjlsw",
    "Viewer": "",
}

GQL_FEATURES = {
    "articles_preview_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": False,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_profile_redirect_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_awards_web_tipping_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "tweet_with_visibility_results_prefer_gql_media_interstitial_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "responsive_web_grok_share_attachment_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_image_annotation_enabled": False,
    "responsive_web_grok_analysis_button_from_backend": False,
    "responsive_web_jetfuel_frame": False,
    "rweb_video_screen_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
}

USER_FEATURES = {
    **GQL_FEATURES,
    "highlights_tweets_tab_ui_enabled": True,
    "hidden_profile_likes_enabled": True,
    "hidden_profile_subscriptions_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "subscriptions_verification_info_is_identity_verified_enabled": False,
    "responsive_web_twitter_article_notes_tab_enabled": False,
    "subscriptions_feature_can_gift_premium": False,
}

TWEET_FEATURES = {
    **GQL_FEATURES,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
}

FIELD_TOGGLES = {
    "timeline": {"withArticlePlainText": False},
    "tweet": {
        "withArticleRichContentState": True,
        "withArticlePlainText": False,
        "withArticleSummaryText": True,
        "withArticleVoiceOver": True,
        "withGrokAnalyze": False,
        "withDisallowedReplyControls": False,
    },
    "user": {"withAuxiliaryUserLabels": True, "withPayments": False},
}

SCRIPT_RE = re.compile(r"https://[\w.-]+/(?:x-web|responsive-web/client-web)/[\w./-]+\.js")
LEGACY_MAIN_RE = re.compile(r"/client-web/main\.([^.\"']+)\.js")
HASH_MAP_RE = re.compile(r'(\d+):"([0-9a-f]{7}|[0-9a-f]{16})"')
NAME_MAP_RE = re.compile(r'(\d+):"([^"]+)"')
OP_PATTERNS = (
    re.compile(r'queryId:[`"](.+?)[`"].{0,400}?operationName:[`"](.+?)[`"]', re.S),
    re.compile(r'operationName:[`"](.+?)[`"].{0,400}?queryId:[`"](.+?)[`"]', re.S),
    re.compile(r'params:\{id:[`"]([^`"]+)[`"].{0,400}?name:[`"]([^`"]+)[`"].{0,200}?operationKind:[`"]', re.S),
)
SKIP_SCRIPT_PARTS = ("/i18n/", "/icons/", "react-syntax-highlighter")


def extract_script_urls(text: str) -> list[str]:
    urls = SCRIPT_RE.findall(text or "")
    if main_match := LEGACY_MAIN_RE.search(text or ""):
        urls.append(f"https://abs.twimg.com/responsive-web/client-web/main.{main_match.group(1)}.js")
    hash_map = {m.group(1): m.group(2) for m in HASH_MAP_RE.finditer(text or "")}
    name_map: dict[str, str] = {}
    for match in NAME_MAP_RE.finditer(text or ""):
        value = match.group(2)
        if not re.fullmatch(r"[0-9a-f]{7}|[0-9a-f]{16}", value):
            name_map[match.group(1)] = value
    for chunk_id, hash_value in hash_map.items():
        name = name_map.get(chunk_id, chunk_id)
        urls.append(
            f"https://abs.twimg.com/responsive-web/client-web/{name}.{hash_value}a.js"
        )
    return list(dict.fromkeys(urls))


def parse_ops_from_js(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, pattern in enumerate(OP_PATTERNS):
        for left, right in pattern.findall(text or ""):
            if not left or not right:
                continue
            if index == 1:
                name, op_id = left, right
            else:
                op_id, name = left, right
            if name in ("queryId", "operationName"):
                continue
            if not re.fullmatch(r"[\w-]{8,64}", op_id or ""):
                continue
            result.setdefault(name, op_id)
    return result


class QueryRegistry:
    """GraphQL operation ids with a cached, self-refreshing map."""

    def __init__(self, cache_path: Path) -> None:
        self.cache_path = Path(cache_path)
        self.ops: dict[str, str] = dict(SEED_OPS)
        self.updated_at = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        ops = data.get("ops")
        if isinstance(ops, dict):
            for key, value in ops.items():
                if isinstance(value, str) and value:
                    self.ops[str(key)] = value
        self.updated_at = int(data.get("updated_at") or 0)

    def get(self, name: str) -> str:
        op_id = self.ops.get(name) or ""
        if not op_id:
            raise RuntimeError(f"缺少 GraphQL operation ID：{name}，请运行 refresh-ids")
        return op_id

    def set(self, name: str, op_id: str) -> None:
        if op_id:
            self.ops[name] = op_id

    def save(self) -> None:
        self.updated_at = int(time.time())
        atomic_write_json(
            self.cache_path,
            {"updated_at": self.updated_at, "ops": self.ops},
        )

    def snapshot(self) -> dict:
        return {
            "updated_at": self.updated_at,
            "ops": {key: value for key, value in self.ops.items() if value},
        }

    async def refresh(self, fetch_text) -> dict[str, str]:
        """fetch_text(url) -> (status_code, text)."""
        pages = (
            "https://x.com/xdevelopers",
            "https://x.com/home",
            "https://x.com/i/embed",
        )
        urls: list[str] = []
        for page in pages:
            try:
                status, text = await fetch_text(page)
                if status == 200 and text:
                    urls.extend(extract_script_urls(text))
            except Exception as exc:
                logger.debug("拉取 X 页面失败 %s：%s", page, exc)
        urls = [url for url in dict.fromkeys(urls) if not any(part in url for part in SKIP_SCRIPT_PARTS)]
        if not urls:
            logger.warning("没有从 X 页面找到脚本，保留现有 queryId")
            return {}

        found: dict[str, tuple[str, int]] = {}
        semaphore = asyncio.Semaphore(10)

        def priority(url: str) -> int:
            if "/responsive-web/client-web/" in url:
                return 2
            if "/x-web/" in url:
                return 1
            return 0

        async def scan(url: str) -> None:
            async with semaphore:
                try:
                    status, text = await fetch_text(url)
                except Exception:
                    return
                if status != 200 or not text:
                    return
                for name, op_id in parse_ops_from_js(text).items():
                    old = found.get(name)
                    if old is None or priority(url) >= old[1]:
                        found[name] = (op_id, priority(url))

        await asyncio.gather(*(scan(url) for url in urls[:240]), return_exceptions=True)
        changed = {}
        for name, (op_id, _) in found.items():
            if self.ops.get(name) != op_id:
                changed[name] = op_id
            self.ops[name] = op_id
        if found:
            self.save()
            logger.info("GraphQL queryId 已刷新：%d 个 operation，变化 %d 个", len(found), len(changed))
        else:
            logger.warning("没有从脚本中解析到 queryId，保留现有值")
        return changed
