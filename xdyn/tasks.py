from __future__ import annotations

import atexit
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .db import Database
from .errors import JobCancelled

logger = logging.getLogger(__name__)


class _ThreadFilter(logging.Filter):
    def __init__(self, thread_name: str) -> None:
        super().__init__()
        self.thread_name = thread_name

    def filter(self, record: logging.LogRecord) -> bool:
        return record.threadName == self.thread_name


@dataclass
class JobContext:
    job_id: int
    kind: str
    target: str
    base_dir: Path
    data_dir: Path
    log_path: Path
    cancel_event: threading.Event
    _db_path: Path = field(repr=False)

    def update(self, total: int | None = None, done: int | None = None, message: str | None = None) -> None:
        fields: dict = {}
        if total is not None:
            fields["total"] = max(0, int(total))
        if done is not None:
            fields["done"] = max(0, int(done))
        if message is not None:
            fields["message"] = str(message)[:500]
        if not fields:
            return
        db = Database(self._db_path, init=False)
        try:
            db.update_job(self.job_id, **fields)
        finally:
            db.close()

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()

    def log_tail(self, limit: int = 120) -> list[str]:
        return read_log_tail(self.log_path, limit=limit)


def read_log_tail(path: Path, limit: int = 120) -> list[str]:
    if not path:
        return []
    file_path = Path(path)
    if not file_path.is_file():
        return []
    try:
        with file_path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            block = min(size, 256 * 1024)
            handle.seek(size - block)
            data = handle.read(block)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if block < size and lines:
        # The first line may have been cut in the middle.
        lines = lines[1:]
    return lines[-max(1, int(limit)):]


class TaskManager:
    """One worker, FIFO background jobs persisted in SQLite."""

    def __init__(self, config: Config) -> None:
        self.base_dir = config.base_dir
        self.data_dir = config.data_dir
        self.db_path = config.db_path
        self.jobs_dir = config.job_logs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        Database(self.db_path).close()
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.RLock()
        self._events: dict[int, threading.Event] = {}
        self._mark_interrupted()
        atexit.register(self.shutdown)

    def _db(self) -> Database:
        return Database(self.db_path, init=False)

    def _mark_interrupted(self) -> None:
        db = self._db()
        try:
            count = db.mark_interrupted_jobs()
            if count:
                logger.warning("服务重启，%d 个未完成任务已标记为 interrupted", count)
        finally:
            db.close()

    def _executor_or_create(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xdyn-job")
            return self._executor

    def submit(self, kind: str, target: str, func: Callable[[JobContext], None]) -> int:
        db = self._db()
        try:
            job_id = db.create_job(kind, target)
        finally:
            db.close()
        event = threading.Event()
        with self._lock:
            self._events[job_id] = event
        self._executor_or_create().submit(self._run, job_id, kind, target, func, event)
        return job_id

    def _run(self, job_id: int, kind: str, target: str, func, event: threading.Event) -> None:
        db = self._db()
        job = db.get_job(job_id)
        if not job or job["status"] == "cancelled":
            db.close()
            with self._lock:
                self._events.pop(job_id, None)
            return
        started = int(time.time())
        log_path = self.jobs_dir / f"{job_id}_{kind}_{started}.log"
        db.update_job(
            job_id, status="running", started_at=started,
            log_path=str(log_path), message="开始执行",
        )
        db.close()

        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        handler.addFilter(_ThreadFilter(threading.current_thread().name))
        root = logging.getLogger()
        root.addHandler(handler)
        ctx = JobContext(
            job_id=job_id, kind=kind, target=target,
            base_dir=self.base_dir, data_dir=self.data_dir,
            log_path=log_path, cancel_event=event, _db_path=self.db_path,
        )
        status = "succeeded"
        error = ""
        message = ""
        try:
            if event.is_set():
                raise JobCancelled()
            func(ctx)
        except JobCancelled:
            status = "cancelled"
            message = "已取消"
            logger.warning("任务 %s 已取消", job_id)
        except Exception as exc:
            status = "failed"
            error = str(exc)[:1000]
            message = f"失败：{exc}"
            logger.exception("任务 %s 失败：%s", job_id, exc)
        finally:
            root.removeHandler(handler)
            handler.close()
            db = self._db()
            try:
                current = db.get_job(job_id)
                current_message = str(current["message"] or "") if current else ""
                if not message:
                    if status == "succeeded":
                        message = current_message if current_message and current_message != "开始执行" else "完成"
                    elif status == "cancelled":
                        message = "已取消"
                    else:
                        message = "失败"
                db.update_job(job_id, status=status, finished_at=int(time.time()), message=message, error=error)
            finally:
                db.close()
            with self._lock:
                self._events.pop(job_id, None)

    def cancel(self, job_id: int) -> bool:
        db = self._db()
        try:
            job = db.get_job(job_id)
            if not job:
                return False
            status = job["status"]
            if status in ("succeeded", "failed", "cancelled", "interrupted"):
                return False
            with self._lock:
                event = self._events.get(job_id)
                if event:
                    event.set()
            if status == "queued":
                db.update_job(job_id, status="cancelled", finished_at=int(time.time()), message="排队中取消")
            else:
                db.update_job(job_id, status="cancelling", message="正在取消")
            return True
        finally:
            db.close()

    def active_job(self):
        db = self._db()
        try:
            return db.active_job()
        finally:
            db.close()

    def log_tail(self, job_id: int, limit: int = 120) -> list[str]:
        db = self._db()
        try:
            job = db.get_job(job_id)
        finally:
            db.close()
        if not job:
            return []
        return read_log_tail(Path(job["log_path"]), limit=limit)

    def shutdown(self) -> None:
        with self._lock:
            executor = self._executor
            self._executor = None
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)
