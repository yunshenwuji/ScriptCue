"""被控端 GUI 版（tkinter）。

覆盖 PRD 被控端需求：
- R-01 加入房间（房间码/口令/昵称，昵称与服务器地址记忆）
- R-05 本地补偿值配置（接受主控端远程修改）
- R-06 就绪标记
- R-08 触发前提示音（引擎回调）
- R-09 置顶小面板（紧凑模式，可拖动）
- R-10 macOS 辅助功能权限引导
- R-11 开机自检

启动：python -m agent.gui
"""

import asyncio
import contextlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import protocol as p
from .engine import AgentEngine
from .keysender import KeySender
from .logsetup import setup_logging
from .timeutil import now_ms

logger = logging.getLogger("scriptcue.agent")

APP_NAME = "述播 ScriptCue 被控端"
POLL_MS = 60          # UI 轮询引擎事件的间隔
UI_FONT_LARGE = ("Microsoft YaHei UI", 20, "bold")
UI_FONT = ("Microsoft YaHei UI", 10)

CMD_TEXT = {p.CMD_PLAY: "起播", p.CMD_PAUSE: "暂停", p.CMD_TEST: "测试"}

# 服务器线路预设：口述员只需选择线路名，无需感知真实地址（低门槛）
SERVER_PRESETS = {
    "线路1（默认）": "https://sb.kadaiad.fun:4680",
}
CHOICE_CUSTOM = "自定义"


@contextlib.contextmanager
def _silence_c_stderr():
    """临时重定向 C 层 stderr 到空设备。

    Tcl/Tk 运行时用 libpng 解码其自带的 PNG 资源，这些 PNG 内嵌的
    ICC 色彩配置块不符合规范，libpng 会向 stderr 打印
    "iCCP: known incorrect sRGB profile" 警告。该警告无害（图像正常解码）
    且无法通过配置关闭，只能在 UI 构建期间以文件描述符级重定向屏蔽。
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        saved_stderr = os.dup(2)
    except OSError:
        yield
        return
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(saved_stderr, 2)
        os.close(devnull)
        os.close(saved_stderr)

# ---------------------------------------------------------------------------
# 配置持久化（昵称记忆，R-01）
# ---------------------------------------------------------------------------

def config_path() -> Path:
    if sys.platform == "win32":
        import os
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    return base / "ScriptCue" / "agent_config.json"


def load_config() -> dict:
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    # 兼容旧版配置：旧版直接存 server 地址，迁移为"自定义"线路
    if "server_preset" not in cfg and cfg.get("server"):
        cfg["server_preset"] = CHOICE_CUSTOM
        cfg["server_custom"] = cfg["server"]
    return cfg


def save_config(cfg: dict) -> None:
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 提示音（R-08）
# ---------------------------------------------------------------------------

def make_beep_fn():
    if sys.platform == "win32":
        import winsound

        def beep():
            try:
                winsound.Beep(1400, 120)
            except Exception:
                winsound.MessageBeep()
        return beep
    if sys.platform == "darwin":
        def beep():
            try:
                subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        return beep
    return None


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------

class GuiApp:
    def __init__(self):
        self.log_path = setup_logging()
        self.cfg = load_config()
        self.event_queue: queue.Queue = queue.Queue()
        self.engine: AgentEngine | None = None
        self.key_sender: KeySender | None = None
        self.pending: dict[str, dict] = {}   # command_id -> {command, local_fire}
        self.panel: tk.Toplevel | None = None

        with _silence_c_stderr():
            self._build_ui()

    # ---------------- UI 构建 ----------------

    def _build_ui(self):
        root = tk.Tk()
        self.root = root
        root.title(APP_NAME)
        root.geometry("400x560")
        root.minsize(360, 480)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        pad = {"padx": 12, "pady": 4}

        # 连接区
        frm_conn = ttk.LabelFrame(root, text="加入房间")
        frm_conn.pack(fill="x", **pad)

        preset = self.cfg.get("server_preset", "线路1（默认）")
        if preset not in SERVER_PRESETS and preset != CHOICE_CUSTOM:
            preset = "线路1（默认）"
        self.var_server_choice = tk.StringVar(value=preset)
        self.var_server = tk.StringVar(value=self.cfg.get("server_custom", ""))
        self.var_room = tk.StringVar(value=self.cfg.get("room", ""))
        self.var_password = tk.StringVar(value="")
        self.var_nickname = tk.StringVar(value=self.cfg.get("nickname", ""))

        # 服务器线路下拉框（预设线路的真实地址不展示给口述员）
        self._row_server = ttk.Frame(frm_conn)
        self._row_server.pack(fill="x", padx=8, pady=3)
        ttk.Label(self._row_server, text="服务器", width=6).pack(side="left")
        self.cmb_server = ttk.Combobox(
            self._row_server, textvariable=self.var_server_choice,
            values=list(SERVER_PRESETS) + [CHOICE_CUSTOM], state="readonly")
        self.cmb_server.pack(side="left", fill="x", expand=True)
        self.cmb_server.bind("<<ComboboxSelected>>",
                             lambda e: self._sync_server_row())

        # 自定义地址行（仅选择"自定义"时显示）
        self.row_custom = ttk.Frame(frm_conn)
        self._entry_row(self.row_custom, "地址", self.var_server)
        self._sync_server_row()

        self._entry_row(frm_conn, "房间码", self.var_room, upper=True)
        self._entry_row(frm_conn, "口令", self.var_password, show="*")
        self._entry_row(frm_conn, "昵称", self.var_nickname)

        btn_row = ttk.Frame(frm_conn)
        btn_row.pack(fill="x", padx=8, pady=(2, 8))
        self.btn_join = ttk.Button(btn_row, text="加入房间", command=self._join)
        self.btn_join.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_leave = ttk.Button(btn_row, text="断开", command=self._disconnect,
                                    state="disabled")
        self.btn_leave.pack(side="left", expand=True, fill="x")

        # 状态区
        frm_status = ttk.LabelFrame(root, text="状态")
        frm_status.pack(fill="x", **pad)
        self.lbl_conn = tk.Label(frm_status, text="未连接", font=UI_FONT_LARGE,
                                 fg="#b02a2a")
        self.lbl_conn.pack(pady=(6, 0))
        self.lbl_clock = ttk.Label(frm_status, text="时钟：未同步")
        self.lbl_clock.pack()
        self.lbl_comp = ttk.Label(frm_status, text="补偿值：0ms")
        self.lbl_comp.pack(pady=(0, 6))

        # 倒计时区
        frm_count = ttk.LabelFrame(root, text="指令倒计时")
        frm_count.pack(fill="x", **pad)
        self.lbl_countdown = tk.Label(frm_count, text="—", font=("Consolas", 28, "bold"))
        self.lbl_countdown.pack(pady=6)

        # 就绪与补偿
        frm_ready = ttk.Frame(root)
        frm_ready.pack(fill="x", **pad)
        self.var_ready = tk.BooleanVar(value=False)
        self.btn_ready = tk.Button(frm_ready, text="我已就绪", font=("Microsoft YaHei UI", 14, "bold"),
                                   bg="#1a9e55", fg="white", relief="flat",
                                   command=self._toggle_ready)
        self.btn_ready.pack(fill="x", ipady=8)

        frm_comp = ttk.Frame(root)
        frm_comp.pack(fill="x", padx=12, pady=4)
        ttk.Label(frm_comp, text="补偿值(ms)：").pack(side="left")
        self.var_comp = tk.IntVar(value=self.cfg.get("compensation", 0))
        self.spn_comp = ttk.Spinbox(frm_comp, from_=-10000, to=10000, increment=10,
                                    textvariable=self.var_comp, width=8)
        self.spn_comp.pack(side="left", padx=4)
        ttk.Button(frm_comp, text="应用", command=self._apply_comp).pack(side="left")

        # 日志区
        frm_log = ttk.LabelFrame(root, text="运行日志")
        frm_log.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(frm_log, height=5, font=("Consolas", 9),
                               state="disabled", wrap="word")
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)

        # 底部工具行
        frm_tools = ttk.Frame(root)
        frm_tools.pack(fill="x", **pad)
        self.var_topmost = tk.BooleanVar(value=self.cfg.get("topmost", False))
        ttk.Checkbutton(frm_tools, text="窗口置顶", variable=self.var_topmost,
                        command=self._toggle_topmost).pack(side="left")
        ttk.Button(frm_tools, text="打开日志", command=self._open_log).pack(side="right")
        ttk.Button(frm_tools, text="紧凑小面板", command=self._open_panel).pack(side="right", padx=(0, 6))
        self._toggle_topmost()

        self.root.after(POLL_MS, self._poll_events)

    def _sync_server_row(self):
        """根据线路选择显示/隐藏自定义地址输入行。"""
        if self.var_server_choice.get() == CHOICE_CUSTOM:
            self.row_custom.pack(fill="x", after=self._row_server)
        else:
            self.row_custom.pack_forget()

    def _entry_row(self, parent, label, var, show=None, upper=False):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=8, pady=3)
        ttk.Label(row, text=label, width=6).pack(side="left")
        entry = ttk.Entry(row, textvariable=var, show=show or "")
        entry.pack(side="left", fill="x", expand=True)
        if upper:
            entry.bind("<KeyRelease>", lambda e: var.set(var.get().upper()))
        return entry

    # ---------------- 动作 ----------------

    def _join(self):
        choice = self.var_server_choice.get()
        if choice in SERVER_PRESETS:
            server = SERVER_PRESETS[choice]
            server_label = choice
        else:
            server = self.var_server.get().strip()
            server_label = server
            if not server:
                messagebox.showwarning(APP_NAME, "请输入自定义服务器地址")
                return
        room = self.var_room.get().strip().upper()
        nickname = self.var_nickname.get().strip()
        if not room or not nickname:
            messagebox.showwarning(APP_NAME, "请填写房间码和昵称")
            return
        if len(room) != 6:
            messagebox.showwarning(APP_NAME, "房间码必须是 6 位")
            return

        self._save_cfg()
        self.pending.clear()

        self.key_sender = KeySender()
        self.engine = AgentEngine(
            server_url=server, room_code=room, nickname=nickname,
            password=self.var_password.get().strip() or None,
            key_sender=self.key_sender,
            on_event=self.event_queue.put,
            beep_fn=make_beep_fn(),
        )
        self.engine.compensation_ms = int(self.var_comp.get() or 0)

        def runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.engine.run())
            finally:
                loop.close()

        threading.Thread(target=runner, daemon=True, name="engine").start()
        self.btn_join.configure(state="disabled")
        self.btn_leave.configure(state="normal")
        self._log(f"正在通过「{server_label}」加入房间 {room} ...")

    def _disconnect(self):
        if self.engine:
            self.engine.disconnect()
            self.engine = None
        self.pending.clear()
        self.btn_join.configure(state="normal")
        self.btn_leave.configure(state="disabled")
        self._set_conn("未连接", "#b02a2a")
        self._log("已断开连接")

    def _toggle_ready(self):
        if not self.engine:
            return
        new = not self.var_ready.get()
        self.var_ready.set(new)
        self.engine.set_ready(new)
        self.btn_ready.configure(
            text="已就绪（点击取消）" if new else "我已就绪",
            bg="#8a93a6" if new else "#1a9e55")
        self._update_panel()

    def _apply_comp(self):
        try:
            ms = int(self.var_comp.get())
        except (TypeError, ValueError):
            messagebox.showwarning(APP_NAME, "补偿值必须是整数毫秒")
            return
        if self.engine:
            self.engine.set_compensation(ms)
        self.cfg["compensation"] = ms
        save_config(self.cfg)
        self.lbl_comp.configure(text=f"补偿值：{ms}ms")

    def _toggle_topmost(self):
        self.root.attributes("-topmost", self.var_topmost.get())
        self.cfg["topmost"] = self.var_topmost.get()
        save_config(self.cfg)

    def _save_cfg(self):
        self.cfg.update({
            "server_preset": self.var_server_choice.get(),
            "server_custom": self.var_server.get().strip(),
            "room": self.var_room.get().strip().upper(),
            "nickname": self.var_nickname.get().strip(),
        })
        self.cfg.pop("server", None)  # 清理旧版字段
        save_config(self.cfg)

    # ---------------- 引擎事件轮询 ----------------

    def _poll_events(self):
        try:
            while True:
                evt = self.event_queue.get_nowait()
                self._handle_event(evt)
        except queue.Empty:
            pass
        self._update_countdown()
        self.root.after(POLL_MS, self._poll_events)

    def _handle_event(self, evt: dict):
        event = evt.get("event")
        if event == "connecting":
            self._set_conn("连接中…", "#d98a00")
        elif event == "connected":
            self._set_conn("已连接", "#1a9e55")
            self._log(f"已加入房间 {evt.get('room_code')}，开始时钟同步")
        elif event == "disconnected":
            self._set_conn("连接断开", "#b02a2a")
        elif event == "reconnecting":
            self._set_conn(f"断线重连中（{evt.get('delay_s')}s）", "#d98a00")
        elif event == "error":
            self._log(f"错误: {evt.get('message')}")
        elif event == "clock_sample":
            self.lbl_clock.configure(
                text=f"时钟：{evt.get('quality')} · 偏移 {evt.get('offset_ms')}ms · "
                     f"RTT {evt.get('rtt_ms')}ms（样本 {evt.get('samples')}）")
        elif event == "command_scheduled":
            self.pending[evt["command_id"]] = {
                "command": evt.get("command"), "local_fire": evt["local_fire"],
            }
            self._log(f"收到{CMD_TEXT.get(evt.get('command'), '')}指令，"
                      f"{evt.get('remaining_ms')}ms 后触发")
        elif event == "command_fired":
            self.pending.pop(evt["command_id"], None)
            self._log(f"已触发，偏差 {evt.get('delta_ms')}ms（{evt.get('status')}）")
        elif event == "command_cancelled":
            self.pending.pop(evt.get("command_id"), None)
            self._log("指令已取消")
        elif event == "fire_skipped":
            self._log(f"触发跳过：{evt.get('reason')}")
        elif event == "comp_changed":
            ms = evt.get("compensation_ms", 0)
            self.var_comp.set(ms)
            self.lbl_comp.configure(text=f"补偿值：{ms}ms")
            self._log(f"补偿值已更新为 {ms}ms（来自主控端）")

    def _update_countdown(self):
        if not self.pending:
            self.lbl_countdown.configure(text="—", fg="#1c2333")
            self._panel_countdown("—")
            return
        # 显示最近的一条
        item = min(self.pending.values(), key=lambda i: i["local_fire"])
        remain_ms = item["local_fire"] - now_ms()
        label = CMD_TEXT.get(item["command"], "")
        if remain_ms > 0:
            text = f"{label} {remain_ms / 1000:.1f}s"
            fg = "#d98a00" if remain_ms > 3000 else "#b02a2a"
        else:
            text = f"{label}执行中"
            fg = "#b02a2a"
        self.lbl_countdown.configure(text=text, fg=fg)
        self._panel_countdown(text)

    def _set_conn(self, text, color):
        self.lbl_conn.configure(text=text, fg=color)
        self._update_panel()

    def _log(self, text):
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{ts}] {text}\n")
        self.txt_log.see("end")
        # 日志行数上限
        if int(self.txt_log.index("end-1c").split(".")[0]) > 200:
            self.txt_log.delete("1.0", "2.0")
        self.txt_log.configure(state="disabled")

    # ---------------- 紧凑小面板（R-09） ----------------

    def _open_panel(self):
        if self.panel is not None and self.panel.winfo_exists():
            self.panel.lift()
            return
        with _silence_c_stderr():
            panel = tk.Toplevel(self.root)
        self.panel = panel
        panel.title("述播小面板")
        panel.overrideredirect(True)
        panel.attributes("-topmost", True)
        panel.geometry("180x86+%d+%d" % (self.root.winfo_x() + 40,
                                         self.root.winfo_y() + 60))
        panel.configure(bg="#16213e")

        self.panel_status = tk.Label(panel, text="未连接", bg="#16213e", fg="#fff",
                                     font=("Microsoft YaHei UI", 11, "bold"))
        self.panel_status.pack(pady=(8, 0))
        self.panel_count = tk.Label(panel, text="—", bg="#16213e", fg="#ffd166",
                                    font=("Consolas", 18, "bold"))
        self.panel_count.pack()

        # 拖动
        def drag_start(e):
            panel._dx, panel._dy = e.x, e.y

        def drag_move(e):
            panel.geometry(f"+{panel.winfo_x() + e.x - panel._dx}"
                           f"+{panel.winfo_y() + e.y - panel._dy}")

        for w in (panel, self.panel_status, self.panel_count):
            w.bind("<ButtonPress-1>", drag_start)
            w.bind("<B1-Motion>", drag_move)
        # 双击收回
        panel.bind("<Double-Button-1>", lambda e: panel.destroy())
        panel.protocol("WM_DELETE_WINDOW", panel.destroy)
        self._update_panel()

    def _update_panel(self):
        if self.panel is None or not self.panel.winfo_exists():
            return
        if self.engine and self.engine.connected:
            ready = "就绪" if self.var_ready.get() else "未就绪"
            self.panel_status.configure(text=f"已连接 · {ready}", fg="#7ff0b0")
        else:
            self.panel_status.configure(text=self.lbl_conn.cget("text"), fg="#ffb4b4")

    def _panel_countdown(self, text):
        if self.panel is not None and self.panel.winfo_exists():
            self.panel_count.configure(text=text)

    # ---------------- 日志导出 ----------------

    def _open_log(self):
        """在系统文件管理器中定位日志文件，方便用户发给开发者。"""
        path = self.log_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                if path.exists():
                    subprocess.Popen(["explorer", "/select,", str(path)])
                else:
                    os.startfile(str(path.parent))  # noqa: S606
            elif sys.platform == "darwin":
                if path.exists():
                    subprocess.Popen(["open", "-R", str(path)])
                else:
                    subprocess.Popen(["open", str(path.parent)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法打开日志目录：\n{exc}\n\n日志路径：{path}")

    # ---------------- 生命周期 ----------------

    def _on_close(self):
        logger.info("用户关闭窗口，退出")
        if self.engine:
            self.engine.disconnect()
        self.root.destroy()

    def run(self):
        self._startup_check()
        self.root.mainloop()

    def _startup_check(self):
        """开机自检（R-11）与 macOS 权限引导（R-10）。"""
        if sys.platform == "darwin":
            from .keysender import accessibility_trusted, open_accessibility_settings
            while not accessibility_trusted():
                retry = messagebox.askretrycancel(
                    APP_NAME,
                    "述播需要「辅助功能」权限才能模拟按键。\n\n"
                    "点击「重试」将打开系统设置的对应页面：\n"
                    "隐私与安全性 → 辅助功能 → 勾选本程序。\n"
                    "（授权后可能需要重新启动本程序）")
                if not retry:
                    self.root.destroy()
                    sys.exit(1)
                open_accessibility_settings()

        sender = KeySender()
        ok, message = sender.check()
        if not ok:
            logger.error("开机自检失败: %s", message)
            messagebox.showerror(APP_NAME, f"开机自检失败：\n{message}")
        else:
            logger.info("按键自检: %s", message)
            self._log(message)


def main():
    app = GuiApp()
    app.run()


if __name__ == "__main__":
    main()
