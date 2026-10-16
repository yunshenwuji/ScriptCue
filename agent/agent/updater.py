"""被控端自动更新：清单检查、下载校验、替换重启、回退与健康确认。

核心原则（与 docs/auto-update.md 同步）：**下载可自动，替换必须人点，演出中永不动手**。
- 安全窗口 safety_window()：未连接，或"未就绪 + 无待触发指令"；下载前与替换前都校验；
- 清单来自服务端 GET /update/manifest.json 或 update.available 推送（同一套处理）；
- 下载 sha256 双取：下载前后各取一次清单，目标资产的 sha256/版本一致才认，
  不一致则重下，最多 3 轮后放弃且不触碰现有安装；
- Windows：运行中的 exe 允许改名 → 换入新 exe → detached 重启；
- macOS：tar.gz 解包到 .app 同目录 → 比对签名身份 → selftest → detached sh 脚本
  在进程退出后原子替换并 open 重启（新 bundle 由本进程落盘，不带 quarantine，
  升级后不再要求"仍要打开"）；
- 回退：启动健康状态机（startup_health_gate，建 Tk 之前运行）把每次启动乐观标记
  为"已武装、未正常收尾"，正常退出由 note_clean_exit 覆写；更新后连续 2 次启动
  未正常收尾自动回退 .old；健康运行 60s 后标记 confirmed=true；.old 保留到
  下一次更新替换时删除，供用户随时手动回退。

本模块不依赖 tkinter：GUI 通过构造参数注入事件回调、安全窗口与服务器地址来源，
因此可在无显示环境下被 agent/tests 直接测试。
"""

import contextlib
import datetime
import hashlib
import json
import logging
import os
import platform as platform_mod
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

from .tls import build_ssl_context
from .version import APP_VERSION

logger = logging.getLogger("scriptcue.agent")

# ---- 节奏与超时 ----
FIRST_CHECK_DELAY_S = 15          # 启动后首查延迟（避开开机自检与 macOS 权限引导）
CHECK_INTERVAL_S = 6 * 3600       # 定时检查间隔
WINDOW_POLL_S = 5                 # 等待演出安全窗口的轮询间隔
HEALTH_CONFIRM_S = 60             # 更新后健康确认时长
ROLLBACK_AFTER_CRASHES = 2        # 连续多少次"启动未正常收尾"后自动回退上一版
HTTP_TIMEOUT_S = 10               # 清单请求超时
DOWNLOAD_TIMEOUT_S = 60           # 下载单次读超时
MAX_MANIFEST_BYTES = 1024 * 1024
SELFTEST_TIMEOUT_WIN_S = 90       # 待装 exe 的 --selftest 超时
SELFTEST_TIMEOUT_MAC_S = 45       # 待装 .app 的 --selftest 超时
MAX_DOWNLOAD_ROUNDS = 3           # sha256 双取不一致的最大重试轮数

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_SCHEMA = 1


class UpdateError(RuntimeError):
    """更新流程的可预期失败：降级为日志/提示，绝不影响按键触发主链路。"""


# ---------------------------------------------------------------------------
# 路径与环境
# ---------------------------------------------------------------------------

def app_dir() -> Path:
    """应用数据目录（与 gui.config_path() 同源：Windows %APPDATA%/ScriptCue，
    macOS ~/.config/ScriptCue；此处独立实现以免 updater ↔ gui 循环导入）。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".config"
    return base / "ScriptCue"


def updates_dir() -> Path:
    """已下载更新包的存放目录。"""
    return app_dir() / "updates"


def state_path() -> Path:
    """更新状态文件（update-state.json）。"""
    return app_dir() / "update-state.json"


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 纯函数：地址推导、版本比较、清单校验、安全窗口
# ---------------------------------------------------------------------------

def http_base(server_str: str) -> str:
    """由 GUI 线路值推导 HTTP 根：wss→https、ws→http、http(s) 原样、裸主机名→https，
    并去掉尾部 /ws 与斜杠。无法解析时返回空串。"""
    s = (server_str or "").strip()
    if not s:
        return ""
    if s.startswith("wss://"):
        s = "https://" + s[len("wss://"):]
    elif s.startswith("ws://"):
        s = "http://" + s[len("ws://"):]
    elif "://" not in s:
        s = "https://" + s
    s = s.rstrip("/")
    if s.endswith("/ws"):
        s = s[:-len("/ws")]
    return s.rstrip("/")


def manifest_url_for(server_str: str) -> str:
    """服务端更新清单地址（http_base + /update/manifest.json）。"""
    base = http_base(server_str)
    return base + "/update/manifest.json" if base else ""


def ver_tuple(s: str | None) -> tuple[int, ...] | None:
    """把 "1.0.3" 解析为 (1, 0, 3)；含非数字段（如 "0.0.0.dev"）返回 None。

    返回 None 即视为开发版：整个更新流程直接跳过。
    """
    if not isinstance(s, str) or not s.strip():
        return None
    parts = s.strip().split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def platform_key(plat: str | None = None, machine: str | None = None) -> str | None:
    """当前平台键 <os>-<arch>（如 windows-x64 / macos-arm64）；不支持返回 None。"""
    plat = plat if plat is not None else sys.platform
    if machine is None:
        machine = platform_mod.machine()
    os_name = {"win32": "windows", "darwin": "macos"}.get(plat)
    arch = {"amd64": "x64", "x86_64": "x86_64",
            "arm64": "arm64", "aarch64": "arm64"}.get((machine or "").lower())
    if os_name is None or arch is None:
        return None
    return f"{os_name}-{arch}"


def validate_manifest(m: dict) -> dict:
    """校验清单结构（schema/version/assets），非法时抛 UpdateError。

    比服务端同名校验更严的一点：资产文件名不得含路径分隔符——客户端会用它
    作为落地文件名，必须杜绝路径穿越。
    """
    if not isinstance(m, dict):
        raise UpdateError("清单必须是 JSON 对象")
    if m.get("schema") != _SUPPORTED_SCHEMA:
        raise UpdateError(f"不支持的清单 schema: {m.get('schema')!r}")
    version = m.get("version")
    if not isinstance(version, str) or not version.strip():
        raise UpdateError("清单缺少 version")
    assets = m.get("assets")
    if not isinstance(assets, dict) or not assets:
        raise UpdateError("清单缺少 assets")
    for key, asset in assets.items():
        if not isinstance(asset, dict):
            raise UpdateError(f"资产 {key} 不是对象")
        name = asset.get("asset")
        if (not isinstance(name, str) or not name
                or "/" in name or "\\" in name or name in (".", "..")):
            raise UpdateError(f"资产 {key} 的 asset 文件名非法")
        if not _SHA256_RE.fullmatch(str(asset.get("sha256", ""))):
            raise UpdateError(f"资产 {key} 的 sha256 非法")
        if not isinstance(asset.get("size"), int) or asset["size"] <= 0:
            raise UpdateError(f"资产 {key} 的 size 非法")
    return m


def select_asset(manifest: dict, key: str) -> dict | None:
    """按平台键选取资产；键不存在时返回 None（调用方提示手动下载，不报错）。"""
    asset = manifest.get("assets", {}).get(key)
    return asset if isinstance(asset, dict) else None


def safety_window(engine, gui_pending) -> bool:
    """演出安全窗口：未连接，或已连接但"未就绪 + 引擎与 GUI 两侧均无待触发指令"。"""
    if engine is None or not engine.connected:
        return True
    if engine.ready:
        return False
    if getattr(engine, "_pending_fires", None):
        return False
    return not gui_pending


# ---------------------------------------------------------------------------
# 纯函数：HTTP 获取、下载与校验
# ---------------------------------------------------------------------------

def hash_file(path: Path) -> str:
    """流式计算文件的 sha256（十六进制小写）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_manifest(url: str, timeout: float = HTTP_TIMEOUT_S) -> dict:
    """GET 并解析清单 JSON；失败抛 UpdateError/OSError 由调用方降级。"""
    ctx = build_ssl_context() if url.startswith("https://") else None
    req = urllib.request.Request(url, headers={
        "User-Agent": f"ScriptCueAgent/{APP_VERSION}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("清单体积超过上限")
    try:
        # utf-8-sig：兼容带 BOM 的清单（Windows 工具链写文件的常见情形）
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"清单不是合法 JSON: {exc}") from exc
    return data


def stream_download(url: str, dest: Path, expected_size: int,
                    on_progress=None, timeout: float = DOWNLOAD_TIMEOUT_S) -> None:
    """流式下载到 dest（调用方传入 .part 临时路径），按百分比回调进度。"""
    ctx = build_ssl_context() if url.startswith("https://") else None
    req = urllib.request.Request(
        url, headers={"User-Agent": f"ScriptCueAgent/{APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp, \
            open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0) or expected_size or 0
        got = 0
        last_reported = -1
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if on_progress is not None and total > 0:
                percent = min(100, int(got * 100 / total))
                if percent != last_reported:
                    last_reported = percent
                    with contextlib.suppress(Exception):
                        on_progress(percent)


def asset_url(manifest: dict, asset: dict, manifest_url: str) -> str:
    """资产下载地址：资产自带的 url（服务端注入）优先，否则用清单 URL 同目录拼接。"""
    url = asset.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    return urljoin(manifest_url, asset["asset"])


def download_verified(manifest_url: str, asset_key: str, dest_dir: Path, *,
                      fetch_fn=None, stream_fn=None, on_progress=None,
                      max_rounds: int = MAX_DOWNLOAD_ROUNDS) -> dict:
    """下载并双重校验目标资产，成功后返回 {path, version, sha256, size, notes, asset}。

    每轮流程：取清单 A → 下载 → 校验 A.sha256 → 再取清单 B → A/B 的目标资产
    sha256 与版本一致才认（防下载期间发布新版）；任一环节不符则重下，
    最多 max_rounds 轮后抛 UpdateError。失败全程不触碰现有安装。
    """
    fetch_fn = fetch_fn or fetch_manifest
    stream_fn = stream_fn or stream_download
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    last_error = "下载校验失败"

    for round_no in range(1, max_rounds + 1):
        m1 = validate_manifest(fetch_fn(manifest_url))
        a1 = select_asset(m1, asset_key)
        if a1 is None:
            raise UpdateError(f"清单中没有当前平台（{asset_key}）的更新包")
        dest = dest_dir / a1["asset"]
        part = dest.with_name(dest.name + ".part")
        try:
            stream_fn(asset_url(m1, a1, manifest_url), part, a1["size"], on_progress)
            digest = hash_file(part)
            if digest != a1["sha256"]:
                last_error = f"sha256 校验失败（第 {round_no} 轮，实际 {digest[:12]}…）"
                logger.warning("更新包校验失败，将重下: %s", last_error)
                continue
            m2 = validate_manifest(fetch_fn(manifest_url))
            a2 = select_asset(m2, asset_key)
            if (a2 is None or a2["sha256"] != a1["sha256"]
                    or m2["version"] != m1["version"]):
                last_error = f"下载期间发布方更新了清单（第 {round_no} 轮）"
                logger.warning("%s，将重下", last_error)
                continue
            os.replace(part, dest)
            logger.info("更新包下载完成: %s v%s（%d 字节）",
                        dest.name, m1["version"], a1["size"])
            return {"path": dest, "version": m1["version"], "sha256": digest,
                    "size": a1["size"], "notes": m1.get("notes") or "",
                    "asset": a1}
        finally:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)

    raise UpdateError(f"更新下载失败（已尝试 {max_rounds} 轮）：{last_error}")


# ---------------------------------------------------------------------------
# selftest 与替换原语（注入 popen/exit 供干跑检查）
# ---------------------------------------------------------------------------

# PyInstaller onefile 通过环境变量把"子进程模式"传给自身；若新进程继承了这些变量，
# bootloader 会误把自己当作 onefile 的内部子进程并执行父进程可执行文件校验：
# 父进程（本进程）的 exe 已被改名为 *.old，校验必然失败，新版本会弹
# "Security validation failure: parent process has different executable!" 拒绝启动。
# 重启/回退/spawn 一律用剔除这些变量的干净环境（等价于用户双击启动的环境）。
_PYI_ENV_PREFIX = "_PYI"
_PYI_ENV_LEGACY = ("_MEIPASS2",)


def cleaned_child_env() -> dict:
    """剔除 PyInstaller 内部环境变量的环境副本。"""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(_PYI_ENV_PREFIX) and k not in _PYI_ENV_LEGACY}


def run_selftest(binary: Path, timeout_s: int, *, popen_fn=None) -> tuple[bool, str]:
    """对待装产物跑 `--selftest` 准入闸：返回 (是否通过, 说明)。

    超时、非零退出、无法启动均视为不通过（调用方据此放弃替换并保留现有安装）。
    """
    if popen_fn is None:
        def popen_fn(argv):
            return subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    env=cleaned_child_env())
    try:
        proc = popen_fn([str(binary), "--selftest"])
    except OSError as exc:
        return False, f"无法启动候选程序: {exc}"
    try:
        code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            proc.kill()
        return False, f"预检超时（{timeout_s}s）"
    if code != 0:
        return False, f"预检未通过（退出码 {code}）"
    return True, "预检通过"


def _default_popen_detached(argv):
    """以脱离本进程的方式启动新程序（Windows detached；POSIX start_new_session）。

    环境必须剔除 PyInstaller 内部变量（见 cleaned_child_env）：否则重启后的新
    bootloader 会把本进程当作 onefile 内部父进程做可执行文件校验而拒绝启动。
    """
    kwargs = {"close_fds": True, "cwd": str(Path(argv[0]).resolve().parent),
              "env": cleaned_child_env()}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def replace_windows(exe: Path, new_exe: Path, *, popen_fn=None, exit_fn=None) -> None:
    """Windows 原地替换（onefile exe）：改名旧版 → 换入新版 → detached 重启。

    运行中的 exe 允许改名、不允许覆写；任一步失败立即复原 `.old` 并抛出，
    绝不留下半替换的安装。成功后本进程 os._exit(0)，永不返回。
    """
    popen_fn = popen_fn or _default_popen_detached
    exit_fn = exit_fn or os._exit
    old = exe.with_name(exe.name + ".old")
    if old.exists():          # 旧回退点让位（.old 仅保留最近一次）
        old.unlink()
    os.replace(exe, old)
    try:
        shutil.move(str(new_exe), str(exe))
    except Exception:
        os.replace(old, exe)  # 复原，现有安装不受损
        raise
    logger.info("已替换程序文件: %s（旧版保留为 %s）", exe, old.name)
    popen_fn([str(exe)])
    exit_fn(0)


def rollback_windows(exe: Path, *, popen_fn=None, exit_fn=None) -> None:
    """Windows 回退：把 `.old` 换回原位并 detached 重启；成功后 os._exit(0)。"""
    popen_fn = popen_fn or _default_popen_detached
    exit_fn = exit_fn or os._exit
    old = exe.with_name(exe.name + ".old")
    if not old.exists():
        raise UpdateError("没有可回退的上一版本")
    tmp = exe.with_name(exe.name + ".rollback-tmp")
    with contextlib.suppress(OSError):
        tmp.unlink(missing_ok=True)
    os.replace(exe, tmp)      # 运行中的 exe 允许改名
    try:
        os.replace(old, exe)
    except Exception:
        os.replace(tmp, exe)  # 复原
        raise
    logger.warning("已回退到上一版本（被替换的版本保留为 %s）", tmp.name)
    popen_fn([str(exe)])
    exit_fn(0)


# ---------------------------------------------------------------------------
# macOS：bundle 定位、解包、签名比对、替换脚本
# ---------------------------------------------------------------------------

def locate_bundle() -> Path | None:
    """定位当前 .app：须为打包运行（sys.frozen）且路径形如 *.app/Contents/MacOS/*。"""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if exe.parent.name != "MacOS" or exe.parent.parent.name != "Contents":
        return None
    bundle = exe.parents[2]
    return bundle if bundle.name.endswith(".app") else None


def extract_update(tar_gz: Path, staging_root: Path) -> Path:
    """解包更新 tar.gz 到 staging_root，校验顶层为唯一的 .app 并返回其路径。

    filter="data" 拒绝绝对路径/逃逸链接并剥掉 setuid 位；旧 Python（<3.10.12）
    不接受 filter 参数时退回不带 filter。
    """
    staging_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_gz, "r:gz") as tf:
        try:
            tf.extractall(staging_root, filter="data")
        except TypeError:  # pragma: no cover - 兼容旧 3.10
            tf.extractall(staging_root)
    tops = [p for p in staging_root.iterdir() if not p.name.startswith("._")]
    apps = [p for p in tops if p.is_dir() and p.name.endswith(".app")]
    if len(apps) != 1:
        raise UpdateError("更新包内容异常：未找到唯一的 .app")
    return apps[0]


def parse_authority(codesign_stderr: str) -> str | None:
    """从 codesign -dvv 的输出里解析 Authority= 行；ad-hoc/未签名返回 None。"""
    m = re.search(r"^Authority=(.+)$", codesign_stderr or "", re.MULTILINE)
    return m.group(1).strip() if m else None


def codesign_authority(bundle: Path) -> str | None:
    """取 bundle 的签名身份；命令不可用/无 Authority 时返回 None。"""
    try:
        proc = subprocess.run(["codesign", "-dvv", str(bundle)],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - macOS 专用
        return None
    return parse_authority(proc.stderr)


def verify_codesign(bundle: Path) -> None:
    """codesign --verify --deep --strict；未通过抛 UpdateError。"""
    try:
        proc = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(bundle)],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover - macOS 专用
        raise UpdateError(f"签名校验无法执行: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise UpdateError(f"待装应用签名校验未通过: {detail}")


def check_codesign_compat(current_bundle: Path, staged_bundle: Path,
                          manifest_authority: str | None) -> None:
    """签名身份一致性检查：三方（当前/待装/清单声明）已知身份必须一致。

    TCC 辅助功能授权与签名身份（csreq）绑定：身份一变，授权即失效。
    任一为 ad-hoc（None）时跳过比对（此时授权本来就与 cdhash 绑定）。
    """
    auths = {
        "当前版本": codesign_authority(current_bundle),
        "待装版本": codesign_authority(staged_bundle),
        "清单声明": manifest_authority if manifest_authority else None,
    }
    known = {name: a for name, a in auths.items() if a}
    if len(set(known.values())) > 1:
        detail = "；".join(f"{name}={a}" for name, a in known.items())
        raise UpdateError(f"签名身份不一致（{detail}），为避免辅助功能授权失效已取消更新")


def _sh_swap_script(bundle: Path, staged_app: Path, staging_root: Path) -> str:
    """生成 detached sh 替换脚本：等主进程退出 → 旧版改名为 .old → 新版上位 → open。

    新版上位失败时清理暂存副本、把 .old 换回并重启旧版（脚本级兜底，绝不让
    用户失去可用安装，也不在安装目录留下 .new-* 残留）。
    """
    q = shlex.quote
    old = str(bundle) + ".old"
    lines = [
        "#!/bin/sh",
        f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done",
        # 剔除 PyInstaller 内部环境变量：避免新进程误入 onefile 子进程模式做父进程校验
        "unset _PYI_ARCHIVE_FILE _PYI_APPLICATION_HOME_DIR "
        "_PYI_PARENT_PROCESS_LEVEL _PYI_SPLASH_IPC _MEIPASS2",
        f"rm -rf {q(old)}",
        f"mv {q(str(bundle))} {q(old)} || exit 1",
        f"if mv {q(str(staged_app))} {q(str(bundle))}; then",
        f"  rm -rf {q(str(staging_root))}",
        f"  open {q(str(bundle))}",
        "else",
        f"  rm -rf {q(str(staging_root))}",
        f"  mv {q(old)} {q(str(bundle))} || true",
        f"  open {q(str(bundle))}",
        "fi",
    ]
    return "\n".join(lines) + "\n"


def replace_macos(bundle: Path, staged_app: Path, *,
                  popen_fn=None, exit_fn=None) -> None:
    """macOS 替换：派发 detached sh 脚本完成"等退出 → 原子替换 → 重启"，本进程退出。

    用脚本而非原地替换：运行中的 bundle 不宜自我覆写，且重启必须在进程退出之后。
    成功后 os._exit(0)，永不返回。
    """
    if popen_fn is None:
        def popen_fn(argv):
            return subprocess.Popen(argv, start_new_session=True, close_fds=True,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
    exit_fn = exit_fn or os._exit
    staging_root = staged_app.parent
    script = _sh_swap_script(bundle, staged_app, staging_root)
    popen_fn(["/bin/sh", "-c", script])
    logger.info("已派发替换脚本，等待主进程退出后完成替换并重启")
    exit_fn(0)


def _sh_rollback_script(bundle: Path) -> str:
    """生成的回退脚本：等主进程退出 → 当前版改名 .bad → .old 上位 → open。

    open 后清除 .bad——它是被回退掉的新版（更新检查会再次发现并可重新下载）
    或崩溃版本，不应长期占用数百 MB 磁盘；脚本被中断的残留由启动清理兜底。
    """
    q = shlex.quote
    bad = str(bundle) + ".bad"
    old = str(bundle) + ".old"
    lines = [
        "#!/bin/sh",
        f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done",
        # 剔除 PyInstaller 内部环境变量（见 _sh_swap_script 同处注释）
        "unset _PYI_ARCHIVE_FILE _PYI_APPLICATION_HOME_DIR "
        "_PYI_PARENT_PROCESS_LEVEL _PYI_SPLASH_IPC _MEIPASS2",
        f"rm -rf {q(bad)}",
        f"mv {q(str(bundle))} {q(bad)} || exit 1",
        f"mv {q(old)} {q(str(bundle))} || exit 1",
        f"open {q(str(bundle))}",
        f"rm -rf {q(bad)}",
    ]
    return "\n".join(lines) + "\n"


def rollback_macos(bundle: Path, *, popen_fn=None, exit_fn=None) -> None:
    """macOS 回退：派发 detached sh 脚本把 .old 换回原位并重启；成功后 os._exit(0)。"""
    if popen_fn is None:
        def popen_fn(argv):
            return subprocess.Popen(argv, start_new_session=True, close_fds=True,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
    exit_fn = exit_fn or os._exit
    old = bundle.with_name(bundle.name + ".old")
    if not old.exists():
        raise UpdateError("没有可回退的上一版本")
    script = _sh_rollback_script(bundle)
    popen_fn(["/bin/sh", "-c", script])
    logger.warning("已派发回退脚本，等待主进程退出后完成回退并重启")
    exit_fn(0)


# ---------------------------------------------------------------------------
# 启动健康状态机（模块级：必须在建 Tk 之前运行）
# ---------------------------------------------------------------------------

def previous_install_path() -> Path | None:
    """可回退的上一版本（.old）路径；不存在/开发运行返回 None。"""
    if sys.platform == "win32":
        if not getattr(sys, "frozen", False):
            return None
        exe = Path(sys.executable)
        cand = exe.with_name(exe.name + ".old")
    elif sys.platform == "darwin":
        bundle = locate_bundle()
        if bundle is None:
            return None
        cand = bundle.with_name(bundle.name + ".old")
    else:
        return None
    return cand if cand.exists() else None


def load_state_file() -> dict | None:
    """读取 update-state.json（容忍 BOM）；不存在/损坏返回 None。"""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取更新状态失败: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def save_state_file(state: dict) -> None:
    """原子写入 update-state.json；失败只记日志。"""
    path = state_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("写入更新状态失败: %s", exc)


def state_after_rollback(state: dict | None) -> dict:
    """回退后的状态：版本互换、confirmed=true（目标版本此前稳定运行过）。"""
    state = state or {}
    return {
        "applied_version": state.get("previous_version") or "unknown",
        "previous_version": state.get("applied_version"),
        "applied_at": _now_iso(),
        "confirmed": True,   # 回退后的版本此前已在本机稳定运行过
        "crash_streak": 0,
        "launch_armed": False,
        "clean_exit": False,
    }


def note_clean_exit() -> None:
    """记录"本次启动已正常收尾"（用户关闭窗口/取消权限引导时调用）。

    正常退出即证明"非启动即崩"：下次启动的连崩计数会清零。
    非更新场景（无未确认状态）不写任何东西；写失败只记日志，绝不上抛。
    """
    try:
        state = load_state_file()
        if not state or state.get("confirmed") is not False:
            return
        state["clean_exit"] = True
        save_state_file(state)
    except Exception:  # noqa: BLE001 - 退出路径绝不允许抛出
        logger.exception("写入正常退出标记失败（已忽略）")


def perform_rollback(*, on_before_restart=None) -> None:
    """回退到上一版本：状态落定 → 收尾回调 → detached 替换并重启。成功后进程退出。

    状态必须先写：派发成功的路径随即 os._exit，没有第二次保存机会。
    派发失败且未发生实际换位（回退点仍在，或调用时就无回退点）时回写回退前
    状态，避免出现"状态说已回退、实际仍是新版"的不一致。
    """
    state_before = load_state_file()
    had_old = previous_install_path() is not None
    save_state_file(state_after_rollback(state_before))
    if on_before_restart is not None:
        try:
            on_before_restart()
        except Exception:  # noqa: BLE001 - 收尾失败不阻断回退
            logger.exception("回退前收尾失败（已忽略）")
    try:
        if sys.platform == "win32":
            if not getattr(sys, "frozen", False):
                raise UpdateError("当前为开发运行（非打包产物），不支持应用内回退")
            rollback_windows(Path(sys.executable))
        elif sys.platform == "darwin":
            bundle = locate_bundle()
            if bundle is None:
                raise UpdateError("未找到 .app 应用位置，无法回退")
            rollback_macos(bundle)
        else:
            raise UpdateError("当前系统不支持应用内回退，请手动下载")
    except Exception:
        # 未发生实际换位（回退点仍在，或调用时就无回退点）→ 回写回退前状态
        if not (had_old and previous_install_path() is None):
            if state_before is not None:
                save_state_file(state_before)
            else:
                with contextlib.suppress(OSError):
                    state_path().unlink(missing_ok=True)
        raise


def _cleanup_leftovers() -> None:
    """清理上次替换/回退遗留的临时文件（崩溃/强杀中断的残留）。

    - Windows：回退残留的 `<exe>.rollback-tmp`；
    - macOS：回退残留的 `<bundle>.bad` 破损包与 `.new-<pid>` 暂存目录
      （替换脚本自带清理，此处兜底脚本未跑完的情形）。
    """
    if not getattr(sys, "frozen", False):
        return
    if sys.platform == "win32":
        exe = Path(sys.executable)
        with contextlib.suppress(OSError):
            exe.with_name(exe.name + ".rollback-tmp").unlink(missing_ok=True)
    elif sys.platform == "darwin":
        bundle = locate_bundle()
        if bundle is None:
            return
        bad = bundle.with_name(bundle.name + ".bad")
        if bad.exists():
            shutil.rmtree(bad, ignore_errors=True)
        for stale in bundle.parent.glob(f".{bundle.name}.new-*"):
            shutil.rmtree(stale, ignore_errors=True)


def startup_health_gate() -> None:
    """启动早期（建 Tk 之前）推进更新健康状态机，再乐观武装本次启动。

    自动回退的判据是"更新后连续 2 次启动未正常收尾"。本函数必须建 Tk 之前
    运行——否则连 Tk 初始化都会崩溃的坏版本永远留不下崩溃证据，自动回退
    反而救不了它。流程：

    1. 清理上次替换/回退遗留的临时文件；
    2. 读上次启动结果：已武装且未正常收尾 → 连崩计数 +1；正常退出 → 清零；
    3. 连崩 ≥ ROLLBACK_AFTER_CRASHES 且有 .old → 自动回退（替换后本进程退出）；
    4. 把本次启动标记为"已武装、未正常收尾"；正常退出时由 note_clean_exit()
       覆写为 clean_exit=true。崩溃/被强杀则标记原样保留，下次启动计一次连崩。

    非更新场景（无状态文件或已确认健康）不写任何东西；
    任何异常只降级为日志，绝不阻断启动。
    """
    try:
        _cleanup_leftovers()
        state = load_state_file()
        if not state or state.get("confirmed") is not False:
            return
        streak = int(state.get("crash_streak") or 0)
        if state.get("launch_armed") and not state.get("clean_exit"):
            streak += 1
            logger.warning("上次启动未正常收尾（连崩计数 %d/%d）",
                           streak, ROLLBACK_AFTER_CRASHES)
        elif state.get("clean_exit"):
            streak = 0
        state["crash_streak"] = streak
        state["launch_armed"] = True
        state["clean_exit"] = False
        save_state_file(state)
        if streak < ROLLBACK_AFTER_CRASHES:
            return
        if previous_install_path() is None:
            logger.warning("更新后连续 %d 次启动未正常收尾，但未找到 .old，"
                           "继续运行当前版本", streak)
            return
        logger.error("更新后连续 %d 次启动未正常收尾，自动回退到上一版本", streak)
        try:
            perform_rollback()
        except Exception:  # noqa: BLE001 - 回退失败不能阻止启动
            logger.exception("自动回退失败，继续以当前版本运行")
    except Exception:  # noqa: BLE001 - 状态机绝不允许阻断启动
        logger.exception("启动健康检查失败（已忽略，继续启动）")


# ---------------------------------------------------------------------------
# 编排：检查节奏、下载、应用、回退、健康确认
# ---------------------------------------------------------------------------

class AgentUpdater:
    """更新流程编排。GUI 侧注入回调，保证本类可脱离 tkinter 测试。

    - emit(event)：把更新事件放进 GUI 的 event_queue；
    - safety_ok()：返回当前是否处于演出安全窗口；
    - server_url_fn()：返回 GUI 当前线路的服务器地址（推导清单 URL）；
    - on_before_restart()：替换/回退前的收尾（断开连接、冲刷日志）。
    """

    def __init__(self, *, emit, safety_ok, server_url_fn, on_before_restart=None):
        self._emit_fn = emit
        self._safety_ok = safety_ok
        self._server_url_fn = server_url_fn
        self._on_before_restart = on_before_restart
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._check_lock = threading.Lock()
        # 替换/回退互斥门闩：从用户确认到进程退出的窗口可达数分钟
        # （selftest 最长 90s + 下载/确认等），同一时刻只允许一个替换类任务
        self._action_lock = threading.Lock()
        self._ready: dict | None = None        # 已下载待安装的更新包信息

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """拉起定时检查线程（启动健康状态机见模块级 startup_health_gate，
        已由 gui.main() 在建 Tk 之前先行执行）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="update-check")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(FIRST_CHECK_DELAY_S):
            return
        while not self._stop.is_set():
            self.check_and_download()
            if self._stop.wait(CHECK_INTERVAL_S):
                return

    # ---------------- 对外动作 ----------------

    def safety_ok(self) -> bool:
        try:
            return bool(self._safety_ok())
        except Exception:  # noqa: BLE001 - 判定失败按不安全处理
            logger.exception("安全窗口判定失败，按不安全处理")
            return False

    def request_manual_check(self) -> None:
        """手动检查（GUI 按钮）：独立线程执行，失败事件带 manual=True 供 GUI 弹提示。"""
        threading.Thread(target=self.check_and_download, kwargs={"manual": True},
                         daemon=True, name="update-check-manual").start()

    def offer_server_manifest(self, manifest: dict) -> None:
        """服务端推送的 update.available：与轮询路径汇入同一处理。"""
        threading.Thread(target=self.check_and_download,
                         kwargs={"manifest": manifest, "from_server": True},
                         daemon=True, name="update-offer").start()

    def apply(self) -> bool:
        """应用已下载的更新（GUI 确认后调用）：独立线程执行（selftest 可长达 90s）。

        门闩保护：从确认到进程退出的窗口可达数分钟，重复提交会产生并发替换
        线程（互相破坏 .old 回退点、留下半替换状态），一律拒绝。
        返回是否已接受提交。
        """
        if not self._action_lock.acquire(blocking=False):
            self._emit({"event": "update_none", "manual": True,
                        "message": "另有一个更新/回退任务正在进行中，请稍候"})
            return False
        ready = self._ready
        if not ready:
            self._action_lock.release()
            self._emit({"event": "update_failed", "manual": True,
                        "message": "没有已下载的更新包，请先检查更新"})
            return False
        self._ready = None   # 消费待安装信息：提交后的重复点击不再进入流程
        threading.Thread(target=self._apply_worker, args=(dict(ready),),
                         daemon=True, name="update-apply").start()
        return True

    def request_rollback(self) -> bool:
        """手动回退上一版本（GUI 确认后调用）；被门闩拒绝时返回 False。"""
        if not self._action_lock.acquire(blocking=False):
            self._emit({"event": "update_none", "manual": True,
                        "message": "另有一个更新/回退任务正在进行中，请稍候"})
            return False
        threading.Thread(target=self._rollback_worker, daemon=True,
                         name="update-rollback").start()
        return True

    def previous_install(self) -> Path | None:
        """可回退的上一版本（.old）路径；不存在/开发运行返回 None。"""
        return previous_install_path()

    def mark_healthy(self) -> None:
        """健康运行确认（GUI 在 mainloop 存活 HEALTH_CONFIRM_S 后调用）。

        判据是"主循环存活"（非启动即崩的充分证据）；按键自检结果可能受环境
        因素影响（UIPI 拦截、macOS 未授权等），由 GUI 只记日志、不参与判定。
        .old 不在此删除：保留到下一次更新替换时让位，供用户随时手动回退。
        """
        state = self._load_state()
        if state and state.get("confirmed") is False:
            state["confirmed"] = True
            self._save_state(state)
            logger.info("更新健康确认: v%s 已稳定运行 %ds",
                        state.get("applied_version"), HEALTH_CONFIRM_S)

    # ---------------- 检查与下载 ----------------

    def check_and_download(self, *, manual: bool = False,
                           manifest: dict | None = None,
                           from_server: bool = False) -> None:
        """检查清单并（在安全窗口内）自动下载新版本。不抛异常。"""
        if not self._check_lock.acquire(blocking=False):
            if manual:
                self._emit({"event": "update_none", "manual": True,
                            "message": "正在检查更新，请稍候再试"})
            return
        try:
            self._check_and_download_inner(manual=manual, manifest=manifest,
                                           from_server=from_server)
        except UpdateError as exc:
            logger.warning("更新检查失败: %s", exc)
            self._emit({"event": "update_failed", "manual": manual,
                        "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 更新通道任何异常只降级为提示
            logger.exception("更新检查异常")
            self._emit({"event": "update_failed", "manual": manual,
                        "message": f"更新检查异常: {exc}"})
        finally:
            self._check_lock.release()

    def _check_and_download_inner(self, *, manual: bool,
                                  manifest: dict | None,
                                  from_server: bool) -> None:
        current = ver_tuple(APP_VERSION)
        if current is None:
            # 开发版（0.0.0.dev 等）永不参与自动更新
            logger.info("当前为开发版本 v%s，跳过更新检查", APP_VERSION)
            if manual:
                self._emit({"event": "update_none", "manual": True,
                            "message": f"当前为开发版本 v{APP_VERSION}，已跳过自动更新检查"})
            return

        if manifest is None:
            url = manifest_url_for(self._server_url_fn())
            if not url:
                raise UpdateError("尚未选择服务器线路，无法检查更新")
            manifest = fetch_manifest(url)
        manifest = validate_manifest(manifest)

        latest = ver_tuple(manifest.get("version"))
        if latest is None:
            logger.info("清单版本 %r 非数字版本，跳过", manifest.get("version"))
            if manual:
                self._emit({"event": "update_none", "manual": True,
                            "message": "清单版本号为开发版本，已跳过"})
            return
        if latest <= current:
            logger.info("当前已是最新版本 v%s（清单 v%s）",
                        APP_VERSION, manifest.get("version"))
            if manual:
                self._emit({"event": "update_none", "manual": True,
                            "message": f"当前已是最新版本 v{APP_VERSION}"})
            return

        version = manifest["version"]
        notes = manifest.get("notes") or ""
        logger.info("发现新版本 v%s%s（来源: %s）", version,
                    f"：{notes}" if notes else "", "服务端推送" if from_server else "轮询")

        key = platform_key()
        if key is None or select_asset(manifest, key) is None:
            self._emit({"event": "update_failed", "manual": manual, "version": version,
                        "message": f"发现新版本 v{version}，但当前平台没有自动更新包，"
                                   "请手动下载安装"})
            return

        url = manifest_url_for(self._server_url_fn())
        asset = select_asset(manifest, key)

        # 幂等：已下载且校验通过的文件直接提示"点击更新"（重装/重启后不重复下载）
        existing = self._have_valid_asset(asset)
        if existing is not None:
            self._ready = self._ready_info(version, notes, asset, str(existing))
            self._emit({"event": "update_available", "version": version, "notes": notes,
                        "downloaded": True, **self._ready})
            return

        self._emit({"event": "update_available", "version": version, "notes": notes,
                    "downloaded": False, "waiting": not self.safety_ok()})

        # 安全窗口：演出中只记录、不下载（避免占带宽影响时钟同步），窗口打开后自动继续
        if not self.safety_ok():
            logger.info("发现新版本 v%s，等待演出空闲后下载", version)
            while not self._stop.is_set() and not self.safety_ok():
                time.sleep(WINDOW_POLL_S)
            if self._stop.is_set():
                return

        result = download_verified(
            url, key, updates_dir(),
            on_progress=lambda pct, v=version: self._emit(
                {"event": "update_progress", "version": v, "percent": pct}))
        self._ready = self._ready_info(result["version"], result.get("notes") or "",
                                       result.get("asset") or {}, str(result["path"]))
        self._emit({"event": "update_downloaded", "size": result.get("size"),
                    **self._ready})

    def _ready_info(self, version: str, notes: str, asset: dict,
                    path: str) -> dict:
        return {
            "version": version,
            "notes": notes,
            "path": path,
            "codesign_authority": asset.get("codesign_authority"),
        }

    def _have_valid_asset(self, asset: dict) -> Path | None:
        """检查 updates 目录中是否已有与清单一致的完整文件（大小 + sha256）。"""
        dest = updates_dir() / asset["asset"]
        try:
            if not dest.is_file() or dest.stat().st_size != asset["size"]:
                return None
            if hash_file(dest) != asset["sha256"]:
                return None
        except OSError:
            return None
        return dest

    # ---------------- 应用（替换并重启） ----------------

    def _apply_worker(self, ready: dict) -> None:
        package = Path(ready.get("path", ""))
        version = str(ready.get("version") or "?")
        try:
            if not self.safety_ok():
                raise UpdateError("当前演出进行中（已连接且就绪或有待触发指令），已取消更新")
            if not package.is_file():
                raise UpdateError("更新包文件不存在，请重新检查更新以下载")
            self._emit({"event": "update_applying", "version": version})
            logger.info("开始应用更新 v%s（%s）", version, package)
            if sys.platform == "win32":
                self._apply_windows(package, version)
            elif sys.platform == "darwin":
                self._apply_macos(package, version, ready.get("codesign_authority"))
            else:
                raise UpdateError("当前系统不支持应用内更新，请手动下载")
        except UpdateError as exc:
            logger.warning("更新应用失败: %s", exc)
            self._ready = ready   # 恢复待安装信息：修复环境后可直接重试
            self._emit({"event": "update_failed", "manual": True,
                        "message": str(exc), "ready": dict(ready)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("更新应用异常")
            self._ready = ready
            self._emit({"event": "update_failed", "manual": True,
                        "message": f"更新失败：{exc}", "ready": dict(ready)})
        finally:
            # 成功路径在 replace_* 内 os._exit，不会走到这里
            self._action_lock.release()

    def _apply_windows(self, package: Path, version: str) -> None:
        if not getattr(sys, "frozen", False):
            raise UpdateError("当前为开发运行（非打包产物），不支持应用内更新")
        ok, message = run_selftest(package, SELFTEST_TIMEOUT_WIN_S)
        if not ok:
            raise UpdateError(f"更新包预检未通过：{message}；已保留现有版本")
        self._prepare_restart(version)
        try:
            replace_windows(Path(sys.executable), package)
        except Exception as exc:
            self._discard_pending_state(version)
            raise UpdateError(f"替换程序文件失败（已复原）：{exc}") from exc

    def _apply_macos(self, package: Path, version: str,
                     manifest_authority: str | None) -> None:
        bundle = locate_bundle()
        if bundle is None:
            raise UpdateError("未找到 .app 应用位置，请手动下载安装新版本")
        if not os.access(bundle.parent, os.W_OK):
            # 不弹管理员密码框（避免演出中触发权限交互），降级为手动安装
            raise UpdateError("应用所在目录不可写，请手动下载安装新版本")
        staging_root = bundle.with_name(f".{bundle.name}.new-{os.getpid()}")
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)
        try:
            staged_app = extract_update(package, staging_root)
            check_codesign_compat(bundle, staged_app, manifest_authority)
            verify_codesign(staged_app)
            staged_bin = staged_app / "Contents" / "MacOS" / Path(sys.executable).name
            if not staged_bin.is_file():
                raise UpdateError("更新包内未找到可执行文件")
            ok, message = run_selftest(staged_bin, SELFTEST_TIMEOUT_MAC_S)
            if not ok:
                raise UpdateError(f"更新包预检未通过：{message}；已保留现有版本")
            self._prepare_restart(version)
            try:
                replace_macos(bundle, staged_app)
            except Exception as exc:
                self._discard_pending_state(version)
                raise UpdateError(f"替换应用失败：{exc}") from exc
        finally:
            # 成功路径进程已由 replace_macos 退出（暂存由替换脚本清理）；
            # 失败路径在此清掉暂存目录
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)

    def _prepare_restart(self, version: str) -> None:
        """写 update-state.json（未确认 + 启动武装占位）并执行重启前收尾。"""
        self._save_state({
            "applied_version": version,
            "previous_version": APP_VERSION,
            "applied_at": _now_iso(),
            "confirmed": False,
            "crash_streak": 0,
            "launch_armed": False,   # 由下次启动的 startup_health_gate 武装
            "clean_exit": False,
        })
        if self._on_before_restart is not None:
            try:
                self._on_before_restart()
            except Exception:  # noqa: BLE001 - 收尾失败不阻断更新
                logger.exception("重启前收尾失败（已忽略）")

    def _discard_pending_state(self, version: str) -> None:
        """替换未生效（已复原）时撤销未确认状态，避免误触发自动回退。"""
        state = self._load_state()
        if state and state.get("confirmed") is False \
                and state.get("applied_version") == version:
            with contextlib.suppress(OSError):
                state_path().unlink(missing_ok=True)

    # ---------------- 回退与健康确认 ----------------

    def _rollback_worker(self) -> None:
        try:
            if not self.safety_ok():
                raise UpdateError("当前演出进行中（已连接且就绪或有待触发指令），已取消回退")
            if self.previous_install() is None:
                raise UpdateError("没有可回退的上一版本")
            state = self._load_state() or {}
            target = state.get("previous_version") or "上一版本"
            self._emit({"event": "update_applying", "version": target, "rollback": True})
            logger.warning("用户触发回退到上一版本")
            self._do_rollback()
        except UpdateError as exc:
            logger.warning("回退失败: %s", exc)
            self._emit({"event": "update_failed", "manual": True, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("回退异常")
            self._emit({"event": "update_failed", "manual": True,
                        "message": f"回退失败：{exc}"})
        finally:
            # 成功路径在 rollback_* 内 os._exit，不会走到这里
            self._action_lock.release()

    def _do_rollback(self) -> None:
        """执行回退：收尾回调 + perform_rollback 的状态落定与 detached 替换重启。

        成功后进程退出；失败时 perform_rollback 已在文件系统未变更的前提下
        回写回退前状态，调用方按普通失败处理。
        """
        perform_rollback(on_before_restart=self._on_before_restart)

    # ---------------- 状态文件 ----------------

    def _load_state(self) -> dict | None:
        return load_state_file()

    def _save_state(self, state: dict) -> None:
        save_state_file(state)

    # ---------------- 事件 ----------------

    def _emit(self, event: dict) -> None:
        try:
            self._emit_fn(event)
        except Exception:  # noqa: BLE001 - 事件投递失败绝不影响主链路
            logger.exception("更新事件投递失败")
