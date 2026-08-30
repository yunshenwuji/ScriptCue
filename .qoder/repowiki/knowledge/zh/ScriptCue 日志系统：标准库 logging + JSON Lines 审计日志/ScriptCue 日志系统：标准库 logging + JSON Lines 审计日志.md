---
kind: logging_system
name: ScriptCue 日志系统：标准库 logging + JSON Lines 审计日志
category: logging_system
scope:
    - '**'
source_files:
    - server/server/main.py
    - server/server/audit.py
    - agent/agent/engine.py
    - agent/agent/cli.py
---

## 1. 使用的系统与框架

- Python 端统一使用标准库 `logging`，未引入第三方日志框架（如 loguru、structlog）。
- 服务端通过 `logging.basicConfig` 一次性配置根 logger，控制台输出采用固定格式 `%(asctime)s %(name)s %(levelname)s %(message)s`。
- 操作审计日志使用自定义的 `AuditLog` 类，以 **JSON Lines** 格式追加写入文件，独立于运行期日志。
- Agent（被控端）CLI 使用 `print` 直接输出带时间戳的人类可读消息；GUI 模式则通过 `on_event` 回调将结构化事件交给上层渲染，不直接写日志。
- 主控端 Web（controller/）为纯前端 JavaScript，无后端日志逻辑，仅依赖浏览器控制台输出。

## 2. 关键文件与位置

| 文件 | 作用 |
|---|---|
| `server/server/main.py` | 调用 `logging.basicConfig` 设置全局日志级别（默认 INFO），创建根 logger `scriptcue`，并启动审计日志 |
| `server/server/audit.py` | `AuditLog` 类：线程安全地以 JSON Lines 格式写入 `server/data/audit.jsonl`，记录指令下发与触发回执 |
| `agent/agent/engine.py` | 获取命名 logger `scriptcue.agent`，用于记录会话异常、回调异常等运行时信息 |
| `agent/agent/cli.py` | CLI 入口，使用 `print` + 本地时间戳 `_ts()` 输出交互式状态，不经过 `logging` |
| `server/server/ws.py`、`server/server/models.py` 等 | 通过注入的 `audit.log(...)` 记录业务事件 |

## 3. 架构与约定

### 3.1 命名空间划分
- 服务端根 logger 命名为 `scriptcue`，由 `main.py` 初始化。
- 被控端引擎使用命名 logger `scriptcue.agent`，便于区分来源。
- 审计日志使用独立 logger `scriptcue.audit`，专门记录审计写入失败等底层错误。

### 3.2 日志级别策略
- 服务端的日志级别由环境变量 `SC_LOG_LEVEL` 控制，默认 `INFO`。该变量名在 `main.py` 中硬编码读取，是唯一的级别配置入口。
- 代码中实际使用的级别包括：
  - `logger.info`：设备心跳超时离线、房间空闲销毁、服务启动信息等常规运行事件。
  - `logger.warning`：会话异常、主控端目录缺失等需要关注但不阻断运行的情况。
  - `logger.exception`：审计日志写入失败、`on_event` 回调抛出异常时，附带 traceback。
- Agent 侧没有对 `logging` 做级别配置，仅使用 `warning`/`exception` 输出到已配置的 root handler。

### 3.3 结构化字段
- 运行期日志使用 `logger.info/warning("... %s", ...)` 的字符串格式化方式，字段内嵌在消息文本中，并非结构化 JSON 日志。
- 审计日志是仓库中唯一结构化的日志：每条记录形如 `{"ts": <毫秒时间>, "event": <事件名>, ...附加字段}`，通过 `json.dumps(record, ensure_ascii=False)` 写入单行 JSON。
- 审计事件类型包括：`agent_offline`、`room_expired` 以及后续扩展的指令下发/回执事件（由 `ws.py`、`models.py` 调用 `audit.log` 写入）。

### 3.4 输出目标
- 运行期日志：stdout/stderr（由 `basicConfig` 默认 handler 输出到控制台）。
- 审计日志：文件 `server/data/audit.jsonl`，路径由环境变量 `SC_DATA_DIR` 决定，默认位于仓库 `server/data` 下；Docker 部署时映射到 `/app/data`。
- Agent CLI：直接 `print` 到终端，用于调试和交互；GUI 模式下通过 `on_event` 回调把结构化事件交给 GUI 层展示。

### 3.5 并发与可靠性
- `AuditLog` 内部使用 `threading.Lock` 保护文件句柄的写入，确保多线程并发安全。
- 写入失败时捕获 `OSError` 并通过 `logger.exception` 记录错误，避免审计写入阻塞主流程。
- 服务生命周期结束时（`lifespan` 退出）调用 `audit.close()` 关闭文件句柄。

## 4. 约定与约束

- **日志级别配置**：服务端的日志级别必须通过环境变量 `SC_LOG_LEVEL` 设置，代码中未提供其他配置入口（`main.py` 第 28 行）。Agent 侧不配置级别，继承 root logger。
- **审计日志格式**：所有审计记录必须是 JSON Lines，每行一个 JSON 对象，包含必填字段 `ts`（毫秒时间戳）和 `event`（事件名），其余字段按业务语义追加（`audit.py` 第 24–26 行强制构造该结构）。
- **审计写入不可中断**：`AuditLog.log` 使用锁串行化写入，即使发生 IO 错误也仅记录异常而不抛错，保证业务逻辑不被审计写入阻塞。
- **Agent 交互输出**：CLI 版不使用 `logging` 模块，而是通过 `print` + `_ts()` 时间戳输出人类可读消息；GUI 版通过 `on_event` 回调传递结构化事件字典，由上层负责渲染。
- **命名规范**：新增模块若需记录运行日志，应使用 `logging.getLogger("scriptcue.<模块名>")` 形式的命名 logger，以便通过 logger name 过滤。
- **Web 前端**：主控端为纯静态页面（`controller/index.html` + `app.js`），无服务器端日志；如需调试可借助浏览器控制台，但不在本仓库范围内。

## 5. 适用性说明

本仓库存在明确的日志体系：Python 端基于标准库 `logging` 的输出 + 独立的 JSON Lines 审计日志子系统，覆盖服务端与被控端的核心运行事件与操作回放需求，因此该类别适用。