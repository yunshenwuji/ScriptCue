---
kind: build_system
name: 构建与发布系统：PyInstaller 桌面端打包 + Docker 服务端容器化
category: build_system
scope:
    - '**'
source_files:
    - agent/build_macos.sh
    - agent/build_windows.ps1
    - server/Dockerfile
    - docker-compose.yml
    - agent/requirements.txt
    - server/requirements.txt
    - .dockerignore
    - .gitignore
---

## 1. 使用的构建系统与工具

仓库采用**多组件、多平台**的构建策略，没有统一的 Makefile/CI 编排，而是按组件各自维护独立的构建脚本与容器定义：

- **被控端（Agent）**：使用 [PyInstaller](https://pyinstaller.org/) 将 Python 源码打包为原生可执行程序。
- **服务端（Server）**：基于 FastAPI + Uvicorn，通过 [Docker](https://www.docker.com/) 镜像化部署，并使用 `docker-compose` 编排。
- **主控端（Controller）**：纯静态 Web（HTML/CSS/JS），随服务端镜像一起分发，无需额外构建步骤。

依赖管理统一使用 `requirements.txt`（pip 风格），每个子项目独立维护。

## 2. 关键文件

| 文件 | 作用 |
|---|---|
| `agent/build_macos.sh` | macOS 下用 PyInstaller 打包为 `.app` 应用包 |
| `agent/build_windows.ps1` | Windows 下用 PyInstaller 打包为单文件 `.exe` |
| `server/Dockerfile` | 服务端 Python 3.12 slim 镜像定义 |
| `docker-compose.yml` | 一键启动服务端的 compose 编排 |
| `agent/requirements.txt` | Agent 运行时依赖（websockets, pynput） |
| `server/requirements.txt` | Server 运行时依赖（fastapi, uvicorn） |
| `.dockerignore` / `.gitignore` | 构建/版本控制过滤规则 |

## 3. 架构与约定

### 3.1 被控端（Agent）打包
- 入口脚本为 `agent/scriptcue_agent.py`，由 PyInstaller 直接指向该文件。
- **macOS**：`build_macos.sh` 使用 `--windowed` 模式生成 `dist/ScriptCue.app`，Bundle ID 固定为 `com.scriptcue.agent`，并通过 `--collect-submodules pynput` 显式收集键盘输入模块。
- **Windows**：`build_windows.ps1` 使用 `--onefile --windowed` 生成 `dist/ScriptCueAgent.exe`。
- 两个脚本均使用严格模式（`set -e` / `$ErrorActionPreference = "Stop"`），失败即中止。
- 产物目录统一为各平台的 `dist/`，不纳入版本控制。
- 当前**未做代码签名与公证**，首次运行需用户手动放行（见 `docs/first-run.md`）。

### 3.2 服务端（Server）容器化
- 基础镜像：`python:3.12-slim`。
- 安装顺序遵循 Docker 最佳实践：先 `COPY requirements.txt` 并 `pip install`，再 COPY 源码，以利用层缓存。
- 工作目录 `/app`，暴露端口 `8000`。
- 健康检查：通过 `urllib.request` 访问 `/healthz`，间隔 30s、超时 3s、重试 3 次。
- 数据持久化：`/app/data` 作为 VOLUME 挂载，用于审计日志等运行时数据。
- 启动命令：`uvicorn server.main:app --host 0.0.0.0 --port 8000`。

### 3.3 编排与部署
- `docker-compose.yml` 仅定义单一服务 `scriptcue-server`，映射主机 8000 端口，使用命名卷 `scriptcue-data` 持久化数据，策略 `restart: unless-stopped`。
- Controller 前端资源在构建时通过 `COPY controller/ ./controller/` 打入镜像，由 Uvicorn 静态托管。

### 3.4 依赖管理
- 每个子项目独立 `requirements.txt`，使用 `>=X,<Y` 的半锁定范围约束主版本，避免破坏性升级。
- Agent 的 `pynput` 通过 `sys_platform == "darwin"` 条件依赖，仅在 macOS 安装。
- PyInstaller 本身注释掉，标注“仅发布时需要”，避免污染开发环境。

## 4. 约定与约束

- **无 CI/CD 流水线**：仓库中未发现 GitHub Actions、Jenkins、GitLab CI 等配置文件，构建与发布目前为本地手工执行脚本。
- **无 Makefile 或统一构建入口**：各平台/组件各自维护脚本，不存在跨平台统一构建目标。
- **版本号管理**：未见 `__version__` 或 `setup.py/pyproject.toml`，版本信息未内嵌到构建产物中；发布物名称硬编码在脚本里（`ScriptCue.app`、`ScriptCueAgent.exe`）。
- **安全约束**：明确未对 macOS `.app` 和 Windows `.exe` 进行签名/公证，首次运行需用户手动放行，相关指引位于 `docs/first-run.md`。
- **数据持久化约定**：服务端所有需要落盘的数据必须写入 `/app/data` 目录，并通过 volume 挂载保留。
- **健康检查约定**：服务端必须在 `/healthz` 提供 HTTP 健康端点，否则容器健康检查会失败。
- **网络端口约定**：服务端默认监听 `8000`，对外暴露端口也固定为 8000。

## 5. 总结

该项目的构建系统呈现**“轻量、按组件自治”**的特点：桌面端通过 PyInstaller 脚本分别产出 macOS 和 Windows 原生安装包，服务端通过 Docker 镜像化并在 compose 中编排。整体缺乏统一的 CI/CD 与版本化管理，适合小规模团队本地构建发布。若后续扩展，建议引入统一的 Makefile/Justfile 聚合构建目标，并补充自动化测试与签名流程。