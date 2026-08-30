---
kind: external_dependency
name: pynput 跨平台按键注入库
slug: pynput
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

### pynput
- 角色：被控端模拟按键（空格键触发播放/暂停）的底层输入注入库，仅在 macOS（`sys_platform == "darwin"`）下安装。
- 集成点：`agent/requirements.txt` 中以平台条件引入；Windows 侧走系统原生 SendInput，macOS 侧通过 pynput 调用 CGEventPost。
- 使用方式：被控端收到绝对时刻指令后，本地高精度定时到点调用 pynput 向前台窗口注入空格键；首次运行会检测辅助功能权限并引导用户授权。
- 方向：未来扩展自定义按键（R-12 进阶项）时仍需基于此库或同层级系统 API。