---
kind: external_dependency
name: PyInstaller 打包工具
slug: pyinstaller
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

### PyInstaller
- 角色：将被控端 Python 程序打包为 Windows `.exe` / macOS `.app` 单文件可执行程序，实现免安装分发。
- 集成点：仓库提供 `agent/build_windows.ps1` 与 `agent/build_macos.sh` 两个构建脚本；产物分别为 `dist/ScriptCue.exe` 与 `dist/ScriptCue.app`。
- 使用方式：发布前安装 pyinstaller，执行对应平台的构建脚本；macOS 未签名版本首次打开需通过 Gatekeeper 绕行（见 `docs/first-run.md`）。
- 方向：对外分发时再考虑 Apple Developer Program 签名与公证流程（当前阶段 pending）。