from __future__ import annotations

from xdyn.config import load_config
from xdyn.db import Database
from xdyn.dedupe import dedupe_media
from xdyn.models import MediaRecord, PostRecord
from xdyn.utils import canonical_media_key


def _insert_media(db, post_id, key, url, file_name, size):
    db.conn.execute(
        """
        INSERT INTO media(post_id, media_key, media_type, url, download_url, file_name,
                          status, local_size)
        VALUES(?, ?, 'photo', ?, ?, ?, 'ok', ?)
        """,
        (post_id, key, url, url + "?name=orig", file_name, size),
    )


def test_canonical_media_key_photo_and_video():
    assert canonical_media_key("https://pbs.twimg.com/media/a.jpg?name=orig", "", "photo") == "https://pbs.twimg.com/media/a.jpg"
    assert canonical_media_key(
        "https://pbs.twimg.com/poster.jpg",
        "https://video.twimg.com/a.mp4?tag=1",
        "video",
    ) == "https://video.twimg.com/a.mp4"


def test_upsert_media_matches_migrated_url_key(tmp_path):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data")
    db = Database(config.db_path)
    folder = config.images_dir / "alice"
    folder.mkdir(parents=True)
    (folder / "old.jpg").write_bytes(b"x" * 10)
    user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
    post_id = db.upsert_post(PostRecord(
        tweet_id="100", user_id=user_id, author_name="Alice",
        url="https://x.com/alice/status/100",
    ))
    _insert_media(db, post_id, "https://pbs.twimg.com/media/a.jpg", "https://pbs.twimg.com/media/a.jpg", "old.jpg", 10)
    db.upsert_media(post_id, [MediaRecord(
        media_key="1955270187365588992",
        media_type="photo",
        url="https://pbs.twimg.com/media/a.jpg",
        download_url="https://pbs.twimg.com/media/a.jpg?name=orig",
        file_name="100_0.jpg",
        status="pending",
    )])
    rows = db.get_media_rows_for_dedupe(user_id)
    assert len(rows) == 1
    assert rows[0]["file_name"] == "old.jpg"
    db.close()


def test_dedupe_merges_rows_and_optional_files(tmp_path):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data")
    db = Database(config.db_path)
    folder = config.images_dir / "alice"
    folder.mkdir(parents=True)
    (folder / "old.jpg").write_bytes(b"a" * 20)
    (folder / "new.jpg").write_bytes(b"a" * 20)
    user_id = db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
    post_id = db.upsert_post(PostRecord(
        tweet_id="100", user_id=user_id, author_name="Alice",
        url="https://x.com/alice/status/100",
    ))
    _insert_media(db, post_id, "https://pbs.twimg.com/media/a.jpg", "https://pbs.twimg.com/media/a.jpg", "old.jpg", 20)
    _insert_media(db, post_id, "1955270187365588992", "https://pbs.twimg.com/media/a.jpg", "new.jpg", 20)

    dry = dedupe_media(db, config, user_id=user_id, dry_run=True, delete_files=True)
    assert dry.groups == 1
    assert db.conn.execute("SELECT COUNT(*) FROM media WHERE post_id = ?", (post_id,)).fetchone()[0] == 2
    assert (folder / "new.jpg").is_file()

    stats = dedupe_media(db, config, user_id=user_id, dry_run=False, delete_files=True)
    assert stats.removed_rows == 1
    assert stats.files_deleted == 1
    rows = db.get_media_rows_for_dedupe(user_id)
    assert len(rows) == 1
    assert rows[0]["file_name"] == "old.jpg"
    assert not (folder / "new.jpg").exists()
    assert (folder / "old.jpg").is_file()
    post = db.conn.execute("SELECT media_count FROM posts WHERE id = ?", (post_id,)).fetchone()
    assert post["media_count"] == 1
    db.close()
