from __future__ import annotations

import asyncio
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from PIL import Image

from xdyn.config import load_config
from xdyn.db import Database
from xdyn.download import Downloader
from xdyn.models import MediaRecord, PostRecord


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def test_download_rejects_non_media_payload(tmp_path):
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()
    (serve_dir / "bad.json").write_text('{"error": "not media"}', encoding="utf-8")
    handler = partial(QuietHandler, directory=str(serve_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data", sleep=0)
        config.sync.retries = 0
        db = Database(config.db_path)
        user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
        (config.data_dir / "images/alice").mkdir(parents=True)
        post = PostRecord(
            tweet_id="101", user_id=user_id, author_name="Alice",
            published_at=1700000000, url="https://x.com/alice/status/101",
            media=[MediaRecord(
                media_key="bad", media_type="photo",
                url="http://127.0.0.1:%s/bad.json" % port,
                download_url="http://127.0.0.1:%s/bad.json" % port,
                file_name="bad.jpg", status="pending",
            )],
        )
        with db.transaction():
            post_id = db.upsert_post(post)
            db.upsert_media(post_id, post.media)
        rows = db.get_media_for_posts([post_id])

        async def run():
            async with Downloader(db, config) as downloader:
                return await downloader.download_rows(rows)

        stats = asyncio.run(run())
        assert stats.failed == 1
        row = db.get_media_with_user_by_id(rows[0]["id"])
        assert row["status"] == "failed"
        assert "application/json" in (row["last_error"] or "")
        assert not (config.data_dir / "images/alice/bad.jpg").exists()
        db.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_download_image_and_extension_correction(tmp_path):
    serve_dir = tmp_path / "serve"
    serve_dir.mkdir()
    Image.new("RGB", (20, 10), (29, 155, 240)).save(serve_dir / "pic.png")
    handler = partial(QuietHandler, directory=str(serve_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data", sleep=0)
        db = Database(config.db_path)
        user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
        (config.data_dir / "images/alice").mkdir(parents=True)
        post = PostRecord(
            tweet_id="100", user_id=user_id, author_name="Alice",
            published_at=1700000000, url="https://x.com/alice/status/100",
            media=[MediaRecord(
                media_key="m1", media_type="photo",
                url="http://127.0.0.1:%s/pic.png" % port,
                download_url="http://127.0.0.1:%s/pic.png" % port,
                file_name="pic.jpg", status="pending",
            )],
        )
        with db.transaction():
            post_id = db.upsert_post(post)
            db.upsert_media(post_id, post.media)
        rows = db.get_media_for_posts([post_id])
        assert len(rows) == 1

        async def run():
            async with Downloader(db, config) as downloader:
                return await downloader.download_rows(rows)

        stats = asyncio.run(run())
        assert stats.ok == 1
        row = db.get_media_with_user_by_id(rows[0]["id"])
        assert row["status"] == "ok"
        assert row["file_name"].endswith(".png")
        target = config.data_dir / "images/alice" / row["file_name"]
        assert target.is_file() and target.stat().st_size > 0
        db.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
