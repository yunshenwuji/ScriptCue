# 述播 ScriptCue

多设备口述影像同步起播系统。通过"时钟对齐 + 绝对时刻调度"，让大屏电脑与多台口述员电脑在公网环境下实现 ≤50ms 精度的同步起播。

详细需求见 [PRD](docs/PRD.md)。

## 目录结构

```
├── server/        云端服务（FastAPI + WebSocket，支持 Docker 部署）
├── controller/    主控端（原生 HTML/JS 网页，由服务端静态托管）
├── agent/         被控端（Python，Windows / macOS，PyInstaller 单文件打包）
└── docs/          文档（协议规范、首次打开指引等）
```

## 架构概览

- **服务端**：房间管理、WebSocket 长连接、授时基准、指令广播、状态汇聚、审计日志。房间数据纯内存存储，重启后由客户端重连自动重建。
- **主控端**：导控人员的响应式网页（手机竖屏优先），查看设备状态并下发起播/暂停等指令。
- **被控端**：加入房间后持续与服务器做类 NTP 时钟同步；收到带绝对执行时刻的指令后，本地高精度定时到点向前台窗口模拟按下空格键。

## 同步核心机制

1. 被控端通过多次 ping 采样估算本地时钟与服务器时钟的偏移，取 RTT 最小的样本作为可信偏移；
2. 主控下发指令时，服务器附加执行时刻 `T = 服务器当前时间 + 提前量（默认 3s）`；
3. 被控端将 T 换算为本地时刻（`T − 时钟偏移 − 补偿值`），粗睡眠 + 末段自旋等待到点触发；
4. 网络延迟与抖动只要不超过提前量，就不影响同步精度。

## 环境要求

- Python ≥ 3.10（服务端与被控端）
- Docker（可选，服务端部署用）

## 快速开始

### 服务端

```bash
cd server
pip install -r requirements.txt
python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
```

或使用 Docker（在仓库根目录执行，审计日志存于 `./data` 目录）：

```bash
docker build -t yunshenwuji/scriptcue-server:latest .
mkdir -p data && sudo chown -R 10001:10001 data
docker run -d -p 8000:8000 -v "$(pwd)/data:/app/data" --name scriptcue-server yunshenwuji/scriptcue-server:latest
```

或使用 docker compose（部署机上直接拉取 Docker Hub 镜像，无需源码）：

```bash
mkdir -p data && sudo chown -R 10001:10001 data
docker compose pull && docker compose up -d
```

### 主控端

浏览器访问 `http://<服务器地址>:8000/`（网页由服务端静态托管）。

### 被控端

```bash
cd agent
pip install -r requirements.txt

# GUI 版（正式使用）
python -m agent.gui

# 命令行版（联调/验证核心链路）
python -m agent.cli --server ws://127.0.0.1:8000 --room <房间码> --nickname <昵称>
```

打包为免安装单文件（发布用，需先安装 pyinstaller）：

- Windows：在 `agent/` 目录执行 `.\build_windows.ps1`，产物 `dist/ScriptCueAgent.exe`
- macOS：在 `agent/` 目录执行 `./build_macos.sh`，产物 `dist/ScriptCue.app`

未签名程序的首次打开方法见 [首次打开指引](docs/first-run.md)。

## 开发约定

- 通信协议为版本化 JSON 文本协议，见 `docs/protocol.md`，服务端、主控端、被控端三方共用；
- 协议常量定义在 `server/server/protocol.py`（共享契约的权威定义），被控端在 `agent/agent/protocol.py` 只复制其中的**共享契约**：`PROTO_VERSION`、消息类型、指令类型与 `VALID_COMMANDS`、时钟质量分级、错误码、`HEARTBEAT_INTERVAL_S`。两份文件不是逐字等价副本——各端独有的运行限制常量（服务端的房间与回执限制、被控端的重连退避与时钟同步参数）不在同步范围内，只存在于自己那一份；
- 改动共享契约的责任链：同一笔改动里必须同时更新 `docs/protocol.md`、`server/server/protocol.py` 与 `agent/agent/protocol.py`，并递增 `PROTO_VERSION`；由提交这笔改动的人负责确认三者一致，做法是逐字比对上面列出的共享契约常量。只改某一端独有的运行限制常量时，不递增 `PROTO_VERSION`，也不需要同步另一端。这条责任链目前只靠人工执行，仓库里还没有机械检查兜底；
- 另有两处跨端耦合但不要求逐字一致：`DEFAULT_LEAD_MS` 以服务端为权威默认值（可用环境变量 `SC_DEFAULT_LEAD_MS` 覆盖）并经 `agent.joined` 下发，被控端那份只是本地兜底值、不参与触发时刻计算；服务端的 `AGENT_OFFLINE_S` 取值是被控端 `HEARTBEAT_INTERVAL_S` 的 3 倍，改心跳间隔时必须一并调整服务端阈值与 `docs/protocol.md`；
- 服务端与被控端可独立部署，互不依赖对方的代码包。
- 版本号与更新清单的责任链：被控端版本号唯一来源是 `agent/agent/version.py`（本地保持 `0.0.0.dev`，发版时由 CI 从 git tag 注入，如 tag `v1.1.0` → `APP_VERSION = "1.1.0"`）；更新清单 schema 变更必须同一笔改动同步 `docs/auto-update.md`、`.github/workflows/release.yml` 的清单生成步骤与两端解析代码（`server/server/updatefeed.py`、`agent/agent/updater.py`）。
- 改动被控端核心链路（时钟偏移换算、绝对时刻调度、取消与触发回执）前后，都要跑一次聚焦检查：在仓库根目录执行 `python -m unittest discover -s agent/tests -t agent -v`（需先按上面的快速开始安装 `agent/requirements.txt`；Windows 下若未激活虚拟环境，把 `python` 换成 `.venv\Scripts\python.exe`）。检查用标准库 unittest 编写、位于 `agent/tests/`，不引入额外依赖，运行时不连网也不注入真实按键；这是当前项目唯一的机械验证入口，新增被控端行为时请在此处补充用例。
- 服务端同样有一个聚焦检查入口：`python -m unittest discover -s server/tests -t server -v`（用同一虚拟环境即可，不额外装依赖；不连网、不写真实数据目录）。新增服务端行为（如更新清单源）时在其中补充用例。

## 版本发布与下载

- 推送 `v*` 标签（如 `git tag v1.1.0 && git push origin v1.1.0`）自动触发 GitHub Actions 打包，并创建 GitHub Release，产物包括：
  - `ScriptCueAgent-windows-x64.exe`（Windows 10+ 64 位单文件）；
  - `ScriptCueAgent-macos-arm64.dmg`（macOS Apple Silicon）与 `ScriptCueAgent-macos-x86_64.dmg`（macOS Intel，拖拽安装镜像）；
  - `ScriptCueAgent-macos-<架构>-update.tar.gz`（应用内自动更新包，免 dmg 挂载）；
  - `update-manifest.json` 更新清单（含各产物的 sha256 与尺寸）。
  也可在 Actions 页面手动运行 **Release** workflow 仅验证打包（产物在运行页下载，此时版本保持 dev、不会进入自动更新通道）；
- 应用内自动更新的完整链路（CDN 目录约定、服务端环境变量、发版顺序硬约束、回退操作与排障）见 [自动更新设计](docs/auto-update.md)。发版顺序硬约束：**先部署新服务端镜像，再发布 v2 被控端**；清单与产物到 CDN 的同步在 tag 发布时自动完成（未配置 PAN123_* Secrets 时跳过，可手动兜底）；
- 服务端 Docker Hub 镜像（`yunshenwuji/scriptcue-server`，amd64）在 Actions 页面手动运行 **Docker Hub** workflow 推送：从 `main` 触发推送 `latest`，从 tag 触发推送对应版本号。需先在仓库 Secrets 配置 `DOCKERHUB_USERNAME` 与 `DOCKERHUB_TOKEN`；
- 未签名产物的首次打开方法见 [首次打开指引](docs/first-run.md)（v1.1.0 起应用内更新不再需要重新放行）。

## 开源协议

本项目以 [MIT License](LICENSE) 协议开源，与述格（ScriptGrid）保持一致。
