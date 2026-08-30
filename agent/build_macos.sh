#!/bin/bash
# 述播被控端 macOS 打包脚本（在 agent/ 目录执行）
# 产物: dist/ScriptCue.app（应用包，免安装）
#
# 前置: pip install pyinstaller pynput
# 说明: 当前未做签名与公证（见 PRD 风险对策），首次打开需右键→打开，
#       指引见 docs/first-run.md

set -e

pyinstaller --noconfirm --windowed \
    --name ScriptCue \
    --osx-bundle-identifier com.scriptcue.agent \
    --collect-submodules pynput \
    scriptcue_agent.py

echo ""
echo "打包完成: agent/dist/ScriptCue.app"
echo "注意: 未签名应用首次打开需右键→打开，见 docs/first-run.md"
