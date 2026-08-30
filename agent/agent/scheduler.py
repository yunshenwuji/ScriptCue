"""高精度定时调度（R-03 / 非功能需求）。

实现要求（PRD §8）：不得使用普通 sleep 到点（OS 调度误差可达 15ms+），
采用 **"粗睡眠 + 最后 50ms 自旋等待"** 策略：

1. 距离目标时刻较远时用 sleep 粗等待（可被取消事件打断）；
2. 进入最后 SPIN_THRESHOLD_MS 毫秒后切换为忙等自旋，
   用忙循环逼近目标时刻，误差可控制在亚毫秒级。

Windows 下将系统定时器分辨率提升到 1ms，保证粗睡眠段不会睡过头太多。
"""

import sys
import threading
import time

from . import protocol as p
from .timeutil import now_ms

if sys.platform == "win32":
    import ctypes

    # timeBeginPeriod(1)：提升系统时钟中断频率，改善 sleep 精度
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:  # pragma: no cover
        pass

# 粗睡眠段每次最长睡眠时长（秒），保证取消响应及时
COARSE_SLEEP_CHUNK_S = 0.25


def precise_wait_until(deadline_ms: float,
                       cancel_event: threading.Event | None = None,
                       spin_threshold_ms: float = p.SPIN_THRESHOLD_MS) -> tuple[float, bool]:
    """等待直到本地时刻 deadline_ms。

    返回 (实际醒来时刻, 是否被取消)。被取消时立即返回，不再等待。
    """
    while True:
        remaining = deadline_ms - now_ms()
        if remaining <= spin_threshold_ms:
            break
        sleep_s = min((remaining - spin_threshold_ms) / 1000.0, COARSE_SLEEP_CHUNK_S)
        if sleep_s <= 0:
            break
        if cancel_event is not None:
            if cancel_event.wait(sleep_s):
                return now_ms(), True
        else:
            time.sleep(sleep_s)

    # 末段自旋：忙等到点，期间仍检查取消（取消发生在临门一脚时宁可放行也不阻塞退出）
    while now_ms() < deadline_ms:
        if cancel_event is not None and cancel_event.is_set():
            return now_ms(), True

    return now_ms(), False
