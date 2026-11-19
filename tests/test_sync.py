from __future__ import annotations

import asyncio

import httpx
import pytest

from xdyn.config import load_config
from xdyn.db import Database
from xdyn.sync import sync_user


def tweet(rest_id, text="hello", created="Mon Jan 01 12:00:00 +0000 2024"):
    return {
        "__typename": "Tweet",
        "rest_id": str(rest_id),
        "legacy": {
            "id_str": str(rest_id),
            "full_text": text,
            "created_at": created,
            "favorite_count": 0,
            "retweet_count": 0,
            "reply_count": 0,
            "conversation_id_str": str(rest_id),
            "extended_entities": {"media": [{
                "id_str": f"m{rest_id}",
                "type": "photo",
                "media_url_https": f"https://pbs.twimg.com/media/{rest_id}.jpg",
                "original_info": {"width": 10, "height": 10},
            }]},
        },
        "core": {"user_results": {"result": {
            "__typename": "User", "rest_id": "1",
            "legacy": {"screen_name": "alice", "name": "Alice", "profile_image_url_https": ""},
        }}},
    }


def page(ids, cursor=""):
    entries = [
        {"entryId": f"tweet-{rest_id}", "content": {"itemContent": {"tweet_results": {"result": tweet(rest_id)}}}}
        for rest_id in ids
    ]
    if cursor:
        entries.append({"entryId": "cursor-bottom-1", "content": {"cursorType": "Bottom", "value": cursor}})
    return {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": [
        {"type": "TimelineAddEntries", "entries": entries}
    ]}}}}}}


class FakeClient:
    def __init__(self, pages, fail_cursors=None):
        self.pages = pages
        self.calls = []
        self.fail_cursors = set(fail_cursors or ())

    async def fetch_page(self, rest_id, cursor="", count=None):
        self.calls.append(cursor)
        if cursor in self.fail_cursors:
            self.fail_cursors.remove(cursor)
            raise httpx.ReadError("")
        return self.pages[cursor]


def test_incremental_stops_when_page_is_old(tmp_path):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data", sleep=0)
    db = Database(config.db_path)
    user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
    db.set_last_tweet_id(user_id, "100")
    client = FakeClient({"": page([102, 101], "p2"), "p2": page([99, 98], "p3"), "p3": page([97])})
    user = db.get_user(user_id)
    try:
        result = asyncio.run(sync_user(db, config, client, None, user, no_download=True, max_pages=10))
        assert result.pages == 2
        assert client.calls == ["", "p2"]
        assert result.new_posts == 2
        assert db.get_user(user_id)["last_tweet_id"] == "102"
    finally:
        db.close()


def test_full_mode_keeps_old_watermark_when_limited(tmp_path):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data", sleep=0)
    db = Database(config.db_path)
    user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
    db.set_last_tweet_id(user_id, "100")
    client = FakeClient({"": page([102], "p2"), "p2": page([101])})
    user = db.get_user(user_id)
    try:
        asyncio.run(sync_user(db, config, client, None, user, full=True, no_download=True, max_pages=1))
        assert db.get_user(user_id)["last_tweet_id"] == "100"
        pending = db.get_user(user_id)
        assert pending["sync_active"] == 1
        assert pending["sync_cursor"] == "p2"
        assert pending["sync_target_id"] == "0"
        assert pending["sync_max_id"] == "102"
        client2 = FakeClient({"": page([102], "p2"), "p2": page([101])})
        asyncio.run(sync_user(db, config, client2, None, db.get_user(user_id), full=True, no_download=True))
        assert db.get_user(user_id)["last_tweet_id"] == "102"
        assert db.get_user(user_id)["sync_active"] == 0
    finally:
        db.close()


def test_sync_stops_after_consecutive_empty_pages(tmp_path):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data", sleep=0)
    db = Database(config.db_path)
    user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
    client = FakeClient({
        "": page([102, 101], "p2"),
        "p2": page([], "p3"),
        "p3": page([], "p4"),
        "p4": page([], "p5"),
        "p5": page([], "p6"),
    })
    try:
        result = asyncio.run(sync_user(db, config, client, None, db.get_user(user_id), no_download=True))
        assert result.pages == 4
        assert client.calls == ["", "p2", "p3", "p4"]
    finally:
        db.close()


def test_paused_sync_resumes_at_cursor(tmp_path):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data", sleep=0)
    db = Database(config.db_path)
    user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
    db.set_last_tweet_id(user_id, "100")
    first = FakeClient({"": page([102, 101], "p2"), "p2": page([99])})
    try:
        result = asyncio.run(sync_user(db, config, first, None, db.get_user(user_id), no_download=True, max_pages=1))
        assert result.pages == 1
        user = db.get_user(user_id)
        assert user["last_tweet_id"] == "100"
        assert user["sync_active"] == 1
        assert user["sync_cursor"] == "p2"
        assert user["sync_target_id"] == "100"
        assert user["sync_max_id"] == "102"

        second = FakeClient({"p2": page([99])})
        result = asyncio.run(sync_user(db, config, second, None, db.get_user(user_id), no_download=True))
        assert result.pages == 1
        assert second.calls == ["p2"]
        user = db.get_user(user_id)
        assert user["last_tweet_id"] == "102"
        assert user["sync_active"] == 0
    finally:
        db.close()


def test_initial_sync_failure_and_resume(tmp_path):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data", sleep=0)
    db = Database(config.db_path)
    user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
    first = FakeClient(
        {"": page([102, 101], "p2"), "p2": page([100, 99], "p3")},
        fail_cursors={"p2"},
    )
    try:
        with pytest.raises(httpx.ReadError):
            asyncio.run(sync_user(db, config, first, None, db.get_user(user_id), no_download=True))
        user = db.get_user(user_id)
        assert user["last_tweet_id"] == "0"
        assert user["sync_active"] == 1
        assert user["sync_cursor"] == "p2"
        assert user["sync_max_id"] == "102"

        second = FakeClient({
            "p2": page([100, 99], "p3"),
            "p3": page([98]),
        })
        result = asyncio.run(sync_user(db, config, second, None, db.get_user(user_id), no_download=True))
        assert result.status == "ok"
        assert second.calls == ["p2", "p3"]
        user = db.get_user(user_id)
        assert user["last_tweet_id"] == "102"
        assert user["sync_active"] == 0
        assert user["sync_cursor"] == ""
        assert user["sync_target_id"] == "0"
        assert user["sync_max_id"] == "0"
    finally:
        db.close()
