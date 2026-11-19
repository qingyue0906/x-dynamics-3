from __future__ import annotations

from xdyn.extract import extract_timeline, extract_timeline_page, extract_tweet, extract_user


def user(rest_id="1", screen="alice", name="Alice"):
    return {
        "__typename": "User",
        "rest_id": rest_id,
        "legacy": {
            "screen_name": screen,
            "name": name,
            "description": "bio",
            "profile_image_url_https": "https://pbs.twimg.com/a_normal.jpg",
            "followers_count": 10,
            "friends_count": 2,
            "statuses_count": 20,
            "media_count": 5,
        },
    }


def tweet(rest_id="100", text="hello", created="Mon Jan 01 12:00:00 +0000 2024", media=True):
    legacy = {
        "id_str": rest_id,
        "full_text": text,
        "created_at": created,
        "favorite_count": 3,
        "retweet_count": 2,
        "reply_count": 1,
        "lang": "en",
        "conversation_id_str": rest_id,
    }
    if media:
        legacy["extended_entities"] = {
            "media": [{
                "id_str": "m1",
                "type": "photo",
                "media_url_https": "https://pbs.twimg.com/media/a.jpg",
                "original_info": {"width": 100, "height": 80},
                "ext_alt_text": "alt",
            }]
        }
    return {
        "__typename": "Tweet",
        "rest_id": rest_id,
        "legacy": legacy,
        "core": {"user_results": {"result": user()}},
    }


def timeline(entries, instructions=None):
    return {"data": {"user": {"result": {"timeline_v2": {"timeline": {"instructions": instructions or [
        {"type": "TimelineAddEntries", "entries": entries}
    ]}}}}}}


def test_normalize_user_and_tweet():
    payload = timeline([
        {"entryId": "tweet-100", "content": {"itemContent": {"tweet_results": {"result": tweet()}}}},
        {"entryId": "cursor-bottom-1", "content": {"cursorType": "Bottom", "value": "CUR"}},
    ])
    posts, cursor = extract_timeline(payload, owner_screen_name="alice")
    assert cursor == "CUR"
    assert len(posts) == 1
    post = posts[0]
    assert post.tweet_id == "100"
    assert post.author_screen_name == "alice"
    assert post.text == "hello"
    assert post.media[0].media_type == "photo"
    assert post.media[0].download_url.endswith("name=orig")
    assert post.published_at == 1704110400


def test_new_schema_legacy_null():
    raw = {
        "__typename": "Tweet",
        "rest_id": "101",
        "legacy": None,
        "full_text": "new schema",
        "created_at": "Mon Jan 01 12:00:00 +0000 2024",
        "favorite_count": 7,
        "core": {"user_results": {"result": user(rest_id="2", screen="bob", name="Bob")}},
        "extended_entities": {"media": [{
            "id_str": "m2", "type": "photo", "media_url_https": "https://pbs.twimg.com/media/b.png",
            "original_info": {"width": 10, "height": 20}
        }]},
    }
    post = extract_tweet({"data": {"tweetResult": {"result": raw}}}, "101")
    assert post is not None
    assert post.text == "new schema"
    assert post.author_screen_name == "bob"
    assert post.favorite_count == 7
    assert post.media[0].file_name.endswith(".png")


def test_retweet_and_quote_are_not_duplicated():
    inner = tweet("200", "original", media=True)
    outer = tweet("300", "RT @alice: original", media=False)
    outer["legacy"]["retweeted_status_result"] = {"result": inner}
    payload = timeline([
        {"entryId": "tweet-300", "content": {"itemContent": {"tweet_results": {"result": outer}}}},
        {"entryId": "cursor-bottom-2", "content": {"cursorType": "Bottom", "value": "NEXT"}},
    ])
    posts, cursor = extract_timeline(payload)
    assert cursor == "NEXT"
    assert len(posts) == 1
    post = posts[0]
    assert post.tweet_id == "300"
    assert post.kind == "retweet"
    assert post.retweet_of_id == "200"
    assert post.media[0].media_key == "https://pbs.twimg.com/media/a.jpg"
    assert post.author_screen_name == "alice"


def test_quote_metadata():
    inner = tweet("201", "quoted", media=False)
    outer = tweet("301", "quoting", media=False)
    outer["legacy"]["quoted_status_result"] = {"result": inner}
    post = extract_tweet({"data": {"tweetResult": {"result": outer}}}, "301")
    assert post.kind == "quote"
    assert post.quoted.tweet_id == "201"
    assert post.quoted.text == "quoted"


def test_timeline_page_structures():
    module_page = timeline([], instructions=[
        {"type": "TimelineAddToModule", "moduleItems": [
            {"item": {"itemContent": {"tweet_results": {"result": tweet("150")}}}},
        ]},
        {"type": "TimelineAddEntries", "entries": [
            {"entryId": "cursor-top-1", "content": {"cursorType": "Top", "value": "TOP"}},
            {"entryId": "cursor-bottom-1", "content": {"cursorType": "Bottom", "value": "NEXT"}},
        ]},
    ])
    page = extract_timeline_page(module_page)
    assert len(page.posts) == 1
    assert page.cursor == "NEXT"
    assert page.module_item_count == 1
    assert page.has_content
    assert not page.cursor_only

    cursor_page = timeline([
        {"entryId": "cursor-top-1", "content": {"cursorType": "Top", "value": "TOP"}},
        {"entryId": "cursor-bottom-1", "content": {"cursorType": "Bottom", "value": "NEXT"}},
    ])
    page = extract_timeline_page(cursor_page)
    assert page.posts == []
    assert page.cursor == "NEXT"
    assert page.cursor_only

    top_terminate = timeline([], instructions=[
        {"type": "TimelineTerminateTimeline", "direction": "Top"},
        {"type": "TimelineAddEntries", "entries": [
            {"entryId": "cursor-bottom-1", "content": {"cursorType": "Bottom", "value": "NEXT"}},
        ]},
    ])
    page = extract_timeline_page(top_terminate)
    assert page.terminated is False
    assert page.cursor_only

    bottom_terminate = timeline([], instructions=[
        {"type": "TimelineTerminateTimeline", "direction": "Bottom"},
        {"type": "TimelineAddEntries", "entries": [
            {"entryId": "cursor-bottom-1", "content": {"cursorType": "Bottom", "value": "NEXT"}},
        ]},
    ])
    page = extract_timeline_page(bottom_terminate)
    assert page.terminated is True


def test_extract_user_new_schema():
    raw = {
        "__typename": "User",
        "rest_id": "42",
        "legacy": None,
        "core": {"screen_name": "neo", "name": "Neo"},
        "avatar": {"image_url": "https://pbs.twimg.com/a_normal.jpg"},
        "profile_bio": {"description": "there is no spoon"},
        "relationship_counts": {"followers": 99, "following": 3},
        "tweet_counts": {"tweets": 12, "media_tweets": 4},
    }
    result = extract_user({"data": {"user": {"result": raw}}})
    assert result["rest_id"] == "42"
    assert result["screen_name"] == "neo"
    assert result["bio"] == "there is no spoon"
    assert result["followers_count"] == 99
    assert result["media_count"] == 4
    assert result["avatar_url"].endswith("_400x400.jpg")
