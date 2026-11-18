from __future__ import annotations

import csv
from pathlib import Path

from PIL import Image

from xdyn.config import load_config
from xdyn.db import Database
from xdyn.migrate import migrate_twitter_download


def write_csv(path: Path, header, rows, meta=None):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        for row in meta or []:
            writer.writerow(row)
        writer.writerow(header)
        writer.writerows(rows)


def build_old(root: Path):
    alice = root / "alice"
    alice.mkdir(parents=True)
    Image.new("RGB", (30, 20), (29, 155, 240)).save(alice / "2024-01-01 12-00-img_1.jpg")
    Image.new("RGB", (30, 20), (10, 120, 200)).save(alice / "2024-01-01 12-00-img_2.jpg")
    main_header = ["Tweet Date", "Display Name", "User Name", "Tweet URL", "Media Type", "Media URL", "Saved Filename", "Tweet Content", "Favorite Count", "Retweet Count", "Reply Count"]
    write_csv(
        alice / "alice-2024-01-01_00-00-00.csv",
        main_header,
        [
            ["2024-01-01 12:00", "Alice", "@alice", "https://x.com/alice/status/100/photo/1", "Image", "https://pbs.twimg.com/media/a.jpg", "2024-01-01 12-00-img_1.jpg", "hello", "3", "2", "1"],
            ["2024-01-01 12:00", "Alice", "@alice", "https://x.com/alice/status/100/photo/2", "Image", "https://pbs.twimg.com/media/b.jpg", "2024-01-01 12-00-img_2.jpg", "hello", "3", "2", "1"],
        ],
        meta=[["Alice", "@alice"], ["Tweet Range : 1990-01-01:2030-01-01"], ["Save Path : old"]],
    )
    text_header = ["Display Name", "User Name", "Tweet Date", "Tweet URL", "Tweet Content", "Favorite Count", "Retweet Count", "Reply Count"]
    write_csv(
        alice / "alice-2024-01-02_00-00-00-text.csv",
        text_header,
        [["Alice", "@alice", "2024-01-02 09:00", "https://x.com/alice/status/101", "text only", "1", "0", "0"]],
        meta=[["Alice", "@alice"], ["Tweet Range : x"], ["Save Path : old"]],
    )

    tag = root / "#Tag"
    tag.mkdir()
    (tag / "2024-01-03 10-00_@bob_abcd.png").write_bytes(b"not-real-media")  # importer must not decode it
    tag_header = ["Tweet Date", "Display Name", "User Name", "Tweet URL", "Media Type", "Media URL", "Saved Path", "Tweet Content", "Favorite Count", "Retweet Count", "Reply Count"]
    write_csv(
        tag / "2024-01-03_00-00-00-media.csv",
        tag_header,
        [["2024-01-03 10:00", "Bob", "@bob", "https://x.com/bob/status/200", "Image", "https://pbs.twimg.com/media/c.png", str(tag / "2024-01-03 10-00_@bob_abcd.png"), "tagged", "0", "0", "0"]],
        meta=[["Run Time : 2024-01-03 00:00:00"]],
    )

    reply = root / "replyuser"
    reply.mkdir()
    Image.new("RGB", (20, 20), (200, 80, 30)).save(reply / "2024-01-04 10-00_@carol_abcd_reply.png")
    reply_header = ["Parent Tweet URL", "Replier Display Name", "Replier User Name", "Reply Date", "Reply Content", "Reply URL", "Reply Favorite Count", "Reply Retweet Count", "Reply Reply Count"]
    write_csv(
        reply / "2024-01-04_00-00-00-Reply.csv",
        reply_header,
        [["https://x.com/alice/status/100", "Carol", "@carol", "2024-01-04 10:00", "nice", "https://x.com/carol/status/300", "1", "0", "0"]],
        meta=[["Run Time : 2024-01-04 00:00:00"]],
    )

    profile = root / "profile"
    profile.mkdir()
    Image.new("RGB", (300, 120), (30, 30, 30)).save(profile / "alice_avatar.jpg")
    Image.new("RGB", (600, 200), (40, 40, 40)).save(profile / "alice_banner.jpg")
    (profile / "alice_description.txt").write_text("Alice bio", encoding="utf-8")


def test_migrate_dry_run_and_copy(tmp_path):
    old = tmp_path / "old"
    build_old(old)
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data")
    db = Database(config.db_path)
    dry = migrate_twitter_download(db, config, old, mode="copy", dry_run=True)
    assert dry.posts >= 4
    assert db.stats()["posts"] == 0
    stats = migrate_twitter_download(db, config, old, mode="copy")
    assert stats.errors == []
    assert stats.users >= 1
    assert stats.collections >= 2
    assert stats.posts >= 4
    assert stats.ok_media >= 4
    assert db.stats()["posts"] >= 4
    assert db.stats()["media"] >= 4
    # main text/media merge, tag collection, reply collection
    assert db.get_user_by_screen("alice") is not None
    tag = db.get_user_by_source("tag:#Tag")
    assert tag is not None
    reply = db.get_user_by_source("reply:replyuser")
    assert reply is not None
    # imported files were copied, not decoded
    copied = list((config.images_dir).rglob("2024-01-03 10-00_@bob_abcd.png"))
    assert copied
    assert copied[0].read_bytes() == b"not-real-media"
    # profile avatar imported
    alice = db.get_user_by_screen("alice")
    assert alice["avatar_file"]
    assert alice["bio"] == "Alice bio"
    before_posts = db.stats()["posts"]
    before_media = db.stats()["media"]
    migrate_twitter_download(db, config, old, mode="copy")
    assert db.stats()["posts"] == before_posts
    assert db.stats()["media"] == before_media
    db.close()


def test_migrate_reference(tmp_path):
    old = tmp_path / "old"
    build_old(old)
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data")
    db = Database(config.db_path)
    stats = migrate_twitter_download(db, config, old, mode="reference", include={"main", "tags", "replies", "profile"})
    assert stats.errors == []
    alice = db.get_user_by_screen("alice")
    assert Path(alice["image_dir"]).is_absolute()
    assert not list(config.images_dir.glob("alice_*"))
    db.close()
