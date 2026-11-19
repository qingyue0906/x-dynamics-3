from __future__ import annotations

from PIL import Image

from xdyn.config import load_config
from xdyn.db import Database
from xdyn.models import MediaRecord, PostRecord
from xdyn.web.app import create_app


def seed(tmp_path):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data")
    db = Database(config.db_path)
    folder = config.images_dir / "1_alice"
    folder.mkdir(parents=True)
    Image.new("RGB", (40, 20), (29, 155, 240)).save(folder / "pic.png")
    (folder / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/1_alice")
    post = PostRecord(
        tweet_id="123456789",
        user_id=user_id,
        author_name="Alice",
        author_screen_name="alice",
        text="Hello 世界",
        published_at=1700000000,
        url="https://x.com/alice/status/123456789",
        media=[
            MediaRecord(media_key="m1", media_type="photo", url="https://x/1.png", download_url="https://x/1.png", file_name="pic.png", width=40, height=20, status="ok", local_size=10),
            MediaRecord(media_key="m2", media_type="video", url="https://x/2.jpg", download_url="https://x/2.mp4", poster_url="", file_name="clip.mp4", status="ok", local_size=10, duration_ms=65000),
        ],
    )
    with db.transaction():
        post_id = db.upsert_post(post)
        db.upsert_media(post_id, post.media)
    db.close()
    return config, user_id, post_id


def test_pages_and_api(tmp_path):
    config, user_id, _ = seed(tmp_path)
    app = create_app(config)
    client = app.test_client()
    for path in ("/", "/users", "/jobs", "/settings", "/tweet", f"/user/{user_id}", "/search?q=hello"):
        response = client.get(path)
        assert response.status_code == 200, path
    response = client.get("/api/posts?offset=0")
    assert response.status_code == 200
    assert "Alice" in response.get_json()["html"]
    response = client.get("/?mode=images")
    assert response.status_code == 200
    assert b"album-item" in response.data
    response = client.get("/api/media?offset=0&limit=5")
    assert response.status_code == 200
    assert "album-item" in response.get_json()["html"]


def test_media_thumbnail_and_video(tmp_path):
    config, user_id, _ = seed(tmp_path)
    app = create_app(config)
    client = app.test_client()
    response = client.get("/media/1")
    assert response.status_code == 200
    assert response.data.startswith(b"\x89PNG")
    response = client.get("/thumb/1")
    assert response.status_code == 200
    assert response.mimetype == "image/webp"
    response = client.get("/media/2")
    assert response.status_code == 200
    response = client.get("/thumb/2")
    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert client.get("/media/9999").status_code == 404


def test_video_poster_is_cached_locally(tmp_path, monkeypatch):
    from io import BytesIO

    import xdyn.web.app as webapp

    config, _, _ = seed(tmp_path)
    db = Database(config.db_path)
    db.conn.execute("UPDATE media SET poster_url = ? WHERE id = 2", ("https://example.invalid/poster.jpg",))
    db.close()
    payload = BytesIO()
    Image.new("RGB", (64, 36), (10, 20, 30)).save(payload, "JPEG")
    monkeypatch.setattr(webapp, "_fetch_remote_image", lambda config, url: payload.getvalue())
    app = create_app(config)
    client = app.test_client()
    response = client.get("/thumb/2")
    assert response.status_code == 200
    assert response.mimetype == "image/webp"
    assert response.data.startswith(b"RIFF")
    assert client.get("/thumb/2").status_code == 200


def test_avatar_banner_and_toggle(tmp_path):
    config, user_id, _ = seed(tmp_path)
    app = create_app(config)
    client = app.test_client()
    response = client.get(f"/avatar/{user_id}")
    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    response = client.post(f"/api/users/{user_id}/toggle", json={"enabled": False}, headers={"X-Xdyn": "1"})
    assert response.status_code == 200
    assert client.post(f"/api/users/{user_id}/toggle", json={"enabled": True}).status_code == 403
    assert client.post(f"/api/users/{user_id}/mode", json={"mode": "timeline"}, headers={"X-Xdyn": "1"}).status_code == 404


def test_reset_sync_session(tmp_path):
    config, user_id, _ = seed(tmp_path)
    db = Database(config.db_path)
    db.set_sync_session(user_id, "cursor-1", 0, 100)
    db.close()
    app = create_app(config)
    client = app.test_client()
    assert client.get("/users").status_code == 200
    assert client.get(f"/user/{user_id}").status_code == 200
    assert client.post(f"/api/users/{user_id}/reset-sync", json={}).status_code == 403
    response = client.post(f"/api/users/{user_id}/reset-sync", json={}, headers={"X-Xdyn": "1"})
    assert response.status_code == 200
    db = Database(config.db_path)
    user = db.get_user(user_id)
    assert user["sync_active"] == 0
    db.close()


def test_settings_csrf_and_cookies(tmp_path):
    config, _, _ = seed(tmp_path)
    app = create_app(config)
    client = app.test_client()
    assert client.post("/api/settings/options", json={}).status_code == 403
    response = client.post("/api/settings/options", json={}, headers={"X-Xdyn": "1"})
    assert response.status_code == 200
    payload = client.get("/api/settings").get_json()
    assert payload["cookies"]["configured"] is False
    response = client.post("/api/settings/cookies/clear", json={}, headers={"X-Xdyn": "1"})
    assert response.status_code == 200


def test_path_traversal_is_blocked(tmp_path):
    config, _, post_id = seed(tmp_path)
    db = Database(config.db_path)
    with db.transaction():
        ids = db.upsert_media(post_id, [MediaRecord(
            media_key="evil", media_type="photo", url="https://evil/x.png",
            file_name="../outside.png", status="ok",
        )])
    db.close()
    app = create_app(config)
    client = app.test_client()
    assert client.get(f"/media/{ids[0]}").status_code == 404
