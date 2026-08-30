---
kind: error_handling
name: ScriptCue 多端 WebSocket 错误处理体系
category: error_handling
scope:
    - '**'
source_files:
    - server/server/ws.py
    - server/server/main.py
    - server/server/models.py
    - agent/agent/engine.py
    - agent/agent/cli.py
    - controller/app.js
---

## 1. 整体方案

ScriptCue 采用「服务端统一协议错误码 + 客户端差异化消费」的三层错误处理架构：
- **服务端（FastAPI）**：通过 `server/ws.py` 中的 `_send_error(ws, code, message)` 统一向 WebSocket 推送 `{type: "error", code, message}` 消息，所有业务分支（房间不存在、口令错误、指令非法、消息格式错误等）均走此路径。
- **被控端（AgentEngine）**：在 `agent/engine.py` 中解析首条应答，若收到 `type == p.ERROR` 则抛出 `ConnectionError` 触发指数退避重连；后续收到的 `error` 事件通过 `on_event` 回调上报给 CLI/GUI。
- **主控端（controller/app.js）**：在 `dispatch` 中单独处理 `case "error"`，根据 `code` 区分“会话失效”与“普通错误”，分别回到首页或显示 `home-error` 区域。

该设计遵循文档化协议（`docs/protocol.md`），错误码集中定义在 `server/server/protocol.py` 与 `agent/agent/protocol.py` 中，确保三端对同一错误的语义一致。

## 2. 关键文件与职责

| 文件 | 职责 |
|---|---|
| `server/server/ws.py` | WebSocket 会话入口，统一 `_recv_json` 校验 JSON、`_send_error` 回推错误、各 handler 按 `p.*_ERROR` 常量返回具体错误码 |
| `server/server/models.py` | 定义 `RoomFullError` 等领域异常，被 ws 捕获后转为协议错误码 |
| `server/server/main.py` | FastAPI 应用生命周期，后台任务（离线扫描、空闲房间销毁）使用 `contextlib.suppress(asyncio.CancelledError)` 优雅退出 |
| `agent/agent/engine.py` | 连接生命周期管理：捕获 `asyncio.CancelledError` 透传、其他异常记录日志并触发重连；调度线程内按键失败时回执 `status="error"` |
| `agent/agent/cli.py` / `gui.py` | 消费引擎 `on_event` 中的 `event: "error"`，转换为终端提示或 GUI 弹窗 |
| `controller/app.js` | 前端错误展示：`ws.onerror` 静默交由 `onclose` 重连；`handleServerError` 按 `room_not_found` 特殊处理；输入校验错误直接 `showHomeError` |

## 3. 架构与约定

### 3.1 服务端错误分类
- **协议层错误**：`ERR_BAD_MESSAGE`（非 JSON 对象）、`ERR_BAD_PROTO`（版本不匹配）、`ERR_BAD_COMMAND`（未知指令/参数类型错误）——由 `_recv_json` 与各 handler 的 `try/except (TypeError, ValueError)` 捕获。
- **资源/权限错误**：`ERR_ROOM_NOT_FOUND`、`ERR_BAD_PASSWORD`、`ERR_NO_SUCH_AGENT`、`ERR_ROOM_FULL` —— 对应业务状态检查失败。
- **运行时错误**：`WebSocketDisconnect` 在 `run_controller_session` / `run_agent_session` 外层 `except` 捕获后仅清理审计日志，不向上抛。

### 3.2 被控端重连策略
`AgentEngine.run()` 实现指数退避：捕获除 `CancelledError` 外的所有异常 → 发出 `event: "error"` → 设置 `connected=False` → 从 `p.RECONNECT_BACKOFF_S` 取 delay（上限 `RECONNECT_BACKOFF_MAX_S`）→ sleep 后重试。加入房间失败时抛出 `ConnectionError`，其 `message` 携带服务器返回的 `code` 与 `message`，用于判断是否自动重建房间（`auto_create`）。

### 3.3 指令执行错误链路
1. 服务端下发 `CMD_EXEC`，被控端调度到独立线程等待精确时刻。
2. 若时钟未同步，立即回执 `{status: "error", detail: "时钟未同步"}`。
3. 若 `KeySender.press_space()` 抛异常，回执 `{status: "error", detail: str(exc)}`。
4. 服务端将回执转发给主控端，前端以 `status="error"` 渲染为红色高亮行。

### 3.4 前端错误呈现约定
- 连接级错误：`ws.onerror` 空实现，依赖 `onclose` 的指数退避重连。
- 业务错误：`handleServerError` 将 `room_not_found` 视为会话失效（清 token 回首页），其余错误调用 `showHomeError(msg.message || msg.code)` 显示在首页错误区。
- 用户输入错误：如房间码格式不符，直接 `showHomeError("请输入 6 位房间码")`，不经过服务端。

## 4. 约束与规则

- **所有 WebSocket 错误必须经 `_send_error` 发送**，禁止直接 `raise` 中断连接（除 `WebSocketDisconnect` 外），保证客户端能收到结构化错误码。
- **JSON 解析失败不中断连接**：`_recv_json` 和 Agent 的消息循环中对 `json.JSONDecodeError` 选择 `continue` 或忽略，避免单条脏消息导致会话终止。
- **异步取消必须显式传播**：`AgentEngine._connect_and_serve` 中 `asyncio.CancelledError` 不被吞掉，而是 `raise` 让上层 `run()` 感知停止信号。
- **审计日志覆盖所有错误路径**：每个 `_send_error` 分支均有对应的 `audit.log` 调用，便于事后追溯。
- **心跳超时判定为离线而非错误**：`main.py` 的 `_offline_sweep` 将连续 3 次心跳超时标记 `online=False`，并通过 `AGENT_UPDATED` 通知主控端，不产生 `error` 消息。
- **补偿值范围强制钳制**：服务端与 Agent 两端均对 `compensation_ms` 做 `max(COMP_MIN, min(COMP_MAX, ...))` 钳制，超出范围即报 `ERR_BAD_MESSAGE`。

## 5. 缺失与局限

- 无全局异常中间件：FastAPI 未配置 `@app.exception_handler`，HTTP 层异常由框架默认处理，仅 `/healthz` 暴露健康信息。
- 无自定义 Exception 类族：除 `RoomFullError` 外，错误均以字符串 `code` + `message` 表达，未定义 Python 异常层次结构。
- 前端无 Promise 链式错误处理：纯原生 JS 使用 try/catch 与 `onerror` 回调，未引入统一错误拦截器。
