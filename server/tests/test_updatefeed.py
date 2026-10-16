"""服务端更新清单源聚焦检查：覆盖文件优先、CDN 降级、url 注入、端点与推送去重。

覆盖 docs/auto-update.md 与 server/server/updatefeed.py、ws.py 的关键约定：

1. SC_UPDATE_MANIFEST_FILE 存在且可解析时完全接管并停用 CDN 刷新（钉版本）；
2. CDN 拉取失败时继续服务旧缓存（只记日志，不抛异常）；
3. serve() 给资产注入绝对 url（ASSET_BASE_URL 优先 / 清单 URL 同目录兜底）并附加
   server_time，且不污染内存中的原始清单；
4. 无清单时 /update/manifest.json 返回 404 + {"error": "no_manifest"}；
5. push_update_if_behind 只推 proto≥2 且版本落后的会话，且同一版本只推一次。

全程不联网（HTTP 由 monkeypatch 替换）；SC_DATA_DIR 指向临时目录，不写真实数据目录。

在仓库根目录运行：
    python -m unittest discover -s server/tests -t server -v
"""

import asyncio
import atexit
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# 必须在导入 server.main 之前重定向数据目录并清空更新相关环境变量：
# 避免审计日志写入真实 ./server/data，且让模块级 update_feed 单例以"无清单"状态创建
_TEST_DATA_DIR = tempfile.TemporaryDirectory(prefix="scriptcue-srv-test-",
                                             ignore_cleanup_errors=True)
os.environ["SC_DATA_DIR"] = _TEST_DATA_DIR.name
for _var in ("SC_UPDATE_MANIFEST_URL", "SC_UPDATE_MANIFEST_FILE",
             "SC_UPDATE_ASSET_BASE_URL", "SC_UPDATE_DISABLED"):
    os.environ.pop(_var, None)

from server import main as server_main  # noqa: E402
from server import updatefeed  # noqa: E402
from server.updatefeed import UpdateFeed  # noqa: E402
from server.ws import push_update_if_behind  # noqa: E402


def _close_audit_at_exit() -> None:
    """退出时先关审计日志句柄（注册晚于临时目录 → LIFO 先执行），

    否则 Windows 上临时目录清理会因文件占用报 PermissionError。
    """
    try:
        server_main.audit.close()
    except Exception:  # noqa: BLE001 - 退出阶段尽力而为
        pass


atexit.register(_close_audit_at_exit)


def make_manifest(version: str = "1.1.0") -> dict:
    return {
        "schema": 1,
        "version": version,
        "notes": "聚焦检查",
        "assets": {
            "windows-x64": {"asset": "ScriptCueAgent-windows-x64.exe", "kind": "exe",
                            "sha256": "a" * 64, "size": 100},
        },
    }


class FeedTestBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sc-feed-test-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.cache = self.dir / "update-cache.json"


class OverrideFileTest(FeedTestBase):

    def test_override_file_takes_over_and_skips_cdn(self):
        override = self.dir / "update-manifest.json"
        override.write_text(json.dumps(make_manifest("1.1.0")), encoding="utf-8")
        feed = UpdateFeed(self.cache,
                          manifest_url="https://cdn.invalid/update-manifest.json",
                          override_file=str(override), disabled=False)
        with mock.patch.object(updatefeed, "_http_get_json",
                               side_effect=AssertionError("覆盖文件生效时不得访问 CDN")):
            result = feed.refresh()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "file")
        self.assertEqual(feed.latest_version(), "1.1.0")

    def test_override_file_with_bom_accepted(self):
        """Windows 编辑器/PowerShell 写出的带 BOM JSON 必须能正常解析。"""
        override = self.dir / "update-manifest.json"
        payload = json.dumps(make_manifest("1.1.0"), ensure_ascii=False).encode("utf-8")
        override.write_bytes(b"\xef\xbb\xbf" + payload)
        feed = UpdateFeed(self.cache, manifest_url="", override_file=str(override),
                          disabled=False)
        result = feed.refresh()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "file")
        self.assertEqual(feed.latest_version(), "1.1.0")

    def test_invalid_override_falls_back_to_cdn(self):
        override = self.dir / "update-manifest.json"
        override.write_text("{ not json", encoding="utf-8")
        feed = UpdateFeed(self.cache, manifest_url="https://cdn.test/m.json",
                          override_file=str(override), disabled=False)
        with mock.patch.object(updatefeed, "_http_get_json",
                               return_value=make_manifest("1.1.0")) as get:
            result = feed.refresh()
        self.assertEqual(result["source"], "cdn")
        get.assert_called_once()
        self.assertTrue(self.cache.exists(), "CDN 来源应落盘缓存")


class CdnFailureTest(FeedTestBase):

    def test_cdn_failure_keeps_serving_cache(self):
        self.cache.write_text(json.dumps(make_manifest("1.1.0")), encoding="utf-8")
        feed = UpdateFeed(self.cache, manifest_url="https://cdn.test/m.json",
                          override_file="", disabled=False)
        self.assertEqual(feed.latest_version(), "1.1.0", "启动时应先加载磁盘缓存")
        with mock.patch.object(updatefeed, "_http_get_json",
                               side_effect=OSError("network down")):
            result = feed.refresh()
        self.assertFalse(result["ok"], "本轮拉取失败应以 ok=False 上报（供审计）")
        self.assertEqual(result["source"], "cache")
        served = feed.serve()
        self.assertIsNotNone(served, "CDN 失败后仍应继续服务旧缓存")
        self.assertEqual(served["version"], "1.1.0")

    def test_invalid_cdn_manifest_rejected(self):
        feed = UpdateFeed(self.cache, manifest_url="https://cdn.test/m.json",
                          override_file="", disabled=False)
        with mock.patch.object(updatefeed, "_http_get_json",
                               return_value={"schema": 9}):
            result = feed.refresh()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["version"])
        self.assertIsNone(feed.serve())


class ServeTest(FeedTestBase):

    def test_injects_asset_base_url_and_server_time(self):
        feed = UpdateFeed(self.cache, manifest_url="", override_file="",
                          asset_base_url="https://cdn.kadaiad.fun/www/ScriptCue-update/",
                          disabled=False)
        feed._apply(make_manifest("1.1.0"), "file", persist=False)
        served = feed.serve()
        self.assertEqual(
            served["assets"]["windows-x64"]["url"],
            "https://cdn.kadaiad.fun/www/ScriptCue-update/ScriptCueAgent-windows-x64.exe")
        self.assertIsInstance(served["server_time"], int)
        # 下发是深拷贝：不得污染内存中的原始清单（否则二次下发会固化错误 URL）
        self.assertNotIn("url", feed._manifest["assets"]["windows-x64"])

    def test_url_falls_back_to_manifest_dir(self):
        feed = UpdateFeed(self.cache,
                          manifest_url="https://cdn.example.com/app/update-manifest.json",
                          override_file="", disabled=False)
        feed._apply(make_manifest("1.1.0"), "cdn", persist=False)
        served = feed.serve()
        self.assertEqual(
            served["assets"]["windows-x64"]["url"],
            "https://cdn.example.com/app/ScriptCueAgent-windows-x64.exe")

    def test_existing_url_kept(self):
        m = make_manifest("1.1.0")
        m["assets"]["windows-x64"]["url"] = "https://explicit.example.com/x.exe"
        feed = UpdateFeed(self.cache, manifest_url="", override_file="", disabled=False)
        feed._apply(m, "file", persist=False)
        self.assertEqual(feed.serve()["assets"]["windows-x64"]["url"],
                         "https://explicit.example.com/x.exe")


class IsBehindTest(FeedTestBase):

    def setUp(self):
        super().setUp()
        self.feed = UpdateFeed(self.cache, manifest_url="", override_file="",
                               disabled=False)
        self.feed._apply(make_manifest("1.1.0"), "file", persist=False)

    def test_behind(self):
        self.assertTrue(self.feed.is_behind("1.0.3"))

    def test_same_version_not_behind(self):
        self.assertFalse(self.feed.is_behind("1.1.0"))

    def test_dev_or_missing_version_not_behind(self):
        self.assertFalse(self.feed.is_behind("0.0.0.dev"))
        self.assertFalse(self.feed.is_behind(None))
        self.assertFalse(self.feed.is_behind(""))


class EndpointTest(FeedTestBase):

    def _feed(self, manifest: dict | None, base_url: str = "") -> UpdateFeed:
        feed = UpdateFeed(self.cache, manifest_url="", override_file="",
                          asset_base_url=base_url, disabled=False)
        if manifest is not None:
            feed._apply(manifest, "file", persist=False)
        return feed

    def test_404_when_no_manifest(self):
        with mock.patch.object(server_main, "update_feed", self._feed(None)):
            resp = asyncio.run(server_main.update_manifest())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(json.loads(resp.body.decode()), {"error": "no_manifest"})

    def test_serves_manifest_when_available(self):
        feed = self._feed(make_manifest("1.1.0"),
                          base_url="https://cdn.kadaiad.fun/www/ScriptCue-update/")
        with mock.patch.object(server_main, "update_feed", feed):
            resp = asyncio.run(server_main.update_manifest())
        self.assertIsInstance(resp, dict)
        self.assertEqual(resp["version"], "1.1.0")
        self.assertTrue(resp["assets"]["windows-x64"]["url"].startswith(
            "https://cdn.kadaiad.fun/www/ScriptCue-update/"))


class FakeSession:
    def __init__(self, proto: int = 2, online: bool = True,
                 build_version: str | None = "1.0.3"):
        self.session_id = "agent-1"
        self.nickname = "测试设备"
        self.proto = proto
        self.online = online
        self.build_version = build_version
        self.update_available = False
        self.last_pushed_version = None
        self.sent: list[dict] = []

    async def send(self, msg: dict) -> bool:
        self.sent.append(msg)
        return True


class FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class PushUpdateTest(FeedTestBase):
    """push_update_if_behind：proto≥2 + 版本落后 + 按版本去重。"""

    def setUp(self):
        super().setUp()
        self.room = SimpleNamespace(code="ABC123", agents={})
        self.audit = FakeAudit()

    def _feed(self, version: str = "1.1.0") -> UpdateFeed:
        feed = UpdateFeed(self.cache, manifest_url="", override_file="",
                          asset_base_url="https://cdn.example.com/app/",
                          disabled=False)
        feed._apply(make_manifest(version), "file", persist=False)
        return feed

    def test_push_once_per_version(self):
        feed = self._feed()
        session = FakeSession()
        asyncio.run(push_update_if_behind(self.room, session, feed, self.audit))
        asyncio.run(push_update_if_behind(self.room, session, feed, self.audit))
        self.assertEqual(len(session.sent), 1, "同一版本只推一次")
        self.assertEqual(session.sent[0]["type"], "update.available")
        self.assertEqual(session.sent[0]["manifest"]["version"], "1.1.0")
        self.assertTrue(session.update_available)
        pushed = [e for e in self.audit.events if e[0] == "update_pushed"]
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][1]["to_version"], "1.1.0")

    def test_new_version_pushes_again(self):
        session = FakeSession()
        asyncio.run(push_update_if_behind(self.room, session, self._feed("1.1.0"),
                                          self.audit))
        asyncio.run(push_update_if_behind(self.room, session, self._feed("1.2.0"),
                                          self.audit))
        self.assertEqual(len(session.sent), 2)

    def test_proto1_never_pushed(self):
        session = FakeSession(proto=1)
        asyncio.run(push_update_if_behind(self.room, session, self._feed(),
                                          self.audit))
        self.assertEqual(session.sent, [], "v1 被控端不认识 update.available，不得推送")

    def test_up_to_date_not_pushed(self):
        session = FakeSession(build_version="1.1.0")
        asyncio.run(push_update_if_behind(self.room, session, self._feed(),
                                          self.audit))
        self.assertEqual(session.sent, [])
        self.assertFalse(session.update_available)

    def test_offline_not_pushed(self):
        session = FakeSession(online=False)
        asyncio.run(push_update_if_behind(self.room, session, self._feed(),
                                          self.audit))
        self.assertEqual(session.sent, [])

    def test_no_manifest_not_pushed(self):
        session = FakeSession()
        empty = UpdateFeed(self.cache, manifest_url="", override_file="",
                           disabled=False)
        asyncio.run(push_update_if_behind(self.room, session, empty, self.audit))
        self.assertEqual(session.sent, [])


if __name__ == "__main__":
    unittest.main()
