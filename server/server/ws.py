"""WebSocket 会话处理：主控端与被控端共用 /ws 端点，首条消息声明角色。

流程与消息定义见 docs/protocol.md。
"""

import asyncio
import contextlib
import json
import logging
import uuid

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from . import protocol as p
from .audit import AuditLog
from .models import Room, RoomFullError, RoomManager
from .timebase import now_ms

logger = logging.getLogger("scriptcue.ws")

FIRST_MESSAGE_TIMEOUT_S = 15
# 补偿值允许范围（毫秒）
COMP_MIN, COMP_MAX = -10000, 10000
# 提前量允许范围（毫秒）
LEAD_MIN, LEAD_MAX = 500, 60000


async def _send(ws: WebSocket, msg: dict) -> bool:
    try:
        await ws.send_json(msg)
        return True
    except Exception:
        return False


async def _send_error(ws: WebSocket, code: str, message: str) -> None:
    await _send(ws, {"type": p.ERROR, "code": code, "message": message})


@contextlib.asynccontextmanager
async def _close_on_exit(ws: WebSocket):
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


async def _recv_json(ws: WebSocket) -> dict | None:
    """收一条 JSON 消息；格式非法时回错误并返回 None（连接保持）。"""
    raw = await ws.receive_text()
    try:
        msg = json.loads(raw)
        if not isinstance(msg, dict):
            raise ValueError
        return msg
    except (json.JSONDecodeError, ValueError):
        await _send_error(ws, p.ERR_BAD_MESSAGE, "消息必须是 JSON 对象")
        return None


# ---------------------------------------------------------------------------
# 主控端会话
# ---------------------------------------------------------------------------

async def _controller_handle_command(ws, room: Room, msg: dict, audit: AuditLog) -> None:
    command = msg.get("command")
    if command not in p.VALID_COMMANDS:
        await _send_error(ws, p.ERR_BAD_COMMAND, f"未知指令: {command}")
        return
    try:
        lead_ms = int(msg.get("lead_ms") or room.lead_ms)
    except (TypeError, ValueError):
        lead_ms = room.lead_ms
    lead_ms = max(LEAD_MIN, min(LEAD_MAX, lead_ms))

    command_id = msg.get("command_id") or uuid.uuid4().hex
    target_id = msg.get("target")
    target_session = None
    if target_id is not None:
        target_session = room.agents.get(target_id)
        if target_session is None:
            await _send_error(ws, p.ERR_NO_SUCH_AGENT, f"设备不存在: {target_id}")
            return

    at = now_ms() + lead_ms
    room.pending_commands[command_id] = {"command": command, "at": at, "target": target_id}

    exec_msg = {"type": p.CMD_EXEC, "command_id": command_id, "command": command, "at": at}
    if target_session is not None:
        target_session.pending_commands[command_id] = at
        await target_session.send(exec_msg)
    else:
        for agent in room.agents.values():
            if agent.online:
                agent.pending_commands[command_id] = at
        await room.broadcast_agents(exec_msg)

    await _send(ws, {"type": p.CMD_SCHEDULED, "command_id": command_id,
                     "command": command, "at": at, "lead_ms": lead_ms,
                     "target": target_id})
    audit.log("command", room=room.code, command_id=command_id, command=command,
              at=at, lead_ms=lead_ms, target=target_id)
    asyncio.create_task(_cleanup_pending(room, command_id, at))


async def _cleanup_pending(room: Room, command_id: str, at: int) -> None:
    """指令到期并超过回执窗口后，清理待执行记录。"""
    delay_ms = at + p.RECEIPT_TIMEOUT_MS - now_ms()
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)
    room.pending_commands.pop(command_id, None)
    for agent in room.agents.values():
        agent.pending_commands.pop(command_id, None)


async def _controller_handle_cancel(ws, room: Room, msg: dict, audit: AuditLog) -> None:
    command_id = msg.get("command_id")
    info = room.pending_commands.get(command_id)
    if not info or info["at"] <= now_ms():
        return  # 指令不存在或已到期，静默忽略
    del room.pending_commands[command_id]
    for agent in room.agents.values():
        agent.pending_commands.pop(command_id, None)
    cancel_msg = {"type": p.CMD_CANCEL, "command_id": command_id}
    target_id = info.get("target")
    if target_id:
        target_session = room.agents.get(target_id)
        if target_session:
            await target_session.send(cancel_msg)
    else:
        await room.broadcast_agents(cancel_msg)
    await _send(ws, {"type": p.CMD_CANCELLED, "command_id": command_id})
    audit.log("cancel", room=room.code, command_id=command_id)


async def _controller_handle_set_comp(ws, room: Room, msg: dict, audit: AuditLog) -> None:
    session = room.agents.get(msg.get("session_id"))
    if session is None:
        await _send_error(ws, p.ERR_NO_SUCH_AGENT, "设备不存在")
        return
    try:
        comp = int(msg.get("compensation_ms"))
    except (TypeError, ValueError):
        await _send_error(ws, p.ERR_BAD_COMMAND, "补偿值必须是整数毫秒")
        return
    comp = max(COMP_MIN, min(COMP_MAX, comp))
    session.compensation_ms = comp
    await session.send({"type": p.COMP_UPDATE, "compensation_ms": comp})
    await room.notify_controller({"type": p.AGENT_UPDATED, "agent": session.state()})
    audit.log("set_comp", room=room.code, session=session.session_id, compensation_ms=comp)


async def run_controller_session(ws: WebSocket, manager: RoomManager,
                                 audit: AuditLog, first: dict) -> None:
    mtype = first["type"]
    room: Room | None = None

    if mtype == p.CTRL_CREATE:
        room = manager.create_room(first.get("room_name") or "", first.get("password"))
    elif mtype == p.CTRL_JOIN:
        room = manager.get(first.get("room_code") or "")
        if room is None:
            await _send_error(ws, p.ERR_ROOM_NOT_FOUND, "房间不存在")
            return
        if room.password and first.get("password") != room.password:
            await _send_error(ws, p.ERR_BAD_PASSWORD, "房间口令错误")
            return
    else:  # CTRL_RESUME
        room = manager.get(first.get("room_code") or "")
        if room is None or first.get("token") != room.controller_token:
            await _send_error(ws, p.ERR_ROOM_NOT_FOUND, "会话已失效，请重新创建或加入房间")
            return

    # 主控端被顶替：新连接接管
    if room.controller_ws is not None and room.controller_ws is not ws:
        with contextlib.suppress(Exception):
            await room.controller_ws.close()
    room.controller_ws = ws
    room.touch()

    await _send(ws, {"type": p.CTRL_JOINED, "room_code": room.code, "room_name": room.name,
                     "token": room.controller_token, "server_time": now_ms(),
                     "agents": room.agent_states()})
    audit.log("controller_join", room=room.code)

    try:
        while True:
            msg = await _recv_json(ws)
            if msg is None:
                continue
            room.touch()
            mtype = msg.get("type")
            if mtype == p.CTRL_COMMAND:
                await _controller_handle_command(ws, room, msg, audit)
            elif mtype == p.CTRL_CANCEL:
                await _controller_handle_cancel(ws, room, msg, audit)
            elif mtype == p.CTRL_SET_COMP:
                await _controller_handle_set_comp(ws, room, msg, audit)
            else:
                await _send_error(ws, p.ERR_BAD_MESSAGE, f"主控端不支持的消息类型: {mtype}")
    except WebSocketDisconnect:
        pass
    finally:
        if room.controller_ws is ws:
            room.controller_ws = None
        audit.log("controller_leave", room=room.code)


# ---------------------------------------------------------------------------
# 被控端会话
# ---------------------------------------------------------------------------

async def _agent_handle_heartbeat(room: Room, session, msg: dict) -> None:
    session.ready = bool(msg.get("ready"))
    quality = msg.get("clock_quality")
    if quality in (p.QUALITY_EXCELLENT, p.QUALITY_GOOD, p.QUALITY_POOR, p.QUALITY_NONE):
        session.clock_quality = quality
    offset = msg.get("clock_offset_ms")
    rtt = msg.get("clock_rtt_ms")
    if isinstance(offset, (int, float)):
        session.clock_offset_ms = float(offset)
    if isinstance(rtt, (int, float)):
        session.clock_rtt_ms = float(rtt)
    await room.notify_controller({"type": p.AGENT_UPDATED, "agent": session.state()})


async def _agent_handle_receipt(room: Room, session, msg: dict, audit: AuditLog) -> None:
    command_id = msg.get("command_id")
    fired_at = msg.get("fired_at")
    status = msg.get("status") or "ok"
    at = session.pending_commands.pop(command_id, None)
    if at is None:
        info = room.pending_commands.get(command_id)
        at = info["at"] if info else None
    delta_ms = int(fired_at - at) if (at is not None and isinstance(fired_at, (int, float))) else None
    await room.notify_controller({
        "type": p.CMD_RECEIPT, "session_id": session.session_id,
        "nickname": session.nickname, "command_id": command_id,
        "fired_at": fired_at, "delta_ms": delta_ms, "status": status,
    })
    audit.log("receipt", room=room.code, session=session.session_id,
              command_id=command_id, fired_at=fired_at, delta_ms=delta_ms, status=status)


async def run_agent_session(ws: WebSocket, manager: RoomManager,
                            audit: AuditLog, first: dict) -> None:
    code = (first.get("room_code") or "").strip().upper()
    token = first.get("token")
    room = manager.get(code)
    session = None

    if room is None:
        if first.get("auto_create"):
            # 服务器重启后客户端自动恢复房间
            room = manager.create_room(first.get("room_name") or code, None, code=code)
            audit.log("room_recreated", room=code)
        else:
            await _send_error(ws, p.ERR_ROOM_NOT_FOUND, "房间不存在")
            return
    else:
        if token:
            session = room.find_session_by_token(token)
        if session is None and room.password and first.get("password") != room.password:
            await _send_error(ws, p.ERR_BAD_PASSWORD, "房间口令错误")
            return

    if session is None:
        nickname = (first.get("nickname") or "").strip()[:32] or "未命名设备"
        try:
            session = room.add_agent(nickname)
        except RoomFullError:
            await _send_error(ws, p.ERR_ROOM_FULL, "房间设备数已达上限")
            return
        # 新会话恢复客户端本地补偿值（服务器重启重建房间时保留校准结果）
        join_comp = first.get("compensation_ms")
        if isinstance(join_comp, (int, float)):
            session.compensation_ms = max(COMP_MIN, min(COMP_MAX, int(join_comp)))
        resumed = False
    else:
        resumed = True

    # 旧连接顶替
    if session.ws is not None and session.ws is not ws:
        with contextlib.suppress(Exception):
            await session.ws.close()
    session.bind(ws)
    room.touch()

    await _send(ws, {"type": p.AGENT_JOINED, "room_code": room.code,
                     "token": session.token, "server_time": now_ms(),
                     "compensation_ms": session.compensation_ms,
                     "lead_ms": room.lead_ms, "resumed": resumed})
    await room.notify_controller({"type": p.AGENT_UPDATED, "agent": session.state()})
    audit.log("agent_join", room=room.code, session=session.session_id,
              nickname=session.nickname)

    try:
        while True:
            msg = await _recv_json(ws)
            if msg is None:
                continue
            room.touch()
            session.last_seen_ms = now_ms()
            mtype = msg.get("type")
            if mtype == p.CLOCK_SYNC_REQ:
                # 授时回复不做任何排队，立即返回
                await _send(ws, {"type": p.CLOCK_SYNC_RES, "id": msg.get("id"),
                                 "t0": msg.get("t0"), "ts": now_ms()})
            elif mtype == p.AGENT_HEARTBEAT:
                await _agent_handle_heartbeat(room, session, msg)
            elif mtype == p.AGENT_SET_COMP:
                try:
                    comp = max(COMP_MIN, min(COMP_MAX, int(msg.get("compensation_ms"))))
                except (TypeError, ValueError):
                    await _send_error(ws, p.ERR_BAD_MESSAGE, "补偿值必须是整数毫秒")
                    continue
                session.compensation_ms = comp
                await _send(ws, {"type": p.COMP_UPDATE, "compensation_ms": comp})
                await room.notify_controller({"type": p.AGENT_UPDATED, "agent": session.state()})
                audit.log("set_comp", room=room.code, session=session.session_id,
                          compensation_ms=comp, source="agent")
            elif mtype == p.CMD_RECEIPT:
                await _agent_handle_receipt(room, session, msg, audit)
            else:
                await _send_error(ws, p.ERR_BAD_MESSAGE, f"被控端不支持的消息类型: {mtype}")
    except WebSocketDisconnect:
        pass
    finally:
        if session.ws is ws:
            session.ws = None
            session.online = False
        await room.notify_controller({"type": p.AGENT_UPDATED, "agent": session.state()})
        audit.log("agent_disconnect", room=room.code, session=session.session_id)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

async def handle_ws(ws: WebSocket, manager: RoomManager, audit: AuditLog) -> None:
    await ws.accept()
    async with _close_on_exit(ws):
        try:
            first_raw = await asyncio.wait_for(ws.receive_text(), FIRST_MESSAGE_TIMEOUT_S)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            return
        try:
            first = json.loads(first_raw)
            if not isinstance(first, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            await _send_error(ws, p.ERR_BAD_MESSAGE, "首条消息必须是 JSON 对象")
            return

        if first.get("proto") != p.PROTO_VERSION:
            await _send_error(ws, p.ERR_BAD_PROTO, f"协议版本不匹配，服务端要求 v{p.PROTO_VERSION}")
            return

        mtype = first.get("type")
        if mtype in (p.CTRL_CREATE, p.CTRL_JOIN, p.CTRL_RESUME):
            await run_controller_session(ws, manager, audit, first)
        elif mtype == p.AGENT_JOIN:
            await run_agent_session(ws, manager, audit, first)
        else:
            await _send_error(ws, p.ERR_BAD_MESSAGE, f"未知的首条消息类型: {mtype}")
