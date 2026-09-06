"""「我已就绪」自绘按钮的聚焦检查：观感派生与交互语义。

背景：经典 tk.Button 在 macOS Aqua 上忽略 -background/-relief，导致
绿底不生效而白色文字照常渲染（白底白字，深浅模式皆然）。修复方案是
用 tk.Label 自绘的 _ReadyButton 替换。本检查覆盖三部分：

1. 颜色加深纯函数 _shade（不依赖 Tk）；
2. 交互观感派生 _look_color：按压 > 悬停 > 基准色；
3. 控件级事件语义（需 Tk 环境，无显示环境时自动跳过）：
   configure(bg=) 的基准色语义、鼠标按下→拖出→拖回→松开的 armed
   触发语义、空格/回车触发、焦点环恒占位。

事件均为 event_generate 合成事件，只派发给目标控件本身：
不连网、不注入真实系统按键、不触碰任何真实窗口。

在仓库根目录运行：
    python -m unittest discover -s agent/tests -t agent -v
"""

import unittest

try:
    import tkinter as tk
except Exception:  # pragma: no cover - 无 Tk 运行时时跳过控件检查
    tk = None

from agent.gui import (COLOR_READY, COLOR_READY_ON,
                       _ReadyButton, _look_color, _shade)

GREEN = COLOR_READY
GREY = COLOR_READY_ON


class ShadeTest(unittest.TestCase):
    """颜色加深纯函数。"""

    def test_factor_zero_keeps_color(self):
        self.assertEqual(_shade(GREEN, 0.0), GREEN)

    def test_factor_one_is_black(self):
        self.assertEqual(_shade(GREEN, 1.0), "#000000")

    def test_known_value(self):
        # 0x1a=26→21, 0x9e=158→126, 0x55=85→68
        self.assertEqual(_shade(GREEN, 0.2), "#157e44")

    def test_result_stays_in_range(self):
        # 每个分量都被钳制在 0~255 且以两位十六进制输出
        self.assertEqual(_shade("#010203", 0.9), "#000000")
        self.assertEqual(len(_shade("#abcdef", 0.5)), 7)

    def test_invalid_inputs_returned_as_is(self):
        for bad in ("", "green", "#12345", "#12g45z", None, 123):
            with self.subTest(value=bad):
                self.assertEqual(_shade(bad, 0.5), bad)


class LookColorTest(unittest.TestCase):
    """观感派生：按压 > 悬停 > 基准色。"""

    def test_base_when_idle(self):
        self.assertEqual(_look_color(GREEN, False, False), GREEN)

    def test_hover_darkens(self):
        self.assertEqual(_look_color(GREEN, True, False), _shade(GREEN, 0.08))

    def test_pressed_darkens_more_than_hover(self):
        pressed = _look_color(GREEN, False, True)
        hovered = _look_color(GREEN, True, False)
        self.assertEqual(pressed, _shade(GREEN, 0.20))
        self.assertNotEqual(pressed, hovered)

    def test_pressed_overrides_hover(self):
        self.assertEqual(_look_color(GREEN, True, True), _shade(GREEN, 0.20))

    def test_works_for_ready_grey(self):
        # 就绪态（灰）同样能派生出可区分的三档颜色
        self.assertEqual(_look_color(GREY, False, False), GREY)
        self.assertNotEqual(_look_color(GREY, True, False), GREY)
        self.assertNotEqual(_look_color(GREY, False, True), GREY)


@unittest.skipIf(tk is None, "无 Tk 运行时")
class ReadyButtonEventTest(unittest.TestCase):
    """控件级检查：需要可用的 Tk 环境（无显示环境自动跳过）。

    事件流用 event_generate 合成，验证的是 _ReadyButton 的绑定语义，
    不依赖真实鼠标键盘。
    """

    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("无显示环境，跳过控件检查")
        # 注意：不能用 withdraw()——未映射窗口会吞掉 event_generate 的合成
        # 事件；也不要设 -alpha（Windows 分层窗口会导致焦点行为不稳定）。
        # 用无边的普通映射窗口即可（实测 focus+Key 事件 5/5 稳定）。
        self.root.geometry("200x200+0+0")
        self.root.overrideredirect(True)
        self.count = 0
        self.btn = _ReadyButton(self.root, command=self._bump,
                                text="我已就绪", bg=GREEN, fg="white")
        self.btn.pack()
        self.root.update()

    def tearDown(self):
        self.root.destroy()

    def _bump(self):
        self.count += 1

    def _focus(self):
        """键盘事件（<space>/<Return>）派发给焦点控件；focus_force 拉稳焦点。"""
        self.btn.focus_force()
        self.root.update()

    # -- 事件序列工具 --

    BTN1_MASK = 0x100  # Button1 状态掩码：<B1-Enter>/<B1-Leave> 绑定匹配它

    def _click(self):
        self.btn.event_generate("<Button-1>", x=4, y=4)
        self.root.update()
        self.btn.event_generate("<ButtonRelease-1>", x=4, y=4)
        self.root.update()

    def _drag_out(self):
        """按住并拖出：合成 Leave 时必须携带 Button1 掩码，
        否则 <B1-Leave> 绑定不匹配（真实鼠标事件自带该掩码）。"""
        self.btn.event_generate("<Button-1>", x=4, y=4)
        self.root.update()
        self.btn.event_generate("<Leave>", x=999, y=999,
                                state=self.BTN1_MASK)
        self.root.update()

    def _drag_back(self):
        self.btn.event_generate("<Enter>", x=4, y=4,
                                state=self.BTN1_MASK)
        self.root.update()

    # -- 基准色与 API 兼容 --

    def test_initial_look_uses_base_color(self):
        self.assertEqual(self.btn.cget("bg"), GREEN)
        self.assertEqual(self.btn.cget("text"), "我已就绪")

    def test_configure_bg_switches_base_color(self):
        """_toggle_ready 的 configure(bg=) 语义：切换基准色且立即可见。"""
        self.btn.configure(text="已就绪（点击取消）", bg=GREY)
        self.root.update()
        self.assertEqual(self.btn.cget("text"), "已就绪（点击取消）")
        self.assertEqual(self.btn.cget("bg"), GREY)
        self.assertEqual(self.btn.cget("highlightbackground"), GREY,
                         "非聚焦时的焦点环应与底色同色（隐形占位）")

    def test_configure_bg_with_dict_form(self):
        """configure({dict}) 形式同样更新基准色。"""
        self.btn.configure({"bg": GREY})
        self.root.update()
        self.assertEqual(self.btn.cget("bg"), GREY)

    def test_focus_thickness_constant(self):
        """聚焦与否布局不跳动：highlightthickness 恒为 FOCUS_WIDTH。"""
        self.assertEqual(int(self.btn.cget("highlightthickness")),
                         _ReadyButton.FOCUS_WIDTH)
        self.btn.focus_set()
        self.root.update()
        self.assertEqual(int(self.btn.cget("highlightthickness")),
                         _ReadyButton.FOCUS_WIDTH)

    def test_invoke_calls_command(self):
        self.btn.invoke()
        self.assertEqual(self.count, 1)

    # -- 鼠标 armed 语义 --

    def test_click_in_place_triggers_once(self):
        self._click()
        self.assertEqual(self.count, 1)

    def test_press_show_pressed_look_then_released_restores(self):
        self.btn.event_generate("<Button-1>", x=4, y=4)
        self.root.update()
        self.assertEqual(self.btn.cget("bg"), _look_color(GREEN, False, True))
        self.btn.event_generate("<ButtonRelease-1>", x=4, y=4)
        self.root.update()
        self.assertEqual(self.btn.cget("bg"), GREEN)

    def test_drag_out_cancels_trigger(self):
        """按住拖出后松开不触发（对齐原生防误触语义）。"""
        self._drag_out()
        self.btn.event_generate("<ButtonRelease-1>", x=999, y=999)
        self.root.update()
        self.assertEqual(self.count, 0)
        self.assertEqual(self.btn.cget("bg"), GREEN,
                         "拖出后按压外观应已恢复")

    def test_drag_back_rearms_trigger(self):
        """拖出后拖回再松开，应恢复触发。"""
        self._drag_out()
        self._drag_back()
        self.btn.event_generate("<ButtonRelease-1>", x=4, y=4)
        self.root.update()
        self.assertEqual(self.count, 1)

    def test_double_click_triggers_twice(self):
        self._click()
        self._click()
        self.assertEqual(self.count, 2)

    def test_release_without_press_does_not_trigger(self):
        """没有按下直接收到松开事件（合成事件流乱序）不应触发。"""
        self.btn.event_generate("<ButtonRelease-1>", x=4, y=4)
        self.root.update()
        self.assertEqual(self.count, 0)

    # -- 键盘语义 --

    def test_space_triggers_and_keeps_pressed_look_until_release(self):
        """对齐 button.tcl：<space> 按下即触发并保持按压外观，松开恢复。"""
        self._focus()
        self.btn.event_generate("<space>")
        self.root.update()
        self.assertEqual(self.count, 1)
        self.assertEqual(self.btn.cget("bg"), _look_color(GREEN, False, True))
        self.btn.event_generate("<KeyRelease-space>")
        self.root.update()
        self.assertEqual(self.btn.cget("bg"), GREEN)

    def test_return_triggers_without_pressed_look(self):
        """对齐 button.tcl：<Return> 直接触发，不进入按压外观。"""
        self._focus()
        self.btn.event_generate("<Return>")
        self.root.update()
        self.assertEqual(self.count, 1)
        self.assertEqual(self.btn.cget("bg"), GREEN)


if __name__ == "__main__":
    unittest.main()
