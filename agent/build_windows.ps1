# 述播被控端 Windows 打包脚本（在 agent/ 目录执行）
# 产物: dist/ScriptCueAgent.exe（单文件免安装）
#
# 前置: pip install pyinstaller

$ErrorActionPreference = "Stop"

pyinstaller --noconfirm --onefile --windowed `
    --name ScriptCueAgent `
    scriptcue_agent.py

Write-Host ""
Write-Host "打包完成: agent/dist/ScriptCueAgent.exe"
Write-Host "注意: 未签名程序首次运行会触发 SmartScreen 提示，见 docs/first-run.md"
