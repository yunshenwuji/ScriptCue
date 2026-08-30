"""操作审计日志（S-07）。

JSON Lines 格式，记录每次指令下发与各端触发回执，供演出后复盘。
写入量极小（每场演出几十条），直接同步追加写即可。
"""

import json
import logging
import threading
from pathlib import Path

from .timebase import now_ms

logger = logging.getLogger("scriptcue.audit")


class AuditLog:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = open(self._path, "a", encoding="utf-8")

    def log(self, event: str, **fields) -> None:
        record = {"ts": now_ms(), "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False)
        try:
            with self._lock:
                self._fh.write(line + "\n")
                self._fh.flush()
        except OSError:
            logger.exception("审计日志写入失败: %s", line)

    def close(self) -> None:
        with self._lock:
            self._fh.close()
