"""自动更新聚焦检查：版本解析、清单校验、下载双取、安全窗口、替换/回退干跑。

覆盖 docs/auto-update.md 与被控端 updater.py 的关键约定：

1. 版本解析与比较：含非数字段（dev 版）一律视为不参与更新；
2. 清单校验：schema/version/assets 缺一不可，资产名不得含路径分隔符；
3. 下载 sha256 双取：下载前后两份清单的 sha256/版本不一致必须重下，
   连续 max_rounds 轮失败后放弃，且不留下任何残留文件（不触碰现有安装）；
4. 安全窗口：未连接 / 未就绪且无待触发指令才是安全窗口；
5. Windows 替换/回退的干跑：真临时文件 + 注入假 popen/exit，验证改名顺序、
   失败复原、selftest 未通过时不替换；
6. macOS 替换/回退：sh 脚本内容（替换顺序、环境清理、残留清理）、tar.gz
   解包校验、签名身份比对、_apply_macos 三条关键路径的干跑；
7. 启动健康状态机：武装标记、正常退出清零、连崩 ROLLBACK_AFTER_CRASHES 次
   触发自动回退、无 .old 时只告警；
8. 替换/回退门闩：apply() 重复提交被拒、无包被拒、失败后待安装信息恢复。

全部用例不连网、不启动真实进程、不注入按键；仅用标准库与临时目录。

在仓库根目录运行：
    python -m unittest discover -s agent/tests -t agent -v
"""

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent import updater
from agent.updater import (AgentUpdater, UpdateError, _sh_rollback_script,
                           _sh_swap_script, asset_url, check_codesign_compat,
                           cleaned_child_env, download_verified, extract_update,
                           http_base, manifest_url_for, note_clean_exit,
                           parse_authority, platform_key, rollback_windows,
                           run_selftest, safety_window, select_asset,
                           startup_health_gate, state_after_rollback,
                           validate_manifest, ver_tuple)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_manifest(version: str, asset_name: str, payload: bytes,
                  key: str = "windows-x64", **asset_extra) -> dict:
    """构造一份合法清单（sha256/size 与 payload 一致）。"""
    return {
        "schema": 1,
        "version": version,
        "notes": f"测试版本 {version}",
        "assets": {
            key: {"asset": asset_name, "kind": "exe",
                  "sha256": sha256_of(payload), "size": len(payload),
                  **asset_extra},
        },
    }


class FakeProc:
    """子进程替身：只返回预设退出码或抛预设异常。"""

    def __init__(self, code: int = 0, exc: Exception | None = None):
        self.code = code
        self.exc = exc
        self.killed = False

    def wait(self, timeout=None):
        if self.exc is not None:
            raise self.exc
        return self.code

    def kill(self):
        self.killed = True


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

class HttpBaseTest(unittest.TestCase):

    def test_wss_maps_to_https(self):
        self.assertEqual(http_base("wss://sb.example.com:4680"),
                         "https://sb.example.com:4680")

    def test_ws_maps_to_http(self):
        self.assertEqual(http_base("ws://127.0.0.1:8000"),
                         "http://127.0.0.1:8000")

    def test_http_passthrough_and_trailing_slash(self):
        self.assertEqual(http_base("https://a.example.com/app/"),
                         "https://a.example.com/app")

    def test_bare_host_defaults_to_https(self):
        self.assertEqual(http_base("a.example.com"), "https://a.example.com")

    def test_strips_ws_suffix(self):
        self.assertEqual(http_base("wss://a.example.com/ws"),
                         "https://a.example.com")

    def test_empty_returns_empty(self):
        self.assertEqual(http_base("  "), "")

    def test_manifest_url(self):
        self.assertEqual(manifest_url_for("wss://a.example.com:4680"),
                         "https://a.example.com:4680/update/manifest.json")
        self.assertEqual(manifest_url_for(""), "")


class VersionTupleTest(unittest.TestCase):

    def test_numeric(self):
        self.assertEqual(ver_tuple("1.0.3"), (1, 0, 3))
        self.assertEqual(ver_tuple("1.2"), (1, 2))

    def test_ordering(self):
        self.assertGreater(ver_tuple("1.0.10"), ver_tuple("1.0.9"))
        self.assertLess(ver_tuple("1.0.3"), ver_tuple("1.1.0"))

    def test_dev_and_invalid_return_none(self):
        for bad in ("0.0.0.dev", "1.0.3-beta", "v1.0.3", "", None, "1.2.x"):
            with self.subTest(value=bad):
                self.assertIsNone(ver_tuple(bad))


class PlatformKeyTest(unittest.TestCase):

    def test_known_combinations(self):
        self.assertEqual(platform_key("win32", "AMD64"), "windows-x64")
        self.assertEqual(platform_key("darwin", "arm64"), "macos-arm64")
        self.assertEqual(platform_key("darwin", "x86_64"), "macos-x86_64")

    def test_unsupported_os_returns_none(self):
        self.assertIsNone(platform_key("linux", "x86_64"))


class ManifestValidationTest(unittest.TestCase):

    def _valid(self) -> dict:
        return make_manifest("1.1.0", "ScriptCueAgent-windows-x64.exe", b"x" * 16)

    def test_valid_passes(self):
        m = self._valid()
        self.assertIs(validate_manifest(m), m)

    def test_wrong_schema(self):
        m = self._valid()
        m["schema"] = 2
        with self.assertRaises(UpdateError):
            validate_manifest(m)

    def test_missing_version(self):
        m = self._valid()
        m["version"] = ""
        with self.assertRaises(UpdateError):
            validate_manifest(m)

    def test_empty_assets(self):
        m = self._valid()
        m["assets"] = {}
        with self.assertRaises(UpdateError):
            validate_manifest(m)

    def test_bad_sha256(self):
        m = self._valid()
        m["assets"]["windows-x64"]["sha256"] = "zz"
        with self.assertRaises(UpdateError):
            validate_manifest(m)

    def test_asset_name_with_separator_rejected(self):
        for bad in ("../evil.exe", "a/b.exe", "a\\b.exe"):
            m = self._valid()
            m["assets"]["windows-x64"]["asset"] = bad
            with self.subTest(name=bad):
                with self.assertRaises(UpdateError):
                    validate_manifest(m)

    def test_select_asset_unknown_key(self):
        m = self._valid()
        self.assertIsNone(select_asset(m, "macos-arm64"))
        self.assertIsNotNone(select_asset(m, "windows-x64"))


class SafetyWindowTest(unittest.TestCase):

    class FakeEngine:
        def __init__(self, connected=False, ready=False, pending=None):
            self.connected = connected
            self.ready = ready
            self._pending_fires = pending or {}

    def test_none_engine_is_safe(self):
        self.assertTrue(safety_window(None, {}))

    def test_disconnected_is_safe_even_ready(self):
        engine = self.FakeEngine(connected=False, ready=True)
        self.assertTrue(safety_window(engine, {}))

    def test_ready_blocks(self):
        engine = self.FakeEngine(connected=True, ready=True)
        self.assertFalse(safety_window(engine, {}))

    def test_engine_pending_blocks(self):
        engine = self.FakeEngine(connected=True, pending={"cmd": "evt"})
        self.assertFalse(safety_window(engine, {}))

    def test_gui_pending_blocks(self):
        engine = self.FakeEngine(connected=True)
        self.assertFalse(safety_window(engine, {"cmd": {"command": "play"}}))

    def test_idle_connected_is_safe(self):
        engine = self.FakeEngine(connected=True, ready=False)
        self.assertTrue(safety_window(engine, {}))


class CleanedChildEnvTest(unittest.TestCase):
    """重启环境必须剔除 PyInstaller 内部变量。

    背景（Windows 真机 e2e 实捉）：旧进程重启新 exe 时若继承了 _PYI_*，新
    bootloader 会误入 onefile 子进程模式校验父进程可执行文件（此时父进程的
    exe 已被改名为 .old），弹 Security validation failure 拒绝启动。
    """

    def test_strips_pyinstaller_vars(self):
        fake = {"PATH": "x", "APPDATA": "d",
                "_PYI_ARCHIVE_FILE": "a", "_PYI_PARENT_PROCESS_LEVEL": "1",
                "_PYI_APPLICATION_HOME_DIR": "c", "_PYI_SPLASH_IPC": "0",
                "_MEIPASS2": "b"}
        with mock.patch.dict("os.environ", fake, clear=True):
            env = cleaned_child_env()
        self.assertEqual(env, {"PATH": "x", "APPDATA": "d"})


class ParseAuthorityTest(unittest.TestCase):

    def test_parses_authority(self):
        out = "Executable=/x\nIdentifier=com.x\nAuthority=ScriptCue Codesign\n"
        self.assertEqual(parse_authority(out), "ScriptCue Codesign")

    def test_adhoc_has_no_authority(self):
        self.assertIsNone(parse_authority("Signature=adhoc\nTeamIdentifier=not set\n"))
        self.assertIsNone(parse_authority(""))


# ---------------------------------------------------------------------------
# 下载校验（sha256 双取）
# ---------------------------------------------------------------------------

class DownloadVerifiedTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sc-upd-test-")
        self.addCleanup(self._tmp.cleanup)
        self.dest_dir = Path(self._tmp.name) / "updates"
        self.payload = b"new-version-binary" * 64

    def _fakes(self, manifest_seq: list[dict]):
        """返回 (fetch_fn, stream_fn, calls)；manifest_seq 按调用顺序弹出。"""
        calls = {"fetch": 0, "stream": []}
        seq = list(manifest_seq)

        def fetch_fn(_url):
            calls["fetch"] += 1
            return seq[min(calls["fetch"] - 1, len(seq) - 1)]

        def stream_fn(url, dest, size, on_progress):
            calls["stream"].append(url)
            Path(dest).write_bytes(self.payload)

        return fetch_fn, stream_fn, calls

    def test_success_single_round(self):
        manifest = make_manifest("1.1.0", "new.exe", self.payload)
        fetch_fn, stream_fn, calls = self._fakes([manifest])
        result = download_verified("https://m.example.com/update/manifest.json",
                                   "windows-x64", self.dest_dir,
                                   fetch_fn=fetch_fn, stream_fn=stream_fn)
        self.assertEqual(result["version"], "1.1.0")
        self.assertEqual(result["path"].read_bytes(), self.payload)
        self.assertEqual(calls["fetch"], 2, "必须取两次清单（下载前后各一次）")
        self.assertEqual(len(list(self.dest_dir.glob("*.part"))), 0,
                         "成功后不应残留 .part 临时文件")

    def test_sha_mismatch_then_retry(self):
        bad_payload = b"corrupted"
        bad = make_manifest("1.1.0", "new.exe", bad_payload)
        good = make_manifest("1.1.0", "new.exe", self.payload)
        # 第一轮：清单 A 的 sha 与真实下载内容不符 → 重下（sha 失败轮不会再取清单 B）；
        # 第二轮：清单变为 good → 成功
        fetch_fn, stream_fn, calls = self._fakes([bad, good, good])
        result = download_verified("https://m.example.com/update/manifest.json",
                                   "windows-x64", self.dest_dir,
                                   fetch_fn=fetch_fn, stream_fn=stream_fn)
        self.assertEqual(result["path"].read_bytes(), self.payload)
        self.assertEqual(len(calls["stream"]), 2, "第一轮校验失败应触发第二轮下载")

    def test_publish_boundary_triggers_redownload(self):
        """下载期间发布方换了新清单（A/B sha 不一致）→ 重下。"""
        v1 = make_manifest("1.1.0", "new.exe", self.payload)
        v2 = make_manifest("1.1.1", "new.exe", self.payload)  # version 不同
        fetch_fn, stream_fn, calls = self._fakes([v1, v2, v2, v2])
        result = download_verified("https://m.example.com/update/manifest.json",
                                   "windows-x64", self.dest_dir,
                                   fetch_fn=fetch_fn, stream_fn=stream_fn)
        self.assertEqual(result["version"], "1.1.1")
        self.assertEqual(len(calls["stream"]), 2)

    def test_all_rounds_fail_keeps_no_residue(self):
        bad = make_manifest("1.1.0", "new.exe", b"other")
        fetch_fn, stream_fn, _calls = self._fakes([bad])
        with self.assertRaises(UpdateError):
            download_verified("https://m.example.com/update/manifest.json",
                              "windows-x64", self.dest_dir, max_rounds=3,
                              fetch_fn=fetch_fn, stream_fn=stream_fn)
        self.assertEqual(list(self.dest_dir.glob("*")), [],
                         "失败后不得残留文件（更不允许触碰现有安装）")

    def test_asset_url_injection_preferred(self):
        manifest = make_manifest("1.1.0", "new.exe", self.payload)
        manifest["assets"]["windows-x64"]["url"] = "https://cdn.example.com/x/new.exe"
        fetch_fn, stream_fn, calls = self._fakes([manifest])
        download_verified("https://m.example.com/update/manifest.json",
                          "windows-x64", self.dest_dir,
                          fetch_fn=fetch_fn, stream_fn=stream_fn)
        self.assertEqual(calls["stream"][0], "https://cdn.example.com/x/new.exe")

    def test_asset_url_fallback_uses_manifest_dir(self):
        manifest = make_manifest("1.1.0", "new.exe", self.payload)
        self.assertEqual(
            asset_url(manifest, manifest["assets"]["windows-x64"],
                      "https://m.example.com/app/update-manifest.json"),
            "https://m.example.com/app/new.exe")


# ---------------------------------------------------------------------------
# selftest 与 Windows 替换/回退干跑
# ---------------------------------------------------------------------------

class RunSelftestTest(unittest.TestCase):

    def test_pass(self):
        ok, message = run_selftest(Path("x.exe"), 10,
                                   popen_fn=lambda argv: FakeProc(0))
        self.assertTrue(ok)
        self.assertEqual(message, "预检通过")

    def test_nonzero_fails(self):
        ok, _message = run_selftest(Path("x.exe"), 10,
                                    popen_fn=lambda argv: FakeProc(1))
        self.assertFalse(ok)

    def test_timeout_fails_and_kills(self):
        proc = FakeProc(exc=subprocess.TimeoutExpired("x.exe", 10))
        ok, message = run_selftest(Path("x.exe"), 10, popen_fn=lambda argv: proc)
        self.assertFalse(ok)
        self.assertTrue(proc.killed)
        self.assertIn("超时", message)

    def test_spawn_error_fails(self):
        def bad_popen(argv):
            raise OSError("no such file")
        ok, _message = run_selftest(Path("x.exe"), 10, popen_fn=bad_popen)
        self.assertFalse(ok)


class ReplaceWindowsTest(unittest.TestCase):
    """Windows 替换干跑：真实临时文件 + 假 popen/exit，不启动任何进程。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sc-replace-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = self.root / "ScriptCueAgent.exe"
        self.exe.write_bytes(b"old-version")
        self.new_exe = self.root / "ScriptCueAgent-windows-x64.exe"
        self.new_exe.write_bytes(b"new-version")
        self.popen_calls: list[list[str]] = []
        self.exit_codes: list[int] = []

    def _popen(self, argv):
        self.popen_calls.append(list(argv))

    def _exit(self, code):
        self.exit_codes.append(code)

    def test_success_swaps_and_restarts(self):
        updater.replace_windows(self.exe, self.new_exe,
                                popen_fn=self._popen, exit_fn=self._exit)
        self.assertEqual(self.exe.read_bytes(), b"new-version")
        self.assertEqual((self.root / "ScriptCueAgent.exe.old").read_bytes(),
                         b"old-version", "旧版应保留为 .old 供回退")
        self.assertEqual(self.popen_calls, [[str(self.exe)]], "应 detached 重启新 exe")
        self.assertEqual(self.exit_codes, [0])

    def test_failure_restores_original(self):
        missing = self.root / "not-there.exe"
        with self.assertRaises(Exception):
            updater.replace_windows(self.exe, missing,
                                    popen_fn=self._popen, exit_fn=self._exit)
        self.assertEqual(self.exe.read_bytes(), b"old-version", "失败必须复原现有安装")
        self.assertEqual(self.popen_calls, [], "失败不得重启")
        self.assertEqual(self.exit_codes, [])

    def test_stale_old_is_replaced_by_current(self):
        stale = self.root / "ScriptCueAgent.exe.old"
        stale.write_bytes(b"ancient")
        updater.replace_windows(self.exe, self.new_exe,
                                popen_fn=self._popen, exit_fn=self._exit)
        self.assertEqual(stale.read_bytes(), b"old-version",
                         ".old 只保留最近一次（旧的回退点让位）")


class RollbackWindowsTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sc-rollback-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.exe = self.root / "ScriptCueAgent.exe"
        self.exe.write_bytes(b"new-broken")
        self.popen_calls: list[list[str]] = []
        self.exit_codes: list[int] = []

    def _popen(self, argv):
        self.popen_calls.append(list(argv))

    def _exit(self, code):
        self.exit_codes.append(code)

    def test_restores_old_and_restarts(self):
        (self.root / "ScriptCueAgent.exe.old").write_bytes(b"old-good")
        rollback_windows(self.exe, popen_fn=self._popen, exit_fn=self._exit)
        self.assertEqual(self.exe.read_bytes(), b"old-good")
        self.assertEqual((self.root / "ScriptCueAgent.exe.rollback-tmp").read_bytes(),
                         b"new-broken")
        self.assertEqual(self.popen_calls, [[str(self.exe)]])
        self.assertEqual(self.exit_codes, [0])

    def test_missing_old_raises_without_touching_install(self):
        with self.assertRaises(UpdateError):
            rollback_windows(self.exe, popen_fn=self._popen, exit_fn=self._exit)
        self.assertEqual(self.exe.read_bytes(), b"new-broken")
        self.assertEqual(self.popen_calls, [])


# ---------------------------------------------------------------------------
# macOS：sh 脚本内容、解包校验、签名比对、_apply_macos 干跑
# ---------------------------------------------------------------------------

class SwapScriptTest(unittest.TestCase):
    """macOS 替换脚本：环境清理、替换顺序、失败分支的残留清理。"""

    def setUp(self):
        self.bundle = "/Applications/ScriptCue.app"
        self.staging_root = "/Applications/.ScriptCue.app.new-77"
        self.script = _sh_swap_script(Path(self.bundle),
                                      Path(self.staging_root) / "ScriptCue.app",
                                      Path(self.staging_root))

    def test_unset_pyinstaller_env(self):
        self.assertIn("unset _PYI_ARCHIVE_FILE _PYI_APPLICATION_HOME_DIR "
                      "_PYI_PARENT_PROCESS_LEVEL _PYI_SPLASH_IPC _MEIPASS2",
                      self.script)

    def test_sequence_and_failure_cleanup(self):
        # 断言用与生产者相同的表达式（shlex.quote + str(Path)），
        # 保证在 Windows/ macOS 两种宿主上都能对上真实脚本
        q = shlex.quote
        bundle = Path(self.bundle)
        staging_root = Path(self.staging_root)
        old = str(bundle) + ".old"
        lines = self.script.splitlines()
        rename_old = next(i for i, l in enumerate(lines)
                          if l.startswith(f"mv {q(str(bundle))} {q(old)}"))
        promote = next(i for i, l in enumerate(lines)
                       if l.startswith("if mv ") and ".new-77" in l)
        self.assertLess(rename_old, promote, "先改名旧版再让新板上位")
        else_idx = next(i for i, l in enumerate(lines) if l.strip() == "else")
        fi_idx = next(i for i, l in enumerate(lines) if l.strip() == "fi")
        failure = "\n".join(lines[else_idx:fi_idx])
        self.assertIn(f"rm -rf {q(str(staging_root))}", failure,
                      "失败分支必须清理 .new-* 暂存目录")
        self.assertIn(f"mv {q(old)} {q(str(bundle))}", failure,
                      "失败分支必须把 .old 换回并重启旧版")


class RollbackScriptTest(unittest.TestCase):
    """macOS 回退脚本：.old 上位顺序与 .bad 残留清理。"""

    def setUp(self):
        self.bundle = "/Applications/ScriptCue.app"
        self.script = _sh_rollback_script(Path(self.bundle))

    def test_unset_pyinstaller_env(self):
        self.assertIn("unset _PYI_ARCHIVE_FILE", self.script)

    def test_sequence_and_bad_cleanup(self):
        # 断言用与生产者相同的表达式（见 SwapScriptTest 说明）
        q = shlex.quote
        bundle = Path(self.bundle)
        bad = str(bundle) + ".bad"
        old = str(bundle) + ".old"
        lines = self.script.splitlines()
        mv_bad = next(i for i, l in enumerate(lines)
                      if l.startswith(f"mv {q(str(bundle))} {q(bad)}"))
        mv_old = next(i for i, l in enumerate(lines)
                      if l.startswith(f"mv {q(old)} {q(str(bundle))} "))
        open_idx = next(i for i, l in enumerate(lines)
                        if l.startswith(f"open {q(str(bundle))}"))
        self.assertLess(mv_bad, mv_old, "先把当前版挪走再让 .old 上位")
        self.assertLess(mv_old, open_idx)
        self.assertTrue(any(l.startswith(f"rm -rf {q(bad)}")
                            for l in lines[open_idx + 1:]),
                        "open 之后应清理 .bad 破损包")


class ExtractUpdateTest(unittest.TestCase):
    """tar.gz 解包：顶层必须唯一 .app，否则拒绝。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sc-extract-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _make_tar(self, names: list[str]) -> Path:
        src = self.root / "src"
        tar_path = self.root / "update.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            for name in names:
                app = src / name / "Contents"
                app.mkdir(parents=True)
                (app / "Info.plist").write_text("x", encoding="utf-8")
                tf.add(src / name, arcname=name)
        return tar_path

    def test_single_app_extracted(self):
        tar_path = self._make_tar(["ScriptCue.app"])
        app = extract_update(tar_path, self.root / "staging")
        self.assertEqual(app.name, "ScriptCue.app")
        self.assertTrue((app / "Contents" / "Info.plist").is_file())

    def test_two_apps_rejected(self):
        tar_path = self._make_tar(["A.app", "B.app"])
        with self.assertRaises(UpdateError):
            extract_update(tar_path, self.root / "staging2")

    def test_no_app_rejected(self):
        tar_path = self._make_tar(["not-an-app"])
        with self.assertRaises(UpdateError):
            extract_update(tar_path, self.root / "staging3")


class CodesignCompatTest(unittest.TestCase):
    """签名身份一致性：已知三方身份必须一致；ad-hoc（None）跳过比对。"""

    def _check(self, current, staged, manifest=None):
        with mock.patch.object(updater, "codesign_authority",
                               side_effect=[current, staged]):
            check_codesign_compat(Path("/cur.app"), Path("/new.app"), manifest)

    def test_matching_identities_pass(self):
        self._check("ScriptCue Sign", "ScriptCue Sign", "ScriptCue Sign")

    def test_conflicting_staged_rejected(self):
        with self.assertRaises(UpdateError):
            self._check("ScriptCue Sign", "Other Sign", "ScriptCue Sign")

    def test_conflicting_manifest_rejected(self):
        with self.assertRaises(UpdateError):
            self._check("ScriptCue Sign", "ScriptCue Sign", "Other Sign")

    def test_adhoc_skips_comparison(self):
        self._check(None, None, None)
        self._check(None, "ScriptCue Sign", None)


class ApplyMacosTest(unittest.TestCase):
    """_apply_macos 三条关键路径：selftest 失败 / 签名不兼容 / 成功派发。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sc-apply-mac-test-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bundle = self.root / "ScriptCue.app"
        (self.bundle / "Contents" / "MacOS").mkdir(parents=True)
        self.package = self.root / "ScriptCueAgent-macos-arm64-update.tar.gz"
        self.package.write_bytes(b"fake-package")
        self.replace_calls: list[tuple[Path, Path]] = []

    def _staging_root(self) -> Path:
        return self.root / f".{self.bundle.name}.new-{os.getpid()}"

    def _apply(self, *, selftest=(True, "预检通过"), compat_error=None):
        """在隔离补丁下跑 _apply_macos（不真的解包/替换/重启）。"""
        def fake_extract(tar_gz, staging_root):
            app = Path(staging_root) / "ScriptCue.app"
            bindir = app / "Contents" / "MacOS"
            bindir.mkdir(parents=True, exist_ok=True)
            (bindir / Path(sys.executable).name).write_bytes(b"bin")
            return app

        def fake_replace(bundle, staged_app, **kwargs):
            self.replace_calls.append((Path(bundle), Path(staged_app)))

        up = AgentUpdater(emit=lambda e: None, safety_ok=lambda: True,
                          server_url_fn=lambda: "")
        with mock.patch.object(updater, "locate_bundle", return_value=self.bundle), \
                mock.patch.object(updater, "extract_update", side_effect=fake_extract), \
                mock.patch.object(updater, "check_codesign_compat",
                                  side_effect=compat_error), \
                mock.patch.object(updater, "verify_codesign"), \
                mock.patch.object(updater, "run_selftest", return_value=selftest), \
                mock.patch.object(updater, "replace_macos", side_effect=fake_replace), \
                mock.patch.object(AgentUpdater, "_prepare_restart"):
            up._apply_macos(self.package, "9.9.9", "ScriptCue Sign")
        return up

    def test_selftest_failure_aborts_and_cleans(self):
        with self.assertRaises(UpdateError):
            self._apply(selftest=(False, "模拟预检失败"))
        self.assertEqual(self.replace_calls, [], "预检未过不得派发替换")
        self.assertFalse(self._staging_root().exists(), "失败路径必须清理暂存目录")

    def test_codesign_incompatible_aborts(self):
        with self.assertRaises(UpdateError):
            self._apply(compat_error=UpdateError("签名身份不一致"))
        self.assertEqual(self.replace_calls, [])
        self.assertFalse(self._staging_root().exists())

    def test_success_dispatches_replace(self):
        self._apply()
        self.assertEqual(len(self.replace_calls), 1)
        self.assertEqual(self.replace_calls[0][0], self.bundle)


# ---------------------------------------------------------------------------
# 编排层：状态文件与开发版跳过
# ---------------------------------------------------------------------------

class DevVersionSkipTest(unittest.TestCase):

    def test_dev_version_skips_everything(self):
        events: list[dict] = []
        with mock.patch.object(updater, "fetch_manifest",
                               side_effect=AssertionError("dev 版不得发起网络请求")):
            up = AgentUpdater(emit=events.append, safety_ok=lambda: True,
                              server_url_fn=lambda: "wss://example.test")
            up.check_and_download(manual=True)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "update_none")
        self.assertTrue(events[0]["manual"])


class UpdateStateTest(unittest.TestCase):
    """启动健康状态机：武装 / 正常退出清零 / 连崩自动回退 / 健康确认。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sc-state-test-")
        self.addCleanup(self._tmp.cleanup)
        self.state_file = Path(self._tmp.name) / "update-state.json"
        self._patch = mock.patch.object(updater, "state_path",
                                        side_effect=lambda: self.state_file)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _make(self) -> AgentUpdater:
        return AgentUpdater(emit=lambda e: None, safety_ok=lambda: True,
                            server_url_fn=lambda: "")

    def _write_state(self, **over) -> None:
        state = {"applied_version": "9.9.9", "previous_version": "9.9.8",
                 "confirmed": False, "crash_streak": 0,
                 "launch_armed": False, "clean_exit": False}
        state.update(over)
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def _read_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_gate_arms_first_launch_without_counting(self):
        """更新落地后的首次启动：只武装本次启动，不计连崩。"""
        self._write_state()
        with mock.patch.object(updater, "previous_install_path", return_value=None):
            startup_health_gate()
        state = self._read_state()
        self.assertEqual(state["crash_streak"], 0)
        self.assertTrue(state["launch_armed"])
        self.assertFalse(state["clean_exit"])
        self.assertFalse(state["confirmed"])

    def test_gate_counts_unresolved_launch(self):
        """上次已武装但未正常收尾（崩溃/被强杀）→ 连崩计数 +1。"""
        self._write_state(launch_armed=True)
        with mock.patch.object(updater, "previous_install_path", return_value=None):
            startup_health_gate()
        state = self._read_state()
        self.assertEqual(state["crash_streak"], 1)
        self.assertTrue(state["launch_armed"])

    def test_gate_resets_streak_after_clean_exit(self):
        """上次正常退出 → 连崩清零（正常开关程序永不触发自动回退）。"""
        self._write_state(launch_armed=True, clean_exit=True, crash_streak=1)
        with mock.patch.object(updater, "previous_install_path", return_value=None):
            startup_health_gate()
        self.assertEqual(self._read_state()["crash_streak"], 0)

    def test_gate_rolls_back_after_consecutive_crashes(self):
        """连续 ROLLBACK_AFTER_CRASHES 次启动未正常收尾且存在 .old → 自动回退。"""
        self._write_state(launch_armed=True,
                          crash_streak=updater.ROLLBACK_AFTER_CRASHES - 1)
        fake_old = Path(self._tmp.name) / "ScriptCueAgent.exe.old"
        with mock.patch.object(updater, "previous_install_path",
                               return_value=fake_old), \
                mock.patch.object(updater, "perform_rollback") as rollback:
            startup_health_gate()
        rollback.assert_called_once()
        self.assertEqual(self._read_state()["crash_streak"],
                         updater.ROLLBACK_AFTER_CRASHES)

    def test_gate_without_old_warns_and_continues(self):
        """连崩但无 .old：只告警，不调用回退。"""
        self._write_state(launch_armed=True,
                          crash_streak=updater.ROLLBACK_AFTER_CRASHES - 1)
        with mock.patch.object(updater, "previous_install_path", return_value=None), \
                mock.patch.object(updater, "perform_rollback") as rollback:
            startup_health_gate()
        rollback.assert_not_called()
        self.assertEqual(self._read_state()["crash_streak"],
                         updater.ROLLBACK_AFTER_CRASHES)

    def test_gate_rollback_failure_does_not_raise(self):
        """回退自身失败时状态机只记日志、不阻断启动。"""
        self._write_state(launch_armed=True,
                          crash_streak=updater.ROLLBACK_AFTER_CRASHES - 1)
        fake_old = Path(self._tmp.name) / "ScriptCueAgent.exe.old"
        with mock.patch.object(updater, "previous_install_path",
                               return_value=fake_old), \
                mock.patch.object(updater, "perform_rollback",
                                  side_effect=UpdateError("模拟回退失败")):
            startup_health_gate()   # 不应抛异常

    def test_note_clean_exit_marks_state(self):
        self._write_state(launch_armed=True)
        note_clean_exit()
        self.assertTrue(self._read_state()["clean_exit"])

    def test_note_clean_exit_ignores_confirmed(self):
        self._write_state(confirmed=True)
        before = self._read_state()
        note_clean_exit()
        self.assertEqual(self._read_state(), before)

    def test_mark_healthy_confirms(self):
        self._write_state(launch_armed=True, crash_streak=1)
        self._make().mark_healthy()
        self.assertTrue(self._read_state()["confirmed"])

    def test_rollback_state_swaps_versions(self):
        state = state_after_rollback({
            "applied_version": "9.9.9", "previous_version": "9.9.8",
            "confirmed": False, "crash_streak": 2})
        self.assertEqual(state["applied_version"], "9.9.8")
        self.assertEqual(state["previous_version"], "9.9.9")
        self.assertTrue(state["confirmed"])
        self.assertEqual(state["crash_streak"], 0)

    def test_no_state_file_is_noop(self):
        startup_health_gate()   # 无状态文件：不应抛异常、不创建文件
        self.assertFalse(self.state_file.exists())


class ActionLockTest(unittest.TestCase):
    """替换/回退门闩：并发提交被拒；失败后待安装信息恢复。"""

    def test_apply_without_package_rejected(self):
        events: list[dict] = []
        up = AgentUpdater(emit=events.append, safety_ok=lambda: True,
                          server_url_fn=lambda: "")
        self.assertFalse(up.apply())
        self.assertEqual(events[0]["event"], "update_failed")

    def test_double_apply_rejected_by_lock(self):
        events: list[dict] = []
        up = AgentUpdater(emit=events.append, safety_ok=lambda: True,
                          server_url_fn=lambda: "")
        up._ready = {"version": "9.9.9", "path": "unused", "notes": ""}
        release = threading.Event()

        def fake_worker(ready):
            try:
                release.wait(5)
            finally:
                up._action_lock.release()

        with mock.patch.object(AgentUpdater, "_apply_worker",
                               side_effect=fake_worker):
            self.assertTrue(up.apply())
            self.assertIsNone(up._ready, "提交成功后待安装信息被消费")
            self.assertFalse(up.apply(), "持锁期间重复提交必须被拒")
            release.set()
        self.assertEqual([e["event"] for e in events], ["update_none"])

    def test_apply_failure_restores_ready(self):
        events: list[dict] = []
        up = AgentUpdater(emit=events.append, safety_ok=lambda: False,
                          server_url_fn=lambda: "")
        up._ready = {"version": "9.9.9", "path": "missing", "notes": ""}
        self.assertTrue(up.apply())
        deadline = time.time() + 5
        while not events and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(events, "应产出 update_failed 事件")
        self.assertEqual(events[0]["event"], "update_failed")
        self.assertEqual(events[0].get("ready", {}).get("version"), "9.9.9")
        self.assertIsNotNone(up._ready, "失败后应恢复待安装信息以便重试")


# ---------------------------------------------------------------------------
# 手动/服务端推送入口的汇合（不建 GUI，直接调 check_and_download）
# ---------------------------------------------------------------------------

class CheckFlowTest(unittest.TestCase):
    """用假清单驱动完整检查流程：发现新版本 → 已下载提示（离线、不真下载）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sc-flow-test-")
        self.addCleanup(self._tmp.cleanup)
        self.payload = b"payload-bytes" * 32

    def test_downloaded_once_then_idempotent(self):
        manifest = make_manifest("9.9.9", "ScriptCueAgent-windows-x64.exe",
                                 self.payload)
        events: list[dict] = []
        up = AgentUpdater(emit=events.append, safety_ok=lambda: True,
                          server_url_fn=lambda: "wss://example.test")
        with mock.patch.object(updater, "APP_VERSION", "1.0.3"), \
                mock.patch.object(updater, "platform_key",
                                  return_value="windows-x64"), \
                mock.patch.object(updater, "manifest_url_for",
                                  return_value="https://m.example.com/update/manifest.json"), \
                mock.patch.object(updater, "updates_dir",
                                  return_value=Path(self._tmp.name) / "updates"), \
                mock.patch.object(updater, "fetch_manifest", return_value=manifest), \
                mock.patch.object(updater, "stream_download",
                                  side_effect=lambda url, dest, size, cb: Path(dest).write_bytes(self.payload)):
            up.check_and_download()
            kinds = [e["event"] for e in events]
            self.assertIn("update_available", kinds)
            self.assertIn("update_downloaded", kinds)
            self.assertEqual(
                [e for e in events if e["event"] == "update_downloaded"][0]["version"],
                "9.9.9")

            # 第二次检查：文件已在本地且校验通过 → 只提示"已下载"，不再下载
            events.clear()
            up.check_and_download()
            self.assertEqual([e["event"] for e in events], ["update_available"])
            self.assertTrue(events[0]["downloaded"])

    def test_waiting_outside_safety_window(self):
        """非安全窗口：只提示等待，窗口判定变为安全后才下载。"""
        manifest = make_manifest("9.9.9", "ScriptCueAgent-windows-x64.exe",
                                 self.payload)
        calls = {"n": 0}

        def fake_safety():
            # 前两次判定（首次上报 + 进入等待判断）为“演出中”，之后窗口打开
            calls["n"] += 1
            return calls["n"] > 2

        events: list[dict] = []
        up = AgentUpdater(emit=events.append, safety_ok=fake_safety,
                          server_url_fn=lambda: "wss://example.test")
        downloaded = {"called": False}

        def fake_stream(url, dest, size, cb):
            downloaded["called"] = True
            Path(dest).write_bytes(self.payload)

        with mock.patch.object(updater, "APP_VERSION", "1.0.3"), \
                mock.patch.object(updater, "platform_key",
                                  return_value="windows-x64"), \
                mock.patch.object(updater, "manifest_url_for",
                                  return_value="https://m.example.com/update/manifest.json"), \
                mock.patch.object(updater, "updates_dir",
                                  return_value=Path(self._tmp.name) / "updates"), \
                mock.patch.object(updater, "fetch_manifest", return_value=manifest), \
                mock.patch.object(updater, "stream_download", side_effect=fake_stream), \
                mock.patch.object(updater, "WINDOW_POLL_S", 0.01):
            up.check_and_download()
        self.assertTrue(downloaded["called"], "窗口打开后应自动继续下载")
        self.assertTrue(events[0]["waiting"], "首个事件应标记等待窗口")
        self.assertFalse(events[0]["downloaded"])


class ServerManifestOfferTest(unittest.TestCase):
    """服务端推送清单（update.available）与轮询走同一处理。"""

    def test_offer_uses_pushed_manifest_without_get(self):
        """推送路径在下载前不得重新 GET 清单（下载自身会双取，此处以假下载跳过）。"""
        events: list[dict] = []
        up = AgentUpdater(emit=events.append, safety_ok=lambda: True,
                          server_url_fn=lambda: "")
        manifest = make_manifest("9.9.9", "ScriptCueAgent-windows-x64.exe", b"x" * 8)
        result = {"path": Path("unused.exe"), "version": "9.9.9",
                  "sha256": manifest["assets"]["windows-x64"]["sha256"],
                  "size": 8, "notes": "",
                  "asset": manifest["assets"]["windows-x64"]}
        with tempfile.TemporaryDirectory(prefix="sc-offer-test-") as tmp, \
                mock.patch.object(updater, "APP_VERSION", "1.0.3"), \
                mock.patch.object(updater, "platform_key",
                                  return_value="windows-x64"), \
                mock.patch.object(updater, "updates_dir",
                                  return_value=Path(tmp) / "updates"), \
                mock.patch.object(updater, "fetch_manifest",
                                  side_effect=AssertionError("推送路径不应重新 GET 清单")), \
                mock.patch.object(updater, "download_verified", return_value=result):
            up.check_and_download(manifest=manifest, from_server=True)
        self.assertEqual([e["event"] for e in events],
                         ["update_available", "update_downloaded"])
        self.assertEqual(events[1]["version"], "9.9.9")


if __name__ == "__main__":
    unittest.main()
