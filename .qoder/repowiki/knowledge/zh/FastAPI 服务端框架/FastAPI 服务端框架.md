---
kind: external_dependency
name: FastAPI 服务端框架
slug: fastapi
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

### FastAPI
- 角色：ScriptCue 云端服务的 Web 框架，承载 WebSocket 会话、房间管理、指令广播与静态主控页托管。
- 集成点：`server/server/main.py` 通过 `uvicorn` 启动；依赖声明在 `server/requirements.txt`（`fastapi>=0.115,<1.0`）。
- 使用方式：作为轻量级 ASGI 应用运行，配合 Uvicorn 提供 HTTP + WebSocket 服务；房间数据为纯内存，重启后由客户端凭房间码重建。
- 方向：后续若迁移到 Node.js 或其他框架，需保持 `/ws` 端点及协议文档中的消息契约不变。