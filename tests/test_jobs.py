from __future__ import annotations

import pytest

from xdyn import jobs
from xdyn.config import load_config
from xdyn.db import Database
from xdyn.sync import SyncResult


class FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeContext:
    def __init__(self, config):
        self.base_dir = config.base_dir
        self.data_dir = config.data_dir
        self.messages = []

    def update(self, **fields):
        self.messages.append(fields)

    def check_cancel(self):
        return None


def test_run_sync_marks_job_failed_when_any_user_fails(tmp_path, monkeypatch):
    config = load_config(base_dir=tmp_path, data_dir=tmp_path / "data")
    config.auth_token = "token"
    config.ct0 = "ct0"
    db = Database(config.db_path)
    db.upsert_user(rest_id="1", screen_name="alice", name="Alice", image_dir="images/alice")
    db.close()

    async def fake_sync_many(*args, **kwargs):
        return [SyncResult(user_id=1, name="Alice", status="failed", message="network reset")]

    monkeypatch.setattr(jobs, "_config", lambda ctx: config)
    monkeypatch.setattr(jobs, "XClient", lambda config: FakeClient())
    monkeypatch.setattr(jobs, "sync_many", fake_sync_many)

    with pytest.raises(RuntimeError, match="断点已保存"):
        jobs.run_sync(FakeContext(config))
