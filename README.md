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
- 协议消息结构常量定义在 `server/server/protocol.py`，被控端维护一份等价副本 `agent/agent/protocol.py`；
- 服务端与被控端可独立部署，互不依赖对方的代码包。

## 版本发布与下载

- 推送 `v*` 标签（如 `git tag v1.0.0 && git push origin v1.0.0`）自动触发 GitHub Actions 打包，并创建 GitHub Release，产物包括：
  - `ScriptCueAgent-windows-x64.exe`（Windows 10+ 64 位单文件）；
  - `ScriptCueAgent-macos-arm64.dmg`（macOS Apple Silicon）与 `ScriptCueAgent-macos-x86_64.dmg`（macOS Intel，拖拽安装镜像）。
  也可在 Actions 页面手动运行 **Release** workflow 仅验证打包（产物在运行页下载）；
- 服务端 Docker Hub 镜像（`yunshenwuji/scriptcue-server`，amd64）在 Actions 页面手动运行 **Docker Hub** workflow 推送：从 `main` 触发推送 `latest`，从 tag 触发推送对应版本号。需先在仓库 Secrets 配置 `DOCKERHUB_USERNAME` 与 `DOCKERHUB_TOKEN`；
- 未签名产物的首次打开方法见 [首次打开指引](docs/first-run.md)。

## 开源协议

本项目以 [MIT License](LICENSE) 协议开源，与述格（ScriptGrid）保持一致。
