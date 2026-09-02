#!/bin/bash
# 述播被控端 macOS 打包脚本（在 agent/ 目录执行）
# 产物: dist/ScriptCue.app 与 dist/ScriptCueAgent-macos-<架构>.dmg（拖拽安装镜像）
#
# 前置: pip install -r requirements.txt pyinstaller
# dmg 采用系统自带 hdiutil 制作（零额外依赖，可完全在 GitHub Actions 中自动完成）
# 说明: 当前未做签名与公证（见 PRD 风险对策），首次打开需右键→打开，
#       指引见 docs/first-run.md

set -e

pyinstaller --noconfirm --windowed \
    --name ScriptCue \
    --osx-bundle-identifier com.scriptcue.agent \
    --collect-submodules pynput \
    --collect-data certifi \
    scriptcue_agent.py

echo "已生成应用包: dist/ScriptCue.app"

# 按运行架构自动命名（arm64 / x86_64），与所在 runner 架构一致
arch="$(uname -m)"
dmgName="ScriptCueAgent-macos-${arch}.dmg"

# 制作带拖拽安装界面的 dmg：
# 暂存目录放入应用与 Applications 快捷方式，用户挂载后将应用拖入右侧图标即可安装
staging="$(mktemp -d)"
cp -R dist/ScriptCue.app "${staging}/"
ln -s /Applications "${staging}/Applications"
hdiutil create \
    -volname "ScriptCue" \
    -srcfolder "${staging}" \
    -ov \
    -format UDZO \
    "dist/${dmgName}"
rm -rf "${staging}"

echo ""
echo "打包完成: agent/dist/${dmgName}"
echo "注意: 未签名应用首次打开需右键→打开，见 docs/first-run.md"
