"""时钟同步模块（R-02）。

类 NTP 算法：被控端发送 ping（携带本地发出时刻 t0），服务器回复服务器时间 ts，
被控端记录收到时刻 t1，估算 offset = ts - (t0 + t1) / 2，rtt = t1 - t0。
多次采样后取 **RTT 最小** 的样本作为可信偏移。

样本带时间戳并设有老化窗口：网络路由变化后旧样本会被逐步淘汰，
避免陈旧的"最小 RTT 样本"持续主导估算。
"""

from dataclasses import dataclass

from . import protocol as p
from .timeutil import now_ms

# 样本最长保留时间（超过后仅在无新样本时兜底使用）
SAMPLE_MAX_AGE_MS = 10 * 60 * 1000
# 样本总量上限（按 RTT 排序保留最优的一批）
SAMPLE_KEEP_MAX = 200


@dataclass
class Sample:
    offset: float   # 服务器时钟 - 本地时钟（毫秒）
    rtt: float      # 往返时延（毫秒）
    taken_at: float  # 采样时刻（本地毫秒时间戳）


class ClockSync:
    def __init__(self):
        self.samples: list[Sample] = []
        self._seq = 0
        self._pending: dict[int, float] = {}  # 请求 id -> 本地发出时刻 t0

    # ---- 请求 / 应答 ----

    def make_request(self) -> dict:
        """构造一条时钟同步请求消息。"""
        self._seq += 1
        t0 = now_ms()
        self._pending[self._seq] = t0
        return {"type": p.CLOCK_SYNC_REQ, "id": self._seq, "t0": t0}

    def handle_response(self, msg: dict) -> Sample | None:
        """处理服务器应答，返回新样本（无效应答返回 None）。"""
        req_id = msg.get("id")
        t0 = self._pending.pop(req_id, None)
        ts = msg.get("ts")
        if t0 is None or not isinstance(ts, (int, float)):
            return None
        t1 = now_ms()
        sample = Sample(offset=ts - (t0 + t1) / 2.0, rtt=t1 - t0, taken_at=t1)
        self.samples.append(sample)
        self._prune()
        return sample

    def _prune(self) -> None:
        if len(self.samples) <= SAMPLE_KEEP_MAX:
            return
        # 保留 RTT 最小的前若干样本（其中必然包含最新的）
        self.samples.sort(key=lambda s: s.rtt)
        self.samples = self.samples[:SAMPLE_KEEP_MAX // 2]

    # ---- 估算结果 ----

    def _fresh_samples(self) -> list[Sample]:
        cutoff = now_ms() - SAMPLE_MAX_AGE_MS
        fresh = [s for s in self.samples if s.taken_at >= cutoff]
        return fresh or list(self.samples)

    @property
    def best(self) -> Sample | None:
        """可信偏移样本：新鲜样本中 RTT 最小者。"""
        fresh = self._fresh_samples()
        if not fresh:
            return None
        return min(fresh, key=lambda s: s.rtt)

    @property
    def offset_ms(self) -> float | None:
        best = self.best
        return best.offset if best else None

    @property
    def rtt_ms(self) -> float | None:
        best = self.best
        return best.rtt if best else None

    def quality(self) -> str:
        """时钟质量分级（协议第 3 节），随心跳上报。"""
        best = self.best
        n = len(self._fresh_samples())
        if best is None:
            return p.QUALITY_NONE
        if n >= 10 and best.rtt <= 80:
            return p.QUALITY_EXCELLENT
        if n >= 5 and best.rtt <= 200:
            return p.QUALITY_GOOD
        return p.QUALITY_POOR

    def reset(self) -> None:
        """重连后清空历史样本，重新密集采样。"""
        self.samples.clear()
        self._pending.clear()
        self._seq = 0
