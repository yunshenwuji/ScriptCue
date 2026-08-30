"""本地时间工具。

与服务端 timebase 一致：毫秒级 Unix 时间戳。
"""

import time


def now_ms() -> float:
    """当前本地时间（毫秒级 Unix 时间戳，浮点以保留亚毫秒信息）。"""
    return time.time_ns() / 1_000_000
