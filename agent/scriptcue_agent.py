"""PyInstaller 打包入口：启动 GUI 版被控端。

打包命令见 build_windows.ps1 / build_macos.sh。
"""

from agent.gui import main

if __name__ == "__main__":
    main()
