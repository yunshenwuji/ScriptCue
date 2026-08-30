"""房间与会话模型（纯内存存储）。

服务器重启后房间丢失，由客户端重连时凭 auto_create / 重新创建恢复。
"""

import random
import secrets

from . import protocol as p
from .timebase import now_ms


class RoomFullError(Exception):
    pass


class AgentSession:
    """一台被控端设备的会话。断线后会话保留，凭令牌恢复。"""

    def __init__(self, session_id: str, nickname: str):
        self.session_id = session_id
        self.nickname = nickname
        self.token = secrets.token_urlsafe(16)
        self.ws = None
        self.online = False
        self.ready = False
        self.clock_quality = p.QUALITY_NONE
        self.clock_offset_ms: float | None = None
        self.clock_rtt_ms: float | None = None
        self.compensation_ms = 0
        self.last_seen_ms = now_ms()
        # 已下发、等待触发或回执的指令：command_id -> 执行时刻 at
        self.pending_commands: dict[str, int] = {}

    def bind(self, ws) -> None:
        self.ws = ws
        self.online = True
        self.last_seen_ms = now_ms()

    def state(self) -> dict:
        """协议中的 AgentState 结构。"""
        return {
            "session_id": self.session_id,
            "nickname": self.nickname,
            "online": self.online,
            "ready": self.ready,
            "clock_quality": self.clock_quality,
            "clock_offset_ms": self.clock_offset_ms,
            "clock_rtt_ms": self.clock_rtt_ms,
            "compensation_ms": self.compensation_ms,
            "last_seen": self.last_seen_ms,
        }

    async def send(self, msg: dict) -> bool:
        if self.ws is None:
            return False
        try:
            await self.ws.send_json(msg)
            return True
        except Exception:
            return False


class Room:
    def __init__(self, code: str, name: str, password: str | None,
                 lead_ms: int = p.DEFAULT_LEAD_MS):
        self.code = code
        self.name = name or code
        self.password = password or None
        self.lead_ms = lead_ms
        self.created_at_ms = now_ms()
        self.last_active_ms = now_ms()
        self.controller_ws = None
        self.controller_token = secrets.token_urlsafe(16)
        self.agents: dict[str, AgentSession] = {}
        # 全体待执行指令：command_id -> {command, at, target}
        self.pending_commands: dict[str, dict] = {}
        self._agent_seq = 0

    def touch(self) -> None:
        self.last_active_ms = now_ms()

    def is_idle(self) -> bool:
        """无任何在线成员（含主控），可进入空闲销毁倒计时。"""
        if self.controller_ws is not None:
            return False
        return not any(a.online for a in self.agents.values())

    def add_agent(self, nickname: str) -> AgentSession:
        if len(self.agents) >= p.MAX_AGENTS_PER_ROOM:
            raise RoomFullError()
        self._agent_seq += 1
        session = AgentSession(f"agent-{self._agent_seq}", nickname)
        self.agents[session.session_id] = session
        return session

    def find_session_by_token(self, token: str) -> AgentSession | None:
        return next((a for a in self.agents.values() if a.token == token), None)

    async def broadcast_agents(self, msg: dict, session: AgentSession | None = None) -> None:
        """广播给房间内（在线的）被控端；指定 session 时只发给它。"""
        targets = [session] if session is not None else [
            a for a in self.agents.values() if a.online
        ]
        for agent in targets:
            await agent.send(msg)

    async def notify_controller(self, msg: dict) -> None:
        if self.controller_ws is None:
            return
        try:
            await self.controller_ws.send_json(msg)
        except Exception:
            pass

    def agent_states(self) -> list[dict]:
        return [a.state() for a in self.agents.values()]


class RoomManager:
    def __init__(self):
        self.rooms: dict[str, Room] = {}

    @staticmethod
    def _generate_code(existing: set[str]) -> str:
        while True:
            code = "".join(random.choices(p.ROOM_CODE_ALPHABET, k=p.ROOM_CODE_LENGTH))
            if code not in existing:
                return code

    def create_room(self, name: str, password: str | None = None,
                    code: str | None = None,
                    lead_ms: int = p.DEFAULT_LEAD_MS) -> Room:
        """创建房间。指定 code 时用于服务器重启后客户端按原房间码重建。"""
        code = code or self._generate_code(set(self.rooms))
        if code in self.rooms:
            return self.rooms[code]
        room = Room(code, name, password, lead_ms)
        self.rooms[code] = room
        return room

    def get(self, code: str) -> Room | None:
        return self.rooms.get(code)

    def remove(self, code: str) -> None:
        self.rooms.pop(code, None)

    def sweep_expired(self, now: int) -> list[str]:
        """销毁空闲超过 24h 的房间，返回被销毁的房间码。"""
        expired = [
            code for code, room in self.rooms.items()
            if room.is_idle() and now - room.last_active_ms > p.ROOM_IDLE_TTL_S * 1000
        ]
        for code in expired:
            del self.rooms[code]
        return expired
