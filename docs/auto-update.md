# 被控端应用内自动更新设计

版本：v1.1.0 起被控端（Windows / macOS）支持应用内自动更新。

核心原则：**下载可自动，替换必须人点，演出中永不动手。**

## 1. 架构与数据流

```
维护者手工同步                       服务端（Docker）                    被控端（GUI）
┌───────────────────┐   GET(启动+每24h) ┌─────────────────┐  ①GET /update/manifest.json
│ 123云盘 CDN 对象存储 │ ◀────────────── │ updatefeed.py    │  ②ws 推送 update.available
│ cdn.kadaiad.fun    │               │ 校验+内存/磁盘缓存  │ ◀────────────┐
│  /www/ScriptCue-update │ ─────────────▶ │ 注入 url+server_time│              │ ③下载（sha256 双取）
│  update-manifest.json│  下发副本        │ /update/manifest.json│              ▼
│  各平台产物            │               │ /healthz 可见状态 │          ④校验通过提示（自动）
└───────────────────┘               └─────────────────┘          ⑤用户点击 → selftest → 替换 → 重启
```

- **CI**（release.yml，tag 触发）只产出产物、`.sha256` 与 `update-manifest.json` 作为 Release 附件；
- **CDN 内容由维护者手动同步**（清单与全部产物；同步方式不在本文档约定范围）；
- 服务端启动即拉取清单并落盘缓存（`./data/update-cache.json`，与审计日志同目录、同 UID 10001
  权限模型），此后按 `SC_UPDATE_REFRESH_H`（默认 24h）刷新；CDN 不可达时继续用缓存，只记日志；
- 被控端检查有四条触发路径（同一套下载校验代码）：启动后 15s 首查 → 每 6h 定时 → 服务端 ws 推送
  `update.available` → 用户手点「检查更新」。

## 2. 更新清单 schema（schema=1）

```jsonc
{
  "schema": 1,
  "version": "1.1.0",            // 纯数字点分，与 tag 同形（不带 v）；含非数字段视为开发版
  "released_at": "2026-10-12T10:00:00+08:00",
  "notes": "修复 …；新增 …",      // 展示给用户，纯文本
  "assets": {
    "windows-x64":  { "asset": "ScriptCueAgent-windows-x64.exe", "kind": "exe",
                      "sha256": "…", "size": 12345678 },
    "macos-arm64":  { "asset": "ScriptCueAgent-macos-arm64-update.tar.gz", "kind": "tar.gz",
                      "sha256": "…", "size": 23456789,
                      "codesign_authority": "ScriptCue Codesign" },
    "macos-x86_64": { "asset": "ScriptCueAgent-macos-x86_64-update.tar.gz", "kind": "tar.gz",
                      "sha256": "…", "size": 23456789,
                      "codesign_authority": "ScriptCue Codesign" }
  }
}
```

- **平台键** = `<os>-<arch>`：被控端用 `sys.platform`（`win32`→`windows`、`darwin`→`macos`）+
  架构名（`AMD64`→`x64`、`arm64`、`x86_64`）拼出；键不存在时只提示"请手动下载"，不报错；
- **`url` 由服务端下发时注入**（CI 清单不含绝对地址）：`SC_UPDATE_ASSET_BASE_URL` 优先，
  未配置时用清单 URL 同目录兜底（保证清单直接放 GitHub Releases 也能用）；
- **`codesign_authority`** 仅 macOS 用：替换前与当前 .app、待装 .app 的签名身份三方比对，
  不一致即拒绝替换（保住 TCC 辅助功能授权的连续性）；任一方为 ad-hoc 时跳过比对；
- 服务端下发副本额外注入 `server_time`，便于排查时钟问题；
- 发布两份：稳定名 `update-manifest.json`（自动通道用）+ 实名 `update-manifest-<version>.json`
  （人工钉版本/审计用）。清单 schema 变更必须同步：本文件、release.yml 清单生成步骤、
  两端解析代码（`server/server/updatefeed.py`、`agent/agent/updater.py`）。

## 3. CDN 目录约定与地址

对象存储根目录 `/www/ScriptCue-update/`，直链域名前缀 `https://cdn.kadaiad.fun`，
即每个文件 `https://cdn.kadaiad.fun/www/ScriptCue-update/<文件名>`：

| 文件 | 说明 |
|---|---|
| `update-manifest.json` | 稳定名清单（服务端拉取对象） |
| `update-manifest-<version>.json` | 实名清单（钉版本用，可选） |
| `ScriptCueAgent-windows-x64.exe` | Windows x64 单文件 |
| `ScriptCueAgent-macos-arm64-update.tar.gz` | macOS Apple Silicon 更新包 |
| `ScriptCueAgent-macos-x86_64-update.tar.gz` | macOS Intel 更新包 |

## 4. 服务端环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SC_UPDATE_MANIFEST_URL` | 空 | CDN 清单地址，如 `https://cdn.kadaiad.fun/www/ScriptCue-update/update-manifest.json` |
| `SC_UPDATE_MANIFEST_FILE` | 空 | 本地清单文件；存在且可解析时**完全接管并停用 CDN 刷新**（钉版本），容器内建议 `/app/data/update-manifest.json`（走已有 `./data` 绑定挂载） |
| `SC_UPDATE_ASSET_BASE_URL` | 空 | 产物 CDN 根地址，如 `https://cdn.kadaiad.fun/www/ScriptCue-update/` |
| `SC_UPDATE_REFRESH_H` | `24` | 清单刷新周期（小时） |
| `SC_UPDATE_DISABLED` | 空 | 置 `1` 整体关闭更新通道（`/update/manifest.json` 返回 404） |

两个地址变量都不配置时更新通道关闭（被控端检查会失败但不影响主链路）；
部署后访问 `/healthz` 可看到 `update_feed: {version, source, age_s}` 一眼确认状态。

## 5. 发版检查表

1. 确认「上一版本已有自动更新能力」——首个带自动更新能力的 v1.1.0 需要用户手动安装一次，
   之后（v1.1.0 → v1.1.1+）才能走应用内通道；
2. **先部署服务端**（`docker compose pull && docker compose up -d`，确认 `/healthz` 的
   `proto=2`），**再发布被控端**（推送 `v*` tag 触发 Release 构建）——顺序颠倒时新被控端
   会被旧服务端以 `bad_proto` 拒绝；
3. 从 GitHub Release 下载产物与 `.sha256`，核对校验和；
4. 将清单与各平台产物同步到 CDN `/www/ScriptCue-update/`（覆盖稳定名清单）；
5. 服务端最迟在下一个刷新周期（或重启）后生效；急需立即生效可重启服务端容器，或直接
   GET 一下 `/update/manifest.json` 前后的版本号确认；
6. 抽查一台旧版被控端：15 秒内应显示"发现新版本"，主控端设备卡片出现"有可用更新"徽标。

## 6. 被控端流程与安全边界

- **判据只有一个函数** `safety_window()`（`agent/agent/updater.py`）：未连接，或已连接但
  "未就绪 + 无待触发指令（引擎 `_pending_fires` 与 GUI `pending` 均空）"；
- **下载**：仅在安全窗口内进行；演出中（就绪或有待触发指令）发现新版本只记录，等窗口打开
  自动继续。避免占带宽影响时钟同步；
- **替换**：必须用户点击（点版本行）+ 点击时重新校验安全窗口 + 连接中时二次确认
  （明确告知会断连重启）；
- **sha256 双取**：下载前取一次清单、下载后再取一次，两份清单里目标资产的 sha256 与版本
  必须一致才认（防下载期间发新版）；不一致自动重下，最多 3 轮后放弃且不触碰现有安装；
- **selftest 准入闸**：替换前对待装产物跑 `--selftest`（导入全部模块 + 非交互按键探测，
  不建 Tk、不弹框、不触发系统权限申请）；Windows 超时 90s / macOS 45s，非 0 或超时即放弃。
  macOS 的辅助功能授权状态**不参与判定**——待装二进制位于暂存目录、签名要求与当前进程
  不同，不可能持有 TCC 授权；授权与否只影响正式启动后的按键注入，由开机自检负责呈现；
- **绝不在未确认时自动重启**；自动回退只在"更新后连续 2 次启动未正常收尾"这一种情况下
  发生，且发生在启动阶段而非演出中；正常关闭程序（哪怕没到 60s 确认窗口）不算崩溃；
- 更新流程的任何异常都只降级为日志与提示，**绝不允许影响按键触发主链路**。

### 6.1 Windows 替换

运行中的 exe 允许改名、不允许覆写：`exe → exe.old` 改名 → 换入新 exe → detached 重启；
任一步失败立即复原并报错。由我们落盘的新 exe 不带 Zone.Identifier → 重启不再触发 SmartScreen。

> **关键坑（Windows 真机 e2e 实捉）**：重启/回退/spawn 新进程时必须从环境中剔除
> PyInstaller 的内部变量（`_PYI_ARCHIVE_FILE`、`_PYI_APPLICATION_HOME_DIR`、
> `_PYI_PARENT_PROCESS_LEVEL`、`_PYI_SPLASH_IPC`、遗留的 `_MEIPASS2`，见
> `agent/agent/updater.py` 的 `cleaned_child_env()`）。否则新 bootloader 会误把自己
> 当作 onefile 的内部子进程，执行"父进程可执行文件一致"安全校验——而父进程（旧版本）
> 的 exe 此时已被改名为 `.old`，校验必然失败，新版本弹
> "Security validation failure: parent process has different executable!" 拒绝启动。
> macOS 的替换/回退脚本同样在 `open` 前 `unset` 这些变量。

### 6.2 macOS 替换

tar.gz 解包到 .app 同目录暂存 → 签名身份三方比对 + `codesign --verify --deep --strict` →
selftest → 派发 detached sh 脚本：等主进程退出 → `ScriptCue.app → ScriptCue.app.old` →
新版上位 → `open` 重启（新 bundle 由本进程落盘、不带 `com.apple.quarantine`，
**升级后不再需要"仍要打开"放行**）。目录不可写时降级为提示手动安装，不弹管理员密码框。

macOS 替换/回退的残留清理：替换脚本的新版上位失败分支会清掉 `.new-<pid>` 暂存目录；
回退脚本在 `open` 后删除 `.bad`（被回退掉的新版，再次检查更新可重新下载）；
脚本被强杀中断留下的 `.bad` 与 `.new-*` 由下次启动的 `_cleanup_leftovers()` 兜底清理。

## 7. 回退与健康确认

- 状态文件 `update-state.json`（与 agent_config.json 同目录）：
  `{applied_version, previous_version, applied_at, confirmed, crash_streak, launch_armed, clean_exit}`；
- **启动健康状态机**（`updater.startup_health_gate()`，在 `gui.main()` 里**建 Tk 之前**运行——
  连 Tk 初始化都会崩溃的坏版本也能被计数、被自动回退救援）：
  - 每次启动先把状态乐观标记为"已武装、未正常收尾"（`launch_armed=true, clean_exit=false`）；
    只有正常退出（用户关窗 / 取消权限引导，`note_clean_exit()`）才覆写 `clean_exit=true`；
  - 下次启动时结算：上次已武装且未正常收尾 → `crash_streak += 1`；上次正常退出 → 清零；
  - `crash_streak >= 2` 且 `.old` 存在 → 自动回退（detached 替换并重启，记 ERROR 日志）；
    正常关闭程序、被健康确认过的版本都不会触发回退；
- **健康确认**：GUI mainloop 存活 60s 即写 `confirmed=true`——"非启动即崩"的充分证据。
  按键自检可能因环境因素失败（UIPI 拦截、macOS 未授权等），只记日志、**不参与判定**；
- **`.old` 保留策略**：健康确认后**不删除** `.old`（用户可随时在 GUI 点「回退上一版本」），
  到下一次更新替换时让位删除；
- **手动回退**：GUI 状态区「回退上一版本」按钮（与版本行、检查更新同属一区，避免工具行拥挤），
  仅当 `.old` 存在时可用；同样走安全窗口校验 + 确认弹框 + detached 重启；
- **幂等与互斥**：更新/回退从提交到进程退出的窗口可达数分钟（selftest 最长 90s），
  updater 用一把 action 门闩保证同一时刻只有一个替换类任务；GUI 提交成功后立即清除
  待安装信息并进入忙态（按钮置灰、版本行不可点），失败事件会携带待安装信息、
  恢复"点击重试"；
- **回退状态落定的失败复原**：回退先写"已回退"状态再派发替换（派发成功即 `os._exit`，
  没有第二次保存机会）；派发失败且回退点仍在（未发生实际换位）时回写回退前状态，
  避免"状态说已回退、实际仍是新版"的不一致。

## 8. 日志关键词排障表（被控端 agent.log）

| 关键词 | 含义 | 常见处置 |
|---|---|---|
| `发现新版本 v…` | 检查到新版本 | 正常；若一直不出现，核对 `/healthz` 的 update_feed |
| `等待演出空闲后下载` | 处于安全窗口外，已等待 | 取消就绪/断开连接后会自动继续 |
| `更新包下载完成` | 下载且双取校验通过 | 正常 |
| `sha256 校验失败` / `下载期间发布方更新了清单` | 校验不一致，自动重下 | 连续 3 轮失败要检查 CDN 上的清单与产物是否同批 |
| `更新包预检未通过` | 待装产物 selftest 失败 | 保留现有版本；回查 CI 构建产物 |
| `签名身份不一致` | macOS 签名身份变了 | 换证书会导致辅助功能授权失效，需一并重新授权 |
| `已替换程序文件` | Windows 替换成功，即将重启 | 正常 |
| `已派发替换脚本` | macOS 交给 sh 脚本完成替换 | 正常 |
| `上次启动未正常收尾` | 一次启动崩溃已记录（连崩计数 n/2） | 正常使用并退出程序会清零；再崩一次将自动回退 |
| `自动回退到上一版本` | 更新后连续 2 次启动未正常收尾 | 已回退；把日志发给开发者排因 |
| `更新健康确认` | mainloop 存活 60s，confirmed=true | 正常 |

服务端排障：`docker logs` 看 `scriptcue.update` 的 `更新清单已更新: v…` 与
`拉取更新清单失败`；审计日志（`./data/audit.jsonl`）内有 `update_manifest_refresh`、
`update_pushed` 事件可复盘推送记录。
