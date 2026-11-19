from __future__ import annotations

from xdyn.config import load_config
from xdyn.db import Database
from xdyn.models import MediaRecord, PostRecord


def test_parse_inputs():
    from xdyn.utils import parse_tweet_input, parse_user_input
    assert parse_tweet_input("https://x.com/alice/status/1234567890123456789?s=20") == "1234567890123456789"
    assert parse_tweet_input("https://twitter.com/i/web/status/1234567890123456789") == "1234567890123456789"
    assert parse_tweet_input("1234567890123456789") == "1234567890123456789"
    assert parse_user_input("@Alice_1") == ("screen_name", "Alice_1")
    assert parse_user_input("https://x.com/Alice_1") == ("screen_name", "Alice_1")
    assert parse_user_input("12345") == ("rest_id", "12345")
    assert parse_user_input("not valid!") is None


def test_settings_and_cookie_parse(tmp_path):
    from xdyn.settings import SettingsStore, parse_cookie_input
    cookies = parse_cookie_input("auth_token=abc; ct0=def; twid=u%3D1")
    assert cookies["auth_token"] == "abc"
    assert cookies["ct0"] == "def"
    store = SettingsStore(tmp_path / "data")
    assert not store.has_cookies()
    store.save_cookie_header("auth_token=abc; ct0=def;")
    assert store.has_cookies()
    masked = store.masked_cookies()
    assert masked["configured"] is True
    assert "auth_token" in masked and masked["auth_token"] is True


def test_db_user_post_media(tmp_path):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data")
    db = Database(config.db_path)
    user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
    assert db.get_user_by_screen("ALICE")["id"] == user_id
    user_id2 = db.upsert_user(rest_id="1", screen_name="alice", name="Alice Updated", bio="bio")
    assert user_id2 == user_id
    post = PostRecord(
        tweet_id="100",
        user_id=user_id,
        author_name="Alice",
        author_screen_name="alice",
        text="hello world",
        published_at=1700000000,
        url="https://x.com/alice/status/100",
        media=[MediaRecord(
            media_key="m1", media_type="photo", url="https://x/1.jpg",
            download_url="https://x/1.jpg", file_name="1.jpg", status="ok", local_size=10,
        )],
    )
    with db.transaction():
        post_id = db.upsert_post(post)
        ids = db.upsert_media(post_id, post.media)
    assert ids
    rows, more = db.list_posts_with_media(q="hello")
    assert len(rows) == 1 and not more
    assert rows[0]["media"][0]["ok"] is True
    media_rows, more = db.list_media_with_posts()
    assert len(media_rows) == 1 and not more
    assert db.count_posts(q="world") == 1
    assert db.count_posts(q="he") == 1
    assert db.stats()["posts"] == 1
    assert db.get_post_id(user_id, "100") == post_id
    db.close()
