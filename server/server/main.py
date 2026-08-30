"""ScriptCue 服务端入口。

启动方式（开发）：
    cd server && python -m uvicorn server.main:app --host 0.0.0.0 --port 8000

环境变量：
    SC_DATA_DIR        数据目录（审计日志），默认 <repo>/server/data，Docker 内为 /app/data
    SC_CONTROLLER_DIR  主控端静态目录，默认 <repo>/controller
    SC_DEFAULT_LEAD_MS 指令默认提前量（毫秒），默认 3000
"""

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from . import protocol as p
from .audit import AuditLog
from .models import RoomManager
from .timebase import now_ms
from .ws import handle_ws

logging.basicConfig(level=os.environ.get("SC_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("scriptcue")

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("SC_DATA_DIR", REPO_ROOT / "server" / "data"))
CONTROLLER_DIR = Path(os.environ.get("SC_CONTROLLER_DIR", REPO_ROOT / "controller"))
if os.environ.get("SC_DEFAULT_LEAD_MS"):
    p.DEFAULT_LEAD_MS = int(os.environ["SC_DEFAULT_LEAD_MS"])

manager = RoomManager()
audit = AuditLog(DATA_DIR / "audit.jsonl")


async def _offline_sweep() -> None:
    """每秒检查一次：心跳超时（3 次未响应）的被控端标记离线并通知主控。"""
    while True:
        await asyncio.sleep(1)
        now = now_ms()
        for room in list(manager.rooms.values()):
            for agent in room.agents.values():
                if agent.online and now - agent.last_seen_ms > p.AGENT_OFFLINE_S * 1000:
                    agent.online = False
                    logger.info("设备心跳超时离线: %s/%s", room.code, agent.nickname)
                    await room.notify_controller({"type": p.AGENT_UPDATED, "agent": agent.state()})
                    audit.log("agent_offline", room=room.code, session=agent.session_id)


async def _idle_sweep() -> None:
    """每分钟检查一次：空闲超过 24h 的房间销毁（S-01）。"""
    while True:
        await asyncio.sleep(60)
        for code in manager.sweep_expired(now_ms()):
            logger.info("房间空闲超时销毁: %s", code)
            audit.log("room_expired", room=code)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(_offline_sweep()), asyncio.create_task(_idle_sweep())]
    logger.info("ScriptCue 服务端已启动，主控端目录: %s，数据目录: %s", CONTROLLER_DIR, DATA_DIR)
    yield
    for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    audit.close()


app = FastAPI(title="ScriptCue Server", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "proto": p.PROTO_VERSION,
        "server_time": now_ms(),
        "rooms": len(manager.rooms),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await handle_ws(ws, manager, audit)


# 主控端静态页面（放在最后，兜底匹配 /）
if CONTROLLER_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(CONTROLLER_DIR), html=True), name="controller")
else:  # pragma: no cover - 部署缺失前端时的保护
    logger.warning("主控端目录不存在: %s，仅提供 WebSocket 与 /healthz", CONTROLLER_DIR)
