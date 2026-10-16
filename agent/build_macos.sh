#!/bin/bash
# 述播被控端 macOS 打包脚本（在 agent/ 目录执行）
# 产物: dist/ScriptCue.app、dist/ScriptCueAgent-macos-<架构>.dmg（拖拽安装镜像）
#       与 dist/ScriptCueAgent-macos-<架构>-update.tar.gz（应用内自动更新包）
#
# 前置: pip install -r requirements.txt pyinstaller
# dmg 采用系统自带 hdiutil 制作（零额外依赖，可完全在 GitHub Actions 中自动完成）
# 签名: 设置 MACOS_SIGNING_IDENTITY 时用自签代码签名证书重签（覆盖 PyInstaller 的
#       ad-hoc 签名），稳定的签名身份让 TCC 把"辅助功能"授权与证书绑定而非与单次
#       构建的 cdhash 绑定，授权可跨版本保留；未设置时跳过（保留 ad-hoc）。
#       未做公证：每次下载新版本 Gatekeeper 仍会提示，放行指引见 docs/first-run.md

set -e

pyinstaller --noconfirm --windowed \
    --name ScriptCue \
    --osx-bundle-identifier com.scriptcue.agent \
    --collect-submodules Quartz \
    --collect-data certifi \
    scriptcue_agent.py

echo "已生成应用包: dist/ScriptCue.app"

# 代码签名（可选）：须在制作 DMG 之前完成，DMG 内必须装已签名的 app。
# --force 覆盖 PyInstaller 的 ad-hoc 签名；--deep 连同嵌套内容一并重签；
# 签完立即校验，签名失败时 set -e 直接终止打包。
# 不加 --options runtime：强化运行时是公证路线才需要的，Python 应用缺 entitlements 反而会翻车。
if [ -n "${MACOS_SIGNING_IDENTITY:-}" ]; then
    echo "使用签名身份: ${MACOS_SIGNING_IDENTITY}"
    codesign --force --deep --sign "${MACOS_SIGNING_IDENTITY}" dist/ScriptCue.app
    codesign --verify --deep --strict dist/ScriptCue.app
    echo "签名校验通过"
else
    echo "未设置 MACOS_SIGNING_IDENTITY，跳过证书签名（保留 ad-hoc 签名）"
fi

# 按运行架构自动命名（arm64 / x86_64），与所在 runner 架构一致
arch="$(uname -m)"
dmgName="ScriptCueAgent-macos-${arch}.dmg"

# 应用内自动更新包（在签名之后制作，保证包内是已签名结构）：
# tar 保留符号链接、权限与签名结构（免 hdiutil 挂载流程），被控端自身下载、
# 解包到 .app 同目录后原子替换。COPYFILE_DISABLE=1 防止 tar 混入 ._* AppleDouble
# 文件污染签名结构（该文件不会出现在 tar 清单中，却会随解包落到 bundle 里）。
COPYFILE_DISABLE=1 tar -czf "dist/ScriptCueAgent-macos-${arch}-update.tar.gz" \
    -C dist ScriptCue.app
echo "已生成更新包: dist/ScriptCueAgent-macos-${arch}-update.tar.gz"

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
echo "更新包:   agent/dist/ScriptCueAgent-macos-${arch}-update.tar.gz"
echo "注意: 未公证应用每次下载新版本首次打开需在系统设置中放行，见 docs/first-run.md"
echo "      应用内更新（-update.tar.gz）由被控端自身下载安装，不经过浏览器，无需重新放行"
