"""PyInstaller 打包入口：启动 GUI 版被控端。

打包命令见 build_windows.ps1 / build_macos.sh。
支持 `--selftest`（替换前预检）：由自动更新器对待装产物调用，
非 0 退出码表示产物不可用，更新器据此放弃替换并保留现有安装。
"""

import sys

from agent.gui import main

if __name__ == "__main__":
    main(sys.argv[1:])
