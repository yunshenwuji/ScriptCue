"""被控端核心链路聚焦检查：绝对时刻调度 → 到点触发 → 回执。

覆盖 README「同步核心机制」与 docs/protocol.md 中被控端一侧的三条约定：

1. 时钟未同步（偏移为空）时，必须立即回送 error 回执并跳过触发，不得排期；
2. 已排期但未到点时收到 command.cancel，不得再注入按键、也不回送触发回执；
3. 正常路径按 `本地触发时刻 = at − 时钟偏移 − 补偿值` 换算，到点注入一次，
   并把触发时刻换算回服务器时钟后随 command.receipt 上报。

按键注入与消息回送都使用替身，因此本检查不需要真实服务端、不会连网、
也不会向任何窗口发送空格。仅依赖标准库与 agent/requirements.txt 已声明的依赖。

在仓库根目录运行：
    python -m unittest discover -s agent/tests -t agent -v
"""

import threading
import time
import unittest

from agent import protocol as p
from agent.clocksync import Sample
from agent.engine import AgentEngine
from agent.timeutil import now_ms

# 轮询等待时的间隔（秒）
POLL_INTERVAL_S = 0.01


class FakeKeySender:
    """按键注入替身：只记录调用次数，不触碰任何真实窗口。"""

    def __init__(self):
        self.press_count = 0
        self._lock = threading.Lock()

    def press_space(self) -> None:
        with self._lock:
            self.press_count += 1

    @property
    def pressed(self) -> bool:
        with self._lock:
            return self.press_count > 0


def make_engine(key_sender: FakeKeySender,
                events: list[dict],
                sent: list[dict]) -> AgentEngine:
    """构造一个不连网的引擎：待发消息被截获到 sent，状态事件被收集到 events。"""
    engine = AgentEngine(server_url="ws://127.0.0.1:8000",
                         room_code="ABC12D",
                         nickname="聚焦检查",
                         key_sender=key_sender,
                         on_event=events.append)
    # 引擎正常通过事件循环回送消息；此处没有连接，直接截获待发消息
    engine._send_threadsafe = sent.append
    return engine


def sync_clock(engine: AgentEngine, offset_ms: float) -> None:
    """注入一个可信时钟样本，使 engine.clock.offset_ms 变为指定偏移。"""
    engine.clock.samples.append(
        Sample(offset=offset_ms, rtt=5.0, taken_at=now_ms()))


def wait_until(predicate, timeout_s: float = 5.0) -> bool:
    """轮询等待条件成立。成立返回 True，超时返回 False。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_INTERVAL_S)
    return predicate()


def events_of(events: list[dict], name: str) -> list[dict]:
    """按事件名筛出引擎发出的状态事件。"""
    return [event for event in events if event.get("event") == name]


class ClockNotSyncedTest(unittest.TestCase):
    """负向用例一：时钟偏移为空。"""

    def test_sends_error_receipt_and_skips_fire(self):
        """立即回送 error 回执、登记 fire_skipped，且不排期、不注入按键。"""
        key_sender = FakeKeySender()
        events: list[dict] = []
        sent: list[dict] = []
        engine = make_engine(key_sender, events, sent)

        self.assertIsNone(engine.clock.offset_ms,
                          "前置条件：新建引擎不应已持有可信时钟偏移")

        engine._schedule_fire({"command_id": "cmd-nosync",
                               "command": p.CMD_PLAY,
                               "at": now_ms() + 1000})

        self.assertEqual(len(sent), 1, "时钟未同步时应立即回送且仅回送一条消息")
        receipt = sent[0]
        self.assertEqual(receipt["type"], p.CMD_RECEIPT)
        self.assertEqual(receipt["command_id"], "cmd-nosync")
        self.assertEqual(receipt["status"], "error")
        self.assertEqual(receipt["detail"], "时钟未同步")

        self.assertFalse(key_sender.pressed, "时钟未同步时不得注入按键")
        self.assertNotIn("cmd-nosync", engine._pending_fires,
                         "时钟未同步时不应登记待触发项")

        skipped = events_of(events, "fire_skipped")
        self.assertEqual(len(skipped), 1, "应发出一次 fire_skipped 供界面提示")
        self.assertEqual(skipped[0]["command_id"], "cmd-nosync")
        self.assertEqual(skipped[0]["reason"], "时钟未同步")
        self.assertEqual(events_of(events, "command_scheduled"), [],
                         "时钟未同步时不应发出已排期事件")


class CancelBeforeFireTest(unittest.TestCase):
    """负向用例二：已排期但未到点时收到取消。"""

    def test_command_cancel_prevents_key_injection(self):
        """触发线程应尽快退出：不注入按键，也不回送触发回执。"""
        key_sender = FakeKeySender()
        events: list[dict] = []
        sent: list[dict] = []
        engine = make_engine(key_sender, events, sent)
        offset_ms = 1200.0
        sync_clock(engine, offset_ms)

        command_id = "cmd-cancel"
        # 本地触发时刻约为 1.5 秒后，留出足够时间投递取消
        at = now_ms() + offset_ms + engine.compensation_ms + 1500
        engine._schedule_fire({"command_id": command_id,
                               "command": p.CMD_PLAY,
                               "at": at})

        self.assertIn(command_id, engine._pending_fires,
                      "指令应已排期并登记对应的取消事件")
        self.assertEqual(len(events_of(events, "command_scheduled")), 1)

        # command.cancel 分支不使用 ws 参数，此处无需真实连接
        engine._dispatch(None, {"type": p.CMD_CANCEL, "command_id": command_id})

        self.assertTrue(
            wait_until(lambda: command_id not in engine._pending_fires),
            "取消后触发线程应尽快退出并清理待触发项")
        self.assertFalse(key_sender.pressed, "收到取消后不得注入按键")
        self.assertEqual(sent, [], "被取消的指令不应回送触发回执")

        cancelled = events_of(events, "command_cancelled")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["command_id"], command_id)
        self.assertEqual(events_of(events, "command_fired"), [],
                         "被取消的指令不应发出已触发事件")


class NormalFireTest(unittest.TestCase):
    """正常路径：本地时刻换算、到点触发一次、回执换算回服务器时钟。"""

    def test_fires_at_converted_local_time_and_reports_receipt(self):
        """按 at − 偏移 − 补偿 排期，到点注入一次，并回送 status 为 ok 的回执。"""
        key_sender = FakeKeySender()
        events: list[dict] = []
        sent: list[dict] = []
        engine = make_engine(key_sender, events, sent)
        offset_ms = 1200.0
        # 取一个足够大的补偿值，使「漏减补偿」这类换算错误能被下面的容差判出来
        engine.compensation_ms = 300
        sync_clock(engine, offset_ms)

        command_id = "cmd-fire"
        at = now_ms() + offset_ms + engine.compensation_ms + 200
        engine._schedule_fire({"command_id": command_id,
                               "command": p.CMD_PLAY,
                               "at": at})

        scheduled = events_of(events, "command_scheduled")
        self.assertEqual(len(scheduled), 1, "应先发出一次已排期事件")
        self.assertEqual(scheduled[0]["command_id"], command_id)
        self.assertAlmostEqual(scheduled[0]["local_fire"],
                               at - offset_ms - engine.compensation_ms,
                               delta=1.0,
                               msg="本地触发时刻应为 at 减去时钟偏移再减去补偿值")
        self.assertAlmostEqual(scheduled[0]["offset_ms"], offset_ms, delta=0.1)
        self.assertEqual(scheduled[0]["compensation_ms"], engine.compensation_ms)

        self.assertTrue(wait_until(lambda: key_sender.press_count == 1),
                        "到点后应注入且仅注入一次空格")
        self.assertTrue(wait_until(lambda: len(sent) == 1),
                        "触发后应回送一条回执")

        receipt = sent[0]
        self.assertEqual(receipt["type"], p.CMD_RECEIPT)
        self.assertEqual(receipt["command_id"], command_id)
        self.assertEqual(receipt["status"], "ok")
        self.assertNotIn("detail", receipt, "成功回执不应携带错误详情")
        # 换算回服务器时钟后，触发时刻应比 at 早一个补偿值（提前触发以抵消注入延迟）
        self.assertAlmostEqual(receipt["fired_at"],
                               at - engine.compensation_ms,
                               delta=150.0,
                               msg="回执时刻应为实际本地触发时刻加回时钟偏移")
        self.assertNotIn(command_id, engine._pending_fires,
                         "触发完成后应清理待触发项")

        fired = events_of(events, "command_fired")
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["command_id"], command_id)
        self.assertEqual(fired[0]["status"], "ok")
        self.assertAlmostEqual(fired[0]["delta_ms"], -engine.compensation_ms,
                               delta=150.0,
                               msg="delta_ms 应为回执时刻与 at 之差")


if __name__ == "__main__":
    unittest.main()
