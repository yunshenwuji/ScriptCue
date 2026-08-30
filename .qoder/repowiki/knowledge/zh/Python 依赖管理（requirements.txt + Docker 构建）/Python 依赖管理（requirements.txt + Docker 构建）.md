---
kind: dependency_management
name: Python 依赖管理（requirements.txt + Docker 构建）
category: dependency_management
scope:
    - '**'
source_files:
    - agent/requirements.txt
    - server/requirements.txt
    - server/Dockerfile
    - docker-compose.yml
---

## 1. 使用的系统/方法
- 语言：Python（Agent 与 Server 均为 Python 项目）
- 包管理器：pip，通过每个子项目的 `requirements.txt` 声明第三方依赖。
- 版本约束：使用 `>=X,<Y` 的宽松上限形式（例如 `fastapi>=0.115,<1.0`、`websockets>=12,<16`），避免锁定到具体小版本，但限制主/次版本范围以兼容升级。
- 平台条件依赖：Agent 使用 PEP 508 环境标记 `sys_platform == "darwin"` 仅在 macOS 上安装 `pynput`，用于键盘注入。
- 打包依赖：`pyinstaller` 被注释掉，仅作为发布期可选依赖存在。
- 容器化：Server 通过 `server/Dockerfile` 基于 `python:3.12-slim` 镜像构建，使用 `pip install --no-cache-dir -r server/requirements.txt` 安装依赖；`docker-compose.yml` 编排该镜像并挂载 `scriptcue-data` 卷持久化审计日志等数据。
- 无 lockfile：仓库中不存在 `requirements.lock`、`poetry.lock`、`Pipfile.lock` 等锁定文件，依赖版本由 pip 解析时确定。
- 私有源/代理：未发现 `.pip/pip.conf`、`setup.cfg` 中的 `index-url`/`extra-index-url` 或环境变量配置，默认使用 PyPI。

## 2. 关键文件
- `agent/requirements.txt`：定义 Agent（被控端）运行时依赖。
- `server/requirements.txt`：定义 Server（演出控制服务端）运行时依赖。
- `server/Dockerfile`：定义 Server 容器镜像构建流程，包含依赖安装与健康检查。
- `docker-compose.yml`：编排 Server 服务，暴露 8000 端口并持久化数据目录。
- `.dockerignore`：Docker 构建上下文过滤（未展开内容，但表明使用 Docker 构建）。

## 3. 架构与约定
- 多组件拆分：Agent 与 Server 各自维护独立的 `requirements.txt`，彼此依赖解耦，便于单独部署。
- 构建即安装：Server 采用“构建阶段安装依赖”的 Docker 模式，不将源码外的依赖引入 git 仓库（无 `vendor/` 或 `third_party/` 目录）。
- 健康检查：Dockerfile 内置 `HEALTHCHECK`，通过访问 `/healthz` 探测服务可用性。
- 运行入口：Server 通过 `uvicorn server.main:app` 启动 FastAPI 应用，端口 8000。

## 4. 约定与约束
- 依赖声明位置：每个 Python 子项目根目录下放置 `requirements.txt`，作为该组件的唯一依赖清单。
- 版本范围策略：所有已声明依赖均使用 `>=X,<Y` 形式的区间约束，禁止直接写死单一版本；这使依赖可在范围内自动升级，但不提供可重复构建保证。
- 平台相关依赖使用环境标记：如 `pynput>=1.7; sys_platform == "darwin"`，确保跨平台安装时只安装所需平台依赖。
- 发布期依赖与运行期依赖分离：`pyinstaller` 在 `agent/requirements.txt` 中以注释形式保留，说明打包工具不参与常规依赖解析，需手动启用。
- 容器构建要求：构建 Server 镜像需在仓库根目录执行 `docker build -f server/Dockerfile -t scriptcue-server .`，因为 Dockerfile 中 COPY 路径为相对路径（`server/requirements.txt`、`server/server/`、`controller/`）。
- 数据持久化约定：通过 `docker-compose.yml` 中的 `scriptcue-data` 卷挂载 `/app/data` 保存审计日志等运行时数据，容器重建不丢失。
- 无全局依赖管理：仓库顶层没有统一的依赖清单或锁文件，各组件独立管理自身依赖。