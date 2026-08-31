"""被控端同步引擎：连接生命周期、时钟同步、绝对时刻调度、触发回执。

设计上"同步引擎"与"执行动作"（按键注入）解耦：引擎负责协议与定时，
动作由注入的 KeySender 完成。CLI 版与 GUI 版共用本引擎。

线程模型：引擎运行在一个 asyncio 事件循环中（调用方负责创建）；
触发在独立线程中执行（自旋等待不能阻塞事件循环），触发完成后通过
run_coroutine_threadsafe 回事件循环发送回执。所有 on_event 回调
均在引擎线程中调用，GUI 需自行切换到 UI 线程。
"""

import asyncio
import json
import logging
import threading

import websockets

from . import protocol as p
from .clocksync import ClockSync
from .keysender import KeySender
from .scheduler import precise_wait_until
from .timeutil import now_ms

logger = logging.getLogger("scriptcue.agent")

# 触发前倒数提示音的起点（R-08）
BEEP_COUNTDOWN_MS = 3000


class AgentEngine:
    def __init__(self, server_url: str, room_code: str, nickname: str,
                 password: str | None = None, room_name: str | None = None,
                 key_sender: KeySender | None = None,
                 on_event=None, beep_fn=None):
        """
        on_event(event: dict) —— 引擎状态回调（引擎线程中调用）。
        beep_fn() —— 单次"滴"声回调，为 None 时不播提示音（R-08）。
        """
        self.server_url = self._normalize_url(server_url)
        self.room_code = room_code.strip().upper()
        self.nickname = nickname
        self.password = password
        self.room_name = room_name
        self.key_sender = key_sender
        self.on_event = on_event or (lambda evt: None)
        self.beep_fn = beep_fn

        self.clock = ClockSync()
        self.compensation_ms = 0
        self.ready = False
        self.connected = False
        self.token: str | None = None
        self.lead_ms = p.DEFAULT_LEAD_MS

        self._ws = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_fires: dict[str, threading.Event] = {}
        self._has_joined = False     # 本进程是否成功加入过该房间
        self._auto_create = False    # 服务器重启后自动重建房间
        self._stopped = False

    # ------------------------------------------------------------------
    # 对外控制接口（可在任意线程调用）
    # ------------------------------------------------------------------

    def set_ready(self, ready: bool) -> None:
        self.ready = ready
        self._send_heartbeat_now()

    def set_compensation(self, ms: int) -> None:
        """本地修改补偿值（R-05）：本地立即生效并上报服务器。"""
        self.compensation_ms = ms
        self._send_threadsafe({"type": p.AGENT_SET_COMP, "compensation_ms": ms})

    def stop(self) -> None:
        self._stopped = True

    def disconnect(self) -> None:
        """停止引擎并立即断开当前连接（用户主动退出房间时使用）。"""
        self._stopped = True
        for cancel in self._pending_fires.values():
            cancel.set()
        ws, loop = self._ws, self._loop
        if ws is not None and loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.strip()
        if url.startswith("http://"):
            url = "ws://" + url[len("http://"):]
        elif url.startswith("https://"):
            url = "wss://" + url[len("https://"):]
        elif not url.startswith(("ws://", "wss://")):
            url = "ws://" + url
        return url.rstrip("/") + "/ws"

    async def run(self) -> None:
        """主循环：连接 → 会话 → 断线后指数退避重连（R-07）。"""
        attempt = 0
        while not self._stopped:
            try:
                self._emit({"event": "connecting", "url": self.server_url})
                await self._connect_and_serve()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._emit({"event": "error", "message": str(exc)})
                logger.warning("会话异常: %s", exc)
            self.connected = False
            self._ws = None
            if self._stopped:
                break
            delay = min(p.RECONNECT_BACKOFF_S[min(attempt, len(p.RECONNECT_BACKOFF_S) - 1)],
                        p.RECONNECT_BACKOFF_MAX_S)
            attempt += 1
            self._emit({"event": "reconnecting", "delay_s": delay})
            await asyncio.sleep(delay)

    async def _connect_and_serve(self) -> None:
        async with websockets.connect(self.server_url, open_timeout=10,
                                      ping_interval=10, ping_timeout=20) as ws:
            self._ws = ws
            self._loop = asyncio.get_running_loop()
            await self._join(ws)

            tasks = [
                asyncio.create_task(self._dense_sync(ws)),
                asyncio.create_task(self._heartbeat_loop(ws)),
                asyncio.create_task(self._maintain_sync_loop(ws)),
            ]
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self._dispatch(ws, msg)
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                self.connected = False
                self._emit({"event": "disconnected"})

    async def _join(self, ws) -> None:
        join_msg = {
            "type": p.AGENT_JOIN, "proto": p.PROTO_VERSION,
            "room_code": self.room_code, "nickname": self.nickname,
            # 携带本地补偿值：服务器重启后房间重建时用于恢复（否则校准结果丢失）
            "compensation_ms": self.compensation_ms,
        }
        if self.password:
            join_msg["password"] = self.password
        if self.token:
            join_msg["token"] = self.token
        if self._auto_create:
            join_msg["auto_create"] = True
            join_msg["room_name"] = self.room_name or self.room_code

        await ws.send(json.dumps(join_msg))
        first = json.loads(await asyncio.wait_for(ws.recv(), 10))

        if first.get("type") == p.ERROR:
            code = first.get("code")
            if code == p.ERR_ROOM_NOT_FOUND and self._has_joined:
                # 服务器重启导致房间丢失 → 下次重连自动重建
                self._auto_create = True
            raise ConnectionError(f"加入房间失败: {first.get('message')} ({code})")
        if first.get("type") != p.AGENT_JOINED:
            raise ConnectionError(f"意外的应答: {first.get('type')}")

        self.token = first.get("token")
        # 仅当服务器持有有效补偿值（令牌恢复会话等）时采纳，
        # 否则保留本地值（房间重建场景服务器为新会话，补偿值为默认 0）
        server_comp = first.get("compensation_ms")
        if isinstance(server_comp, (int, float)) and self.token and first.get("resumed"):
            self.compensation_ms = server_comp
        self.lead_ms = first.get("lead_ms", p.DEFAULT_LEAD_MS)
        self._has_joined = True
        self._auto_create = False
        self.clock.reset()
        self.connected = True
        self._emit({"event": "connected", "room_code": self.room_code,
                    "server_time": first.get("server_time")})

    # ------------------------------------------------------------------
    # 时钟同步（R-02）
    # ------------------------------------------------------------------

    async def _dense_sync(self, ws) -> None:
        """加入房间后立即密集采样。"""
        for _ in range(p.DENSE_SYNC_SAMPLES):
            await ws.send(json.dumps(self.clock.make_request()))
            await asyncio.sleep(p.DENSE_SYNC_INTERVAL_MS / 1000)

    async def _maintain_sync_loop(self, ws) -> None:
        """维持性采样，抑制时钟漂移。"""
        while True:
            await asyncio.sleep(p.MAINTAIN_SYNC_INTERVAL_S)
            try:
                await ws.send(json.dumps(self.clock.make_request()))
            except Exception:
                return

    # ------------------------------------------------------------------
    # 心跳（S-02 / R-06 状态上报）
    # ------------------------------------------------------------------

    def _heartbeat_msg(self) -> dict:
        msg = {
            "type": p.AGENT_HEARTBEAT,
            "ready": self.ready,
            "clock_quality": self.clock.quality(),
            "compensation_ms": self.compensation_ms,
        }
        offset = self.clock.offset_ms
        rtt = self.clock.rtt_ms
        if offset is not None:
            msg["clock_offset_ms"] = round(offset, 1)
        if rtt is not None:
            msg["clock_rtt_ms"] = round(rtt, 1)
        pending = next(iter(self._pending_fires), None)
        if pending:
            msg["pending_command"] = pending
        return msg

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            try:
                await ws.send(json.dumps(self._heartbeat_msg()))
            except Exception:
                return
            await asyncio.sleep(p.HEARTBEAT_INTERVAL_S)

    def _send_heartbeat_now(self) -> None:
        """状态变化（如就绪切换）时立即补发一次心跳。"""
        self._send_threadsafe(self._heartbeat_msg())

    def _send_threadsafe(self, msg: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send_json(msg), loop)
        except RuntimeError:
            pass

    async def _send_json(self, msg: dict) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(json.dumps(msg))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 消息分发
    # ------------------------------------------------------------------

    def _dispatch(self, ws, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == p.CLOCK_SYNC_RES:
            sample = self.clock.handle_response(msg)
            if sample is not None:
                self._emit({"event": "clock_sample", "offset_ms": round(sample.offset, 1),
                            "rtt_ms": round(sample.rtt, 1),
                            "quality": self.clock.quality(),
                            "samples": len(self.clock.samples)})
        elif mtype == p.CMD_EXEC:
            self._schedule_fire(msg)
        elif mtype == p.CMD_CANCEL:
            cancel = self._pending_fires.get(msg.get("command_id"))
            if cancel is not None:
                cancel.set()
                self._emit({"event": "command_cancelled",
                            "command_id": msg.get("command_id")})
        elif mtype == p.COMP_UPDATE:
            self.compensation_ms = msg.get("compensation_ms", self.compensation_ms)
            self._emit({"event": "comp_changed",
                        "compensation_ms": self.compensation_ms})
        elif mtype == p.ERROR:
            self._emit({"event": "error", "code": msg.get("code"),
                        "message": msg.get("message")})

    # ------------------------------------------------------------------
    # 绝对时刻调度与触发（R-03 / R-04）
    # ------------------------------------------------------------------

    def _schedule_fire(self, msg: dict) -> None:
        command_id = msg.get("command_id")
        command = msg.get("command")
        at = msg.get("at")
        if not command_id or not isinstance(at, (int, float)):
            return

        offset = self.clock.offset_ms
        if offset is None:
            # 时钟未同步，无法调度；回报错误让主控端可见
            self._send_threadsafe({"type": p.CMD_RECEIPT, "command_id": command_id,
                                   "status": "error", "detail": "时钟未同步"})
            self._emit({"event": "fire_skipped", "command_id": command_id,
                        "reason": "时钟未同步"})
            return

        local_fire = at - offset - self.compensation_ms
        cancel = threading.Event()
        self._pending_fires[command_id] = cancel
        remaining_ms = local_fire - now_ms()
        self._emit({"event": "command_scheduled", "command_id": command_id,
                    "command": command, "at": at, "local_fire": local_fire,
                    "remaining_ms": round(remaining_ms, 1),
                    "offset_ms": round(offset, 1),
                    "compensation_ms": self.compensation_ms})

        thread = threading.Thread(target=self._fire_worker,
                                  args=(command_id, command, at, local_fire,
                                        offset, cancel),
                                  daemon=True, name=f"fire-{command_id[:8]}")
        thread.start()

    def _fire_worker(self, command_id: str, command: str, at: float,
                     local_fire: float, offset: float,
                     cancel: threading.Event) -> None:
        try:
            # R-08：执行时刻前 3 秒起"滴、滴、滴"倒数提示音
            if self.beep_fn is not None:
                beep_from = local_fire - BEEP_COUNTDOWN_MS
                if now_ms() < beep_from:
                    _, cancelled = precise_wait_until(beep_from, cancel)
                    if cancelled:
                        return
                beep_at = max(now_ms(), beep_from)
                for _ in range(3):
                    # 临近到点（<500ms）时停止提示，保证精确等待不被打断
                    if cancel.is_set() or local_fire - now_ms() < 500:
                        break
                    try:
                        self.beep_fn()
                    except Exception:
                        pass
                    beep_at += 1000
                    if beep_at >= local_fire:
                        break
                    _, cancelled = precise_wait_until(beep_at, cancel)
                    if cancelled:
                        return

            actual_local, cancelled = precise_wait_until(local_fire, cancel)
            self._pending_fires.pop(command_id, None)
            if cancelled:
                # command.cancel 消息到达时已发出 command_cancelled 事件，此处不再重复
                return

            status, detail = "ok", None
            if self.key_sender is not None:
                try:
                    self.key_sender.press_space()
                except Exception as exc:
                    status, detail = "error", str(exc)

            fired_at = round(actual_local + offset)  # 换算为服务器时钟（R-04）
            receipt = {"type": p.CMD_RECEIPT, "command_id": command_id,
                       "fired_at": fired_at, "status": status}
            if detail:
                receipt["detail"] = detail
            self._send_threadsafe(receipt)
            self._emit({"event": "command_fired", "command_id": command_id,
                        "command": command, "fired_at": fired_at,
                        "delta_ms": round(fired_at - at, 1), "status": status})
        finally:
            self._pending_fires.pop(command_id, None)

    # ------------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        try:
            self.on_event(event)
        except Exception:
            logger.exception("on_event 回调异常")
