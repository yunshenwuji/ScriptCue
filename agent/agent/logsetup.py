"""被控端本地文件日志初始化。

将引擎事件与关键运行时信息以人可读的带时间戳纯文本格式写入本地文件，
便于用户遇到问题时把日志发给开发者定位根因。

- 日志目录与配置目录同级：
    Windows: %APPDATA%/ScriptCue/logs/
    macOS:   ~/.config/ScriptCue/logs/
- 文件：agent.log，RotatingFileHandler 轮转（单文件 2MB，保留 3 个备份）
- 编码：UTF-8
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "agent.log"
LOG_MAX_BYTES = 2 * 1024 * 1024   # 单文件上限 2MB
LOG_BACKUP_COUNT = 3              # 保留 3 个备份（总计最多 ~8MB）

# 精确到毫秒的时间戳格式：[2026-09-04 14:30:05.123] INFO  消息内容
LOG_FORMAT = "[%(asctime)s.%(msecs)03d] %(levelname)-5s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_logger_name = "scriptcue.agent"
_log_path: Path | None = None


def log_dir() -> Path:
    """日志目录（与 gui.config_path() 的配置目录同级）。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    return base / "ScriptCue" / "logs"


def setup_logging(level: int = logging.DEBUG) -> Path:
    """初始化文件日志，返回日志文件路径。

    幂等：多次调用不会重复添加 handler，首次调用后返回同一路径。
    """
    global _log_path
    logger = logging.getLogger(_logger_name)
    if _log_path is not None:
        return _log_path

    directory = log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / LOG_FILE_NAME, maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    except OSError:
        # 日志目录不可写（磁盘满/权限问题）时静默降级：不写文件，不影响主流程
        logger.warning("无法创建日志文件目录: %s", directory)
        _log_path = directory / LOG_FILE_NAME
        return _log_path

    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT))
    logger.setLevel(level)
    logger.addHandler(handler)
    # 避免向 root logger 传播导致重复输出（如宿主环境已配置 root）
    logger.propagate = False
    _log_path = directory / LOG_FILE_NAME
    return _log_path
