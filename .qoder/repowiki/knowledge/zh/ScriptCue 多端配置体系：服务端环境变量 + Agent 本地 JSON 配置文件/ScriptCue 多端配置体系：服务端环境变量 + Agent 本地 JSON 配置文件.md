---
kind: configuration_system
name: ScriptCue 多端配置体系：服务端环境变量 + Agent 本地 JSON 配置文件
category: configuration_system
scope:
    - '**'
source_files:
    - server/server/main.py
    - agent/agent/gui.py
    - docker-compose.yml
    - server/Dockerfile
    - controller/app.js
---

## 1. 整体方案

本仓库没有统一的配置中心，而是按组件分别采用两种轻量级方式：
- **服务端（server）**：通过 Python `os.environ` 读取环境变量进行部署/运行期配置，配合 `docker-compose.yml` 注入卷挂载。
- **Agent 被控端（agent）**：通过 GUI 持久化一个用户可编辑的 JSON 配置文件到操作系统标准应用数据目录。
- **主控端（controller）**：纯前端静态页面，无外部配置；连接地址由浏览器当前 host/port 推导，运行时参数通过 UI 输入框传递。

## 2. 关键文件与位置

- `server/server/main.py`：FastAPI 入口，集中声明所有环境变量及默认值。
- `agent/agent/gui.py`：Agent GUI 层，实现配置的加载、保存与用户界面绑定。
- `docker-compose.yml`：为 server 提供数据卷挂载（`scriptcue-data:/app/data`）。
- `server/Dockerfile`：容器镜像定义（配合 compose 使用）。
- `controller/app.js`：主控端 JS，仅硬编码常量（如 `RECEIPT_TIMEOUT_MS=8000`、`HOLD_MS=1000`），不读外部配置。

## 3. 架构与约定

### 3.1 服务端配置（环境变量 + 默认路径）

`server/server/main.py` 在模块顶层以注释形式声明了全部可配置项，并在启动时读取：

| 变量名 | 含义 | 默认值 |
|---|---|---|
| `SC_DATA_DIR` | 审计日志等数据目录 | `<repo>/server/data`（Docker 内 `/app/data`） |
| `SC_CONTROLLER_DIR` | 主控端静态 HTML/CSS 目录 | `<repo>/controller` |
| `SC_DEFAULT_LEAD_MS` | 指令默认提前量（毫秒） | `3000` |
| `SC_LOG_LEVEL` | Python logging 级别 | `INFO` |

这些变量通过 `os.environ.get(..., default)` 读取，未设置时使用基于 `Path(__file__).resolve().parents[2]` 计算的相对路径作为默认值。服务启动后通过 `lifespan` 打印实际使用的 `CONTROLLER_DIR` 和 `DATA_DIR`，便于运维确认。

### 3.2 Agent 本地配置文件（JSON 持久化）

`agent/agent/gui.py` 中 `config_path()` 根据平台选择存储位置：
- Windows：`%APPDATA%/ScriptCue/agent_config.json`
- 其他系统：`~/.config/ScriptCue/agent_config.json`

`load_config()` 从该 JSON 文件读取字典，若文件不存在或解析失败则返回空 `{}`；`save_config()` 写入并自动创建父目录。GUI 启动时调用 `load_config()`，后续对 `server`、`room`、`nickname`、`compensation`、`topmost` 等字段的修改通过 `_save_cfg()` 调用 `save_config()` 落盘。

### 3.3 主控端（无配置）

`controller/app.js` 是单文件原生 JavaScript，无任何配置加载逻辑。WebSocket 地址通过 `wsUrl()` 函数基于 `location.protocol` 与 `location.host` 动态构造（`ws://host/ws` 或 `wss://host/ws`），因此部署时只需把该静态页面放到服务器根路径即可。

### 3.4 容器化配置

`docker-compose.yml` 将 `server/Dockerfile` 构建出的镜像暴露端口 `8000`，并通过命名卷 `scriptcue-data` 持久化 `/app/data`（对应服务端的 `SC_DATA_DIR` 默认值）。未显式传入环境变量，因此服务使用代码中的默认路径。

## 4. 约定与约束

- **服务端只认环境变量**：所有可调参数均通过 `SC_*` 前缀的环境变量注入，不在代码中提供命令行参数或配置文件。
- **Agent 配置即用户偏好**：`agent_config.json` 仅保存 GUI 交互产生的用户偏好（服务器地址、房间码、昵称、补偿值、窗口置顶），不包含敏感信息；读写失败会被静默忽略（`except OSError: pass`）。
- **主控端零配置**：通过浏览器上下文推断连接地址，避免部署时额外维护配置文件。
- **日志级别可通过环境变量覆盖**：`SC_LOG_LEVEL` 直接传给 `logging.basicConfig(level=...)`。
- **数据目录必须存在**：服务端启动时不会主动创建 `SC_DATA_DIR`，但 `AuditLog` 会在首次写入时依赖底层文件系统权限；compose 通过 volume 保证 `/app/data` 存在。
- **协议版本硬编码**：`controller/app.js` 中 `PROTO = 1` 与服务端 `p.PROTO_VERSION` 需保持一致，属于协议约定的“配置”而非运行时配置项。

## 5. 适用性说明

本仓库确实实现了跨组件的配置体系，但规模较小且分散：服务端用环境变量，Agent 用本地 JSON 文件，主控端无配置。没有统一配置框架、schema 校验或密钥管理，适合小型部署场景。