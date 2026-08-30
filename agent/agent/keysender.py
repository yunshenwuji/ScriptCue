"""按键注入（R-03 / R-10 / R-11）。

- Windows：Win32 SendInput（ctypes 直调，无第三方依赖，系统级快速路径）；
- macOS：CGEventPost（pynput 封装）。

选择原生实现而非高层库的原因：注入延迟最低（1~5ms），且便于做开机自检
与权限状态检测（macOS 辅助功能权限）。
"""

import subprocess
import sys


class KeySendError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Windows: SendInput
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    VK_SPACE = 0x20
    # ULONG_PTR：64 位下为 8 字节
    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    # 联合体必须包含最大的成员（MOUSEINPUT），否则 sizeof(INPUT)
    # 与系统要求不符，SendInput 会直接返回 0（ERROR_INVALID_PARAMETER）
    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _SendInput = _user32.SendInput
    _SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    _SendInput.restype = wintypes.UINT

    def _send_space() -> None:
        inputs = (INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].ki = KEYBDINPUT(wVk=VK_SPACE)
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].ki = KEYBDINPUT(wVk=VK_SPACE, dwFlags=KEYEVENTF_KEYUP)
        sent = _SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        if sent != 2:
            err = ctypes.get_last_error()
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.kernel32.FormatMessageW(
                0x1000, None, err, 0, buf, 256, None)  # FORMAT_MESSAGE_FROM_SYSTEM
            raise KeySendError(
                f"SendInput 失败（仅注入 {sent}/2，系统错误 {err}: {buf.value.strip()}）。"
                "请确认：1) 本程序未被安全软件拦截；"
                "2) 前台窗口（播放器等）不是以管理员身份运行")


# ---------------------------------------------------------------------------
# macOS: CGEventPost（经 pynput）
# ---------------------------------------------------------------------------

elif sys.platform == "darwin":
    import ctypes
    import ctypes.util

    # 辅助功能权限检测：AXIsProcessTrusted（无需 pyobjc）
    _appservices = None
    try:
        _lib_path = ctypes.util.find_library("ApplicationServices")
        if _lib_path:
            _appservices = ctypes.cdll.LoadLibrary(_lib_path)
            _appservices.AXIsProcessTrusted.restype = ctypes.c_bool
    except OSError:  # pragma: no cover
        _appservices = None

    def _send_space() -> None:
        from pynput.keyboard import Controller, Key
        Controller().tap(Key.space)

    def accessibility_trusted() -> bool:
        """当前进程是否已获得辅助功能权限。"""
        if _appservices is None:
            return False
        return bool(_appservices.AXIsProcessTrusted())

    def open_accessibility_settings() -> None:
        """打开"系统设置 → 隐私与安全性 → 辅助功能"页面（R-10 引导）。"""
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

class KeySender:
    """跨平台空格键注入器。"""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        if sys.platform not in ("win32", "darwin"):
            raise KeySendError(f"不支持的操作系统: {sys.platform}")

    def check(self) -> tuple[bool, str]:
        """开机自检（R-11）：验证按键注入能力。返回 (是否通过, 说明)。"""
        if sys.platform == "darwin" and not accessibility_trusted():
            return False, ("未获得 macOS 辅助功能权限。请在弹出的引导中授权"
                           "（系统设置 → 隐私与安全性 → 辅助功能），授权后重启本程序。")
        if self.dry_run:
            return True, "演练模式：跳过实际按键注入"
        try:
            # 向当前前台窗口发送一次空格作为自检（与正式触发同一路径）
            _send_space()
            return True, "按键注入自检通过"
        except Exception as exc:  # pragma: no cover
            return False, f"按键注入自检失败: {exc}"

    def press_space(self) -> None:
        """向前台窗口模拟按下并释放空格键。"""
        if self.dry_run:
            return
        _send_space()
