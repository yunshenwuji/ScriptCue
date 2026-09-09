"""按键注入（R-03 / R-10 / R-11）。

- Windows：Win32 SendInput（ctypes 直调，无第三方依赖，系统级快速路径）；
- macOS：CGEventPost（pyobjc Quartz 直调，不经 pynput）。

选择原生实现而非高层库的原因：注入延迟最低（1~5ms），且便于做开机自检
与辅助功能权限的检测、申请与授权记录清理（macOS TCC）。
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
        # 数组实例可直接作为指针参数传递（ctypes 自动转换），
        # 不能用 byref()，其产生的是"指向数组的指针"，与 argtypes 检查不匹配
        sent = _SendInput(2, inputs, ctypes.sizeof(INPUT))
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
# macOS: CGEventPost（pyobjc Quartz 直调）
# ---------------------------------------------------------------------------

elif sys.platform == "darwin":
    import ctypes
    import ctypes.util

    class AccessibilityCheckError(RuntimeError):
        """辅助功能权限检测能力不可用（系统框架加载失败）。"""

    # 本应用在辅助功能列表中的登记标识（与打包脚本 --osx-bundle-identifier 一致）
    _BUNDLE_ID = "com.scriptcue.agent"

    # 辅助功能权限检测与申请：AXIsProcessTrusted(WithOptions)（无需 pyobjc）。
    # 框架加载失败必须显式抛 AccessibilityCheckError，不能静默当作"未授权"——
    # 否则环境异常会被伪装成权限缺失，把引导流程逼进无解的死循环
    _appservices = None
    try:
        _lib_path = ctypes.util.find_library("ApplicationServices")
        if _lib_path:
            _appservices = ctypes.cdll.LoadLibrary(_lib_path)
            _appservices.AXIsProcessTrusted.restype = ctypes.c_bool
            _appservices.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
            _appservices.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    except OSError:  # pragma: no cover
        _appservices = None

    # 发起申请需用 CoreFoundation 构造 {kAXTrustedCheckOptionPrompt: True} 字典。
    # 回调必须绑定 kCFType 系列真实回调结构：系统 API 内部以标准 CFString 语义
    # （hash+内容比较）查询键并断言取出的值类型，裸指针语义（NULL 回调）会让
    # 查询落空、系统对取出的 NULL 值调用 CFGetTypeID 时段错误闪退。
    class _CFDictCallBacks(ctypes.Structure):
        """CFDictionaryKeyCallBacks / CFDictionaryValueCallBacks（两者字段布局一致）。"""

        _fields_ = [
            ("version", ctypes.c_long),
            ("retain", ctypes.c_void_p),
            ("release", ctypes.c_void_p),
            ("copyDescription", ctypes.c_void_p),
            ("equal", ctypes.c_void_p),
            ("hash", ctypes.c_void_p),
        ]

    _corefoundation = None
    try:
        _cf_path = ctypes.util.find_library("CoreFoundation")
        if _cf_path:
            _corefoundation = ctypes.cdll.LoadLibrary(_cf_path)
            _corefoundation.CFStringCreateWithCString.restype = ctypes.c_void_p
            _corefoundation.CFStringCreateWithCString.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            _corefoundation.CFDictionaryCreateMutable.restype = ctypes.c_void_p
            _corefoundation.CFDictionaryCreateMutable.argtypes = [
                ctypes.c_void_p, ctypes.c_long,
                ctypes.POINTER(_CFDictCallBacks), ctypes.POINTER(_CFDictCallBacks)]
            _corefoundation.CFDictionarySetValue.restype = None
            _corefoundation.CFDictionarySetValue.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            _corefoundation.CFRelease.restype = None
            _corefoundation.CFRelease.argtypes = [ctypes.c_void_p]
    except OSError:  # pragma: no cover
        _corefoundation = None

    def _ensure_checkable() -> None:
        if _appservices is None:
            raise AccessibilityCheckError(
                "ApplicationServices 框架加载失败，无法检测辅助功能权限")

    def _prompt_options() -> tuple[int, int]:
        """构造 {kAXTrustedCheckOptionPrompt: True} 的 CFDictionary 引用。

        键值回调绑定 CoreFoundation 导出的 kCFTypeDictionary*CallBacks 全局
        结构，保证系统 API 以标准 CF 语义查询键时能够命中。返回
        (字典引用, 键字符串引用)，按 CF"Create"规则由调用方 CFRelease。
        """
        # kCFStringEncodingUTF8
        key = _corefoundation.CFStringCreateWithCString(
            None, b"AXTrustedCheckOptionPrompt", 0x08000100)
        # kCFBooleanTrue 是 CoreFoundation 导出的常量 CFBooleanRef（Get 规则，无需释放）
        true_val = ctypes.c_void_p.in_dll(_corefoundation, "kCFBooleanTrue")
        key_cb = _CFDictCallBacks.in_dll(
            _corefoundation, "kCFTypeDictionaryKeyCallBacks")
        value_cb = _CFDictCallBacks.in_dll(
            _corefoundation, "kCFTypeDictionaryValueCallBacks")
        options = _corefoundation.CFDictionaryCreateMutable(
            None, 1, ctypes.byref(key_cb), ctypes.byref(value_cb))
        _corefoundation.CFDictionarySetValue(options, key, true_val)
        return options, key

    # 空格键虚拟键码（ANSI keycode 0x31），跨所有键盘布局通用，无需经 TIS/
    # HIToolbox 构造的 unicode→keycode 映射表。pynput 的 Controller() 在构造时会
    # 调用 TISCopyCurrentKeyboardInputSource / TISGetInputSourceProperty，而这类输入源
    # API 在 macOS 26.x 上被强制要求主线程（dispatch_assert_queue(main)）；本函数却在
    # 后台自旋触发线程执行，会触发断言使进程 SIGTRAP 闪退。直接投递 CGEvent 既绕开
    # TIS，又因 CGEventPost 线程无关而可安全留在触发线程、不损失亚毫秒触发精度。
    _VK_SPACE = 0x31

    def _send_space() -> None:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventPost,
            kCGHIDEventTap,
        )
        for is_press in (True, False):  # 按下 + 抬起，等价 pynput 的一次 tap
            CGEventPost(kCGHIDEventTap,
                        CGEventCreateKeyboardEvent(None, _VK_SPACE, is_press))

    def accessibility_trusted() -> bool:
        """当前进程是否已获得辅助功能权限。

        检测能力不可用时抛 AccessibilityCheckError（区分"环境异常"与"未授权"）。
        """
        _ensure_checkable()
        return bool(_appservices.AXIsProcessTrusted())

    def request_accessibility_permission() -> bool:
        """向系统发起辅助功能权限申请（R-10），返回申请后是否已授权。

        未授权时系统弹原生申请框，并把当前二进制自动登记进辅助功能列表——
        登记的代码身份是"当下正在申请的进程"，从源头避免旧版本残留条目的
        代码签名要求（csreq）不匹配新二进制导致的授权失效。
        """
        _ensure_checkable()
        if _corefoundation is None:  # pragma: no cover - 极端环境降级为静默检测
            return bool(_appservices.AXIsProcessTrusted())
        options, key = _prompt_options()
        if not options:  # pragma: no cover - 字典创建失败的极端环境防御：宁可少弹窗不闪退
            return bool(_appservices.AXIsProcessTrusted())
        trusted = bool(_appservices.AXIsProcessTrustedWithOptions(options))
        # 申请为同步调用，返回后系统不再引用两个对象，按 Create 规则释放
        _corefoundation.CFRelease(options)
        _corefoundation.CFRelease(key)
        return trusted

    def reset_accessibility_entry() -> bool:
        """用 tccutil 清除本应用在辅助功能服务下的全部授权记录。

        场景：从 ad-hoc 签名旧版本升级而来时，列表残留旧身份条目（显示已勾选，
        对新二进制无效），清除后由 request_accessibility_permission 按当前
        二进制重新登记。tccutil 只操作本应用 bundle id 自己的记录，无需特权。
        """
        try:
            proc = subprocess.run(
                ["tccutil", "reset", "Accessibility", _BUNDLE_ID],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
            return False
        return proc.returncode == 0

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
        if sys.platform == "darwin":
            try:
                trusted = accessibility_trusted()
            except AccessibilityCheckError as exc:
                return False, f"辅助功能权限检测不可用：{exc}"
            if not trusted:
                return False, ("未获得 macOS 辅助功能权限。请按启动引导授权"
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
