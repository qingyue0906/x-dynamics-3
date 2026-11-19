from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from .config import Config
from .errors import XApiError, XAuthError, XRateLimitError, XStaleError, XTransactionError
from .extract import extract_user
from .gql import (
    FIELD_TOGGLES,
    GQL_FEATURES,
    TWEET_FEATURES,
    USER_FEATURES,
    QueryRegistry,
)
from .http import HttpClient
from .transaction import TransactionProvider
from .utils import compact_json

logger = logging.getLogger(__name__)

BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
AUTH_CODES = {32, 64, 89, 239}
RATE_CODES = {88, 429}


class XClient:
    def __init__(self, config: Config) -> None:
        config.require_cookie()
        self.config = config
        self.registry = QueryRegistry(config.query_cache_path)
        headers = {
            "Authorization": f"Bearer {BEARER}",
            "X-Csrf-Token": config.ct0,
            "X-Twitter-Auth-Type": "OAuth2Session",
            "X-Twitter-Active-User": "yes",
            "X-Twitter-Client-Language": "zh-CN",
            "Referer": "https://x.com/",
            "Origin": "https://x.com",
        }
        self.http = HttpClient(config, headers=headers, cookies=config.cookie_map)
        self.transaction = TransactionProvider(config, self.http)

    async def __aenter__(self) -> "XClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        await self.transaction.close()
        await self.http.close()

    async def _fetch_text(self, url: str) -> tuple[int, str]:
        if "/i/embed" in url or "abs.twimg.com" in url:
            response = await self.transaction.public_get(url)
        else:
            response = await self.http.get(url)
        return response.status_code, response.text

    async def refresh_ids(self) -> dict[str, str]:
        logger.info("正在从 X 前端脚本刷新 GraphQL queryId...")
        changed = await self.registry.refresh(self._fetch_text)
        await self.transaction.refresh()
        return changed

    async def auth_check(self) -> tuple[bool, str]:
        headers = {
            "X-Csrf-Token": self.config.ct0,
            "X-Twitter-Auth-Type": "OAuth2Session",
            "X-Twitter-Active-User": "yes",
            "Referer": "https://x.com/",
        }
        for url in (
            "https://api.x.com/1.1/account/verify_credentials.json?skip_status=true&include_email=false",
            "https://x.com/i/api/1.1/account/settings.json",
        ):
            try:
                response = await self.http.get(url, headers=headers)
            except Exception as exc:
                logger.debug("Cookie 校验请求失败 %s：%s", url, exc)
                continue
            if response.status_code in (401, 403):
                logger.debug("Cookie 校验接口 %s 返回 HTTP %s", url, response.status_code)
                continue
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception:
                    data = {}
                screen = str(data.get("screen_name") or "")
                return True, f"登录有效：@{screen}" if screen else "登录有效"
            logger.debug("Cookie 校验接口 %s 返回 HTTP %s", url, response.status_code)
        try:
            payload = await self.graphql(
                "UserByScreenName",
                {"screen_name": "x", "withGrokTranslatedBio": False},
                features=USER_FEATURES,
                field_toggles=FIELD_TOGGLES["user"],
            )
            if extract_user(payload):
                return True, "GraphQL 可访问（登录状态未单独确认）"
        except Exception as exc:
            logger.debug("GraphQL Cookie 校验失败：%s", exc)
        return False, "无法确认登录状态（接口返回异常，可能仍可同步，请查看任务日志）"

    async def graphql(
        self,
        operation: str,
        variables: dict,
        features: dict | None = None,
        field_toggles: dict | None = None,
        max_attempts: int = 4,
    ) -> dict:
        features_obj = dict(features or GQL_FEATURES)
        toggles = dict(field_toggles or {})
        refreshed = False
        feature_retry = False
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            try:
                query_id = self.registry.get(operation)
                path = f"/i/api/graphql/{query_id}/{operation}"
                transaction_id = await self.transaction.generate("GET", path)
                params = {
                    "variables": compact_json(variables),
                    "features": compact_json(features_obj),
                }
                if toggles:
                    params["fieldToggles"] = compact_json(toggles)
                response = await self.http.get(
                    f"https://x.com{path}",
                    params=params,
                    headers={
                        "X-Client-Transaction-Id": transaction_id,
                        "Referer": "https://x.com/",
                    },
                )
                result = await self._handle_response(
                    response, operation, features_obj, feature_retry
                )
                if isinstance(result, tuple) and result[0] == "features":
                    for name in result[1]:
                        features_obj[name] = True
                    feature_retry = True
                    continue
                return result
            except XStaleError as exc:
                last_error = exc
                if refreshed:
                    raise
                refreshed = True
                logger.warning("queryId/transaction 可能已失效，刷新后重试一次：%s", exc)
                try:
                    await self.registry.refresh(self._fetch_text)
                    await self.transaction.refresh()
                except Exception as refresh_exc:
                    logger.warning("刷新 queryId 失败：%s", refresh_exc)
                continue
            except XRateLimitError:
                raise
            except XAuthError:
                raise
            except XTransactionError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                if attempt >= max_attempts - 1:
                    break
                wait = min(30.0, 2.0 * (2 ** attempt))
                logger.warning("%s 请求网络异常：%s，%.1fs 后重试", operation, exc, wait)
                await asyncio.sleep(wait)
            except XApiError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= max_attempts - 1:
                    break
                wait = min(20.0, 1.5 * (2 ** attempt))
                logger.warning("%s 请求失败：%s，%.1fs 后重试", operation, exc, wait)
                await asyncio.sleep(wait)
        raise XApiError(f"{operation} 请求失败：{last_error}")

    async def _handle_response(
        self,
        response: httpx.Response,
        operation: str,
        features: dict,
        feature_retry: bool,
    ) -> Any:
        status = response.status_code
        text = response.text or ""
        if status == 200:
            try:
                payload = response.json()
            except Exception as exc:
                if "Rate limit exceeded" in text:
                    raise XRateLimitError("X 提示 Rate limit exceeded") from exc
                raise XStaleError(f"{operation} 返回了非 JSON 内容") from exc
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if errors:
                self._raise_for_errors(errors, operation)
            return payload
        if status == 400:
            missing = self._missing_features(text)
            if missing and not feature_retry:
                logger.warning("%s 缺少 features：%s，自动补齐后重试", operation, ", ".join(missing))
                return ("features", missing)
            raise XApiError(f"{operation} HTTP 400：{text[:300]}")
        if status == 404:
            raise XStaleError(f"{operation} HTTP 404（queryId 或 transaction 失效）")
        if status == 429:
            await self._wait_for_reset(response, operation)
            raise XRateLimitError(f"{operation} 触发限流")
        if status in (401, 403):
            raise XAuthError(f"{operation} HTTP {status}：Cookie 失效或无权限")
        if 500 <= status < 600:
            raise XApiError(f"{operation} HTTP {status}：{text[:200]}")
        if "Rate limit exceeded" in text:
            raise XRateLimitError("X 提示 Rate limit exceeded")
        raise XApiError(f"{operation} HTTP {status}：{text[:300]}")

    @staticmethod
    def _missing_features(text: str) -> list[str]:
        match = re.search(r"features?\s+cannot be null[：:]\s*([^\n\"}]+)", text, re.I)
        if not match:
            return []
        names = re.findall(r"[A-Za-z0-9_]+", match.group(1))
        return list(dict.fromkeys(names))

    def _raise_for_errors(self, errors: list, operation: str) -> None:
        messages = []
        stale = False
        for item in errors:
            if not isinstance(item, dict):
                continue
            code = int(item.get("code") or 0)
            message = str(item.get("message") or "")
            messages.append(f"{code}: {message}")
            if code in AUTH_CODES or "Could not authenticate" in message:
                raise XAuthError(f"{operation} 鉴权失败：{code} {message}")
            if code in RATE_CODES or "Rate limit" in message:
                raise XRateLimitError(f"{operation} 限流：{code} {message}")
            if re.search(r"(?:query|operation).*(?:not found|unknown|invalid|expired)", message, re.I):
                stale = True
        if stale:
            raise XStaleError(f"{operation} 返回疑似失效的 query/operation：{' | '.join(messages)[:400]}")
        raise XApiError(f"{operation} 返回错误：{' | '.join(messages)[:400]}")

    async def _wait_for_reset(self, response: httpx.Response, operation: str) -> None:
        try:
            remaining = int(response.headers.get("x-rate-limit-remaining", "-1"))
        except ValueError:
            remaining = -1
        try:
            reset = int(response.headers.get("x-rate-limit-reset", "0"))
        except ValueError:
            reset = 0
        wait = max(5, reset - int(time.time()) + 2) if reset else 60
        if remaining == 0 and reset:
            logger.warning("%s 限流剩余 0，等待 %d 秒", operation, wait)
        if wait > 180:
            raise XRateLimitError(
                f"{operation} 限流，预计还需等待 {wait} 秒。请稍后再试，或降低同步频率。"
            )
        await asyncio.sleep(min(wait, 180))
        if remaining == 0:
            raise XRateLimitError(f"{operation} 已触发限流，等待后仍建议稍后再试")

    async def fetch_user(self, screen_name: str) -> dict:
        payload = await self.graphql(
            "UserByScreenName",
            {"screen_name": screen_name, "withSafetyModeUserFields": True, "withGrokTranslatedBio": False},
            features=USER_FEATURES,
            field_toggles=FIELD_TOGGLES["user"],
        )
        user = extract_user(payload)
        if not user:
            raise XApiError(f"找不到用户 @{screen_name}，或账号受保护/已停用")
        return user

    async def fetch_user_by_id(self, rest_id: str) -> dict:
        payload = await self.graphql(
            "UserByRestId",
            {"userId": str(rest_id), "withSafetyModeUserFields": True},
            features=USER_FEATURES,
            field_toggles=FIELD_TOGGLES["user"],
        )
        user = extract_user(payload)
        if not user:
            raise XApiError(f"找不到用户 ID {rest_id}")
        return user

    async def fetch_page(self, rest_id: str, cursor: str = "", count: int | None = None) -> dict:
        count = int(count or self.config.sync.page_size)
        variables: dict[str, Any] = {
            "userId": str(rest_id),
            "count": count,
            "includePromotedContent": False,
            "withClientEventToken": False,
            "withBirdwatchNotes": False,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor
        return await self.graphql(
            "UserMedia",
            variables,
            features=GQL_FEATURES,
            field_toggles=FIELD_TOGGLES["timeline"],
        )

    async def fetch_tweet(self, tweet_id: str) -> dict:
        try:
            return await self.graphql(
                "TweetResultByRestId",
                {
                    "tweetId": str(tweet_id),
                    "includePromotedContent": False,
                    "withBirdwatchNotes": False,
                    "withVoice": True,
                    "withCommunity": True,
                },
                features=TWEET_FEATURES,
                field_toggles=FIELD_TOGGLES["tweet"],
            )
        except XStaleError:
            raise
        except XApiError as exc:
            logger.warning("TweetResultByRestId 失败，改用 TweetDetail：%s", exc)
            return await self.graphql(
                "TweetDetail",
                {
                    "focalTweetId": str(tweet_id),
                    "referrer": "tweet",
                    "with_rux_injections": True,
                    "includePromotedContent": False,
                    "withCommunity": True,
                    "withQuickPromoteEligibilityTweetFields": True,
                    "withBirdwatchNotes": True,
                    "withVoice": True,
                    "withV2Timeline": True,
                },
                features=TWEET_FEATURES,
                field_toggles=FIELD_TOGGLES["tweet"],
            )
