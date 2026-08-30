"""服务器授时基准。

所有协议中的"服务器时间"均以本模块为准，必须使用单调可靠的高精度来源。
time.time_ns() 在 Windows / Linux / macOS 上均可达毫秒级精度，满足授时需求。
"""

import time


def now_ms() -> int:
    """当前服务器时间（毫秒级 Unix 时间戳）。"""
    return time.time_ns() // 1_000_000
