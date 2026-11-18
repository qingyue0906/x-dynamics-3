from __future__ import annotations

import logging

from .db import Database

logger = logging.getLogger(__name__)


def prepare_runtime(config) -> None:
    db = Database(config.db_path)
    try:
        interrupted = db.mark_interrupted_jobs()
        if interrupted:
            logger.warning("发现 %d 个未完成后台任务，已标记为 interrupted", interrupted)
    finally:
        db.close()
