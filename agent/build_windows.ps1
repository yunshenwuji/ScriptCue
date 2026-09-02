# 述播被控端 Windows 打包脚本（在 agent/ 目录执行）
# 产物: dist/ScriptCueAgent.exe（单文件免安装）
#
# 前置: 项目 .venv 中已安装 pyinstaller（pip install pyinstaller）

$ErrorActionPreference = "Stop"

# 优先使用项目虚拟环境的 Python，避免依赖外部激活的 conda/全局环境
$repoRoot = Split-Path $PSScriptRoot -Parent
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $python = $venvPython
} else {
    $python = "python"
    Write-Warning "未找到项目虚拟环境 .venv，使用当前 PATH 中的 python"
}

& $python -m PyInstaller --noconfirm --onefile --windowed `
    --name ScriptCueAgent `
    --collect-data certifi `
    scriptcue_agent.py

Write-Host ""
Write-Host "打包完成: agent/dist/ScriptCueAgent.exe"
Write-Host "注意: 未签名程序首次运行会触发 SmartScreen 提示，见 docs/first-run.md"
