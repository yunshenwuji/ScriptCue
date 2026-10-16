"""ScriptCue 服务端入口。

启动方式（开发）：
    cd server && python -m uvicorn server.main:app --host 0.0.0.0 --port 8000

环境变量：
    SC_DATA_DIR          数据目录（审计日志、更新清单缓存），默认 <repo>/server/data，Docker 内为 /app/data
    SC_CONTROLLER_DIR    主控端静态目录，默认 <repo>/controller
    SC_DEFAULT_LEAD_MS   指令默认提前量（毫秒），默认 3000
    SC_UPDATE_MANIFEST_URL    CDN 上的 update-manifest.json 地址（更新通道开关之一）
    SC_UPDATE_MANIFEST_FILE   本地清单文件，存在且可解析时完全接管并停用 CDN 刷新（钉版本）
    SC_UPDATE_ASSET_BASE_URL  产物 CDN 根地址（如 https://cdn.example.com/app/）
    SC_UPDATE_REFRESH_H       清单刷新周期（小时），默认 24
    SC_UPDATE_DISABLED=1      整体关闭更新通道

更新通道细节见 docs/auto-update.md（含发布顺序硬约束）。
"""

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import protocol as p
from .audit import AuditLog
from .models import RoomManager
from .timebase import now_ms
from .updatefeed import UpdateFeed
from .ws import handle_ws, push_update_if_behind

logging.basicConfig(level=os.environ.get("SC_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("scriptcue")

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("SC_DATA_DIR", REPO_ROOT / "server" / "data"))
CONTROLLER_DIR = Path(os.environ.get("SC_CONTROLLER_DIR", REPO_ROOT / "controller"))


def _refresh_interval_s() -> float:
    """SC_UPDATE_REFRESH_H（小时）→ 秒；非法值退回默认 24h。"""
    try:
        hours = float(os.environ.get("SC_UPDATE_REFRESH_H", "24"))
    except ValueError:
        hours = 24.0
    return max(hours, 0.05) * 3600


manager = RoomManager()
audit = AuditLog(DATA_DIR / "audit.jsonl")
update_feed = UpdateFeed(DATA_DIR / "update-cache.json")


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


async def _push_updates_to_online() -> None:
    """遍历全部在线会话补推更新通知（按会话去重，重复调用无副作用）。"""
    for room in list(manager.rooms.values()):
        for session in list(room.agents.values()):
            try:
                await push_update_if_behind(room, session, update_feed, audit)
            except Exception:  # noqa: BLE001 - 更新通道不得影响房间主流程
                logger.exception("推送更新通知失败: %s/%s", room.code, session.session_id)


async def _update_feed_loop() -> None:
    """启动即刷一次清单，之后每 SC_UPDATE_REFRESH_H 小时刷一次。

    刷新后向全部在线被控端补推 update.available（按版本去重）：覆盖服务器重启、
    CDN 后于被控端上线等场景；清单来源文件或 CDN 的失败都只记日志。
    """
    interval_s = _refresh_interval_s()
    while True:
        result = await asyncio.to_thread(update_feed.refresh)
        audit.log("update_manifest_refresh", source=result["source"],
                  version=result["version"], ok=result["ok"])
        if result["ok"]:
            await _push_updates_to_online()
        await asyncio.sleep(interval_s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(_offline_sweep()), asyncio.create_task(_idle_sweep())]
    if update_feed.enabled():
        logger.info("更新通道已启用（清单源: %s）",
                    update_feed.override_file or update_feed.manifest_url)
        tasks.append(asyncio.create_task(_update_feed_loop()))
    else:
        logger.info("更新通道未配置，跳过（/update/manifest.json 将返回 404）")
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
        "update_feed": update_feed.status(),
    }


# 更新清单端点。必须注册在底部 StaticFiles mount 之前（现有 mount 在文件末尾，
# 本路由靠前注册即优先匹配）；无可用清单时返回 404 + {"error": "no_manifest"}。
@app.get("/update/manifest.json")
async def update_manifest():
    served = update_feed.serve()
    if served is None:
        return JSONResponse({"error": "no_manifest"}, status_code=404)
    return served


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await handle_ws(ws, manager, audit, update_feed)


# 主控端静态页面（放在最后，兜底匹配 /）
if CONTROLLER_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(CONTROLLER_DIR), html=True), name="controller")
else:  # pragma: no cover - 部署缺失前端时的保护
    logger.warning("主控端目录不存在: %s，仅提供 WebSocket 与 /healthz", CONTROLLER_DIR)
