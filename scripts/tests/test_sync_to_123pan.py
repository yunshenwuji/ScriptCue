"""发布流水线 CDN 同步脚本的聚焦检查：重试分类、回读校验与目录定位。

覆盖 scripts/sync_to_123pan.py 的关键约定（背景见 docs/auto-update.md §3.1）：

1. multipart 结构（字段名、filename、etag=文件 MD5、duplicate=2 覆盖策略）；
2. 重试分类：网络类/5xx/429 重试，4xx（含 401）立即抛出，重试耗尽抛 SyncError；
3. 回读校验三层判据：version → released_at（区分同版本旧构建）→ 最小产物直链大小；
4. 目录定位：PAN123_PARENT_FILE_ID 优先、按名翻页搜索、回收站过滤、未找到/多义报错；
5. 主流程护栏：缺凭据、缺清单、版本不符、assets 为空均在上传前失败。

全程不联网（_raw_request / _fetch_json_url / _remote_size 全部替换为桩），
不写真实数据目录（临时目录使用后自动清理）。

在仓库根目录运行：
    python -m unittest discover -s scripts/tests -t scripts -v
"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sync_to_123pan as sync


def make_manifest(version="1.2.0", released_at="2026-10-17T00:00:00+00:00",
                  size=16) -> dict:
    return {
        "schema": 1,
        "version": version,
        "released_at": released_at,
        "assets": {
            "windows-x64": {
                "asset": "ScriptCueAgent-windows-x64.exe",
                "kind": "exe",
                "sha256": "0" * 64,
                "size": size,
            },
        },
    }


class FakeClient:
    """代替 Client：按顺序吐出预置响应，并记录 query。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.queries = []

    def call(self, method, path, *, body=None, query=None, base=None, timeout=None):
        self.queries.append(dict(query or {}))
        return self.pages.pop(0)


class MultipartTest(unittest.TestCase):
    """multipart/form-data 结构。"""

    def test_structure(self):
        content_type, body = sync._multipart_body(
            {"parentFileID": "7", "duplicate": "2"},
            {"file": ("a.exe", b"XYZ")})
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="parentFileID"', body)
        self.assertIn(b'name="duplicate"', body)
        self.assertIn(b'name="duplicate"\r\n\r\n2\r\n', body)
        self.assertIn(b'filename="a.exe"', body)
        self.assertIn(b"XYZ", body)
        self.assertTrue(body.endswith(b"--\r\n"))


class JsonDataTest(unittest.TestCase):
    """OpenAPI 响应解析与错误映射。"""

    def test_ok_returns_data(self):
        raw = json.dumps({"code": 0, "message": "ok", "data": {"a": 1}}).encode()
        self.assertEqual(sync._json_data(200, raw), {"a": 1})

    def test_nonzero_code_raises(self):
        raw = json.dumps({"code": 429, "message": "too many"}).encode()
        with self.assertRaises(sync.ApiError) as ctx:
            sync._json_data(200, raw)
        self.assertEqual(ctx.exception.code, 429)

    def test_non_json_body_raises(self):
        with self.assertRaises(sync.ApiError):
            sync._json_data(502, b"<html>bad gateway</html>")


class RetryClassifyTest(unittest.TestCase):
    """可重试错误分类。"""

    def test_network_error_retryable(self):
        self.assertTrue(sync._retryable(sync.ApiError(None, None, "net")))

    def test_5xx_and_429_retryable(self):
        self.assertTrue(sync._retryable(sync.ApiError(500, 500, "srv")))
        self.assertTrue(sync._retryable(sync.ApiError(429, 429, "qps")))
        self.assertTrue(sync._retryable(sync.ApiError(200, 429, "qps-body")))

    def test_4xx_not_retryable(self):
        self.assertFalse(sync._retryable(sync.ApiError(404, 404, "nf")))
        self.assertFalse(sync._retryable(sync.ApiError(200, 401, "token")))


class WithRetryTest(unittest.TestCase):
    """_with_retry 的行为。"""

    def setUp(self):
        patcher = mock.patch.object(sync.time, "sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def action():
            calls["n"] += 1
            if calls["n"] < 3:
                raise sync.ApiError(500, 500, "boom")
            return "ok"

        self.assertEqual(sync._with_retry("x", action), "ok")
        self.assertEqual(calls["n"], 3)

    def test_non_retryable_raises_immediately(self):
        calls = {"n": 0}

        def action():
            calls["n"] += 1
            raise sync.ApiError(404, 404, "no")

        with self.assertRaises(sync.ApiError):
            sync._with_retry("x", action)
        self.assertEqual(calls["n"], 1)

    def test_exhausted_raises_sync_error(self):
        def action():
            raise sync.ApiError(None, None, "net")

        with self.assertRaises(sync.SyncError):
            sync._with_retry("x", action)


class ManifestMismatchTest(unittest.TestCase):
    """回读校验三层判据（纯函数部分）。"""

    BASE = "https://cdn.example.com/app"

    def test_version_mismatch(self):
        reason = sync._manifest_mismatch(make_manifest(version="1.1.1"),
                                         "1.2.0", None, self.BASE)
        self.assertIn("version", reason)

    def test_released_at_mismatch_same_version(self):
        stale = make_manifest(version="1.2.0",
                              released_at="2026-10-16T00:00:00+00:00")
        with mock.patch.object(sync, "_remote_size", return_value=16):
            reason = sync._manifest_mismatch(stale, "1.2.0",
                                             "2026-10-17T00:00:00+00:00", self.BASE)
        self.assertIn("released_at", reason)

    def test_artifact_size_mismatch(self):
        with mock.patch.object(sync, "_remote_size", return_value=999):
            reason = sync._manifest_mismatch(make_manifest(), "1.2.0", None, self.BASE)
        self.assertIn("直链大小", reason)

    def test_all_match(self):
        with mock.patch.object(sync, "_remote_size", return_value=16):
            self.assertIsNone(sync._manifest_mismatch(make_manifest(), "1.2.0", None,
                                                      self.BASE))

    def test_remote_size_unknown_tolerated(self):
        with mock.patch.object(sync, "_remote_size", return_value=None):
            self.assertIsNone(sync._manifest_mismatch(make_manifest(), "1.2.0", None,
                                                      self.BASE))


class VerifyManifestTest(unittest.TestCase):
    """verify_manifest 的重试与失败语义。"""

    BASE = "https://cdn.example.com/app"
    RELEASED_AT = "2026-10-17T00:00:00+00:00"

    def setUp(self):
        patcher = mock.patch.object(sync.time, "sleep", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stale_then_fresh_passes(self):
        stale = make_manifest(version="1.1.1")
        fresh = make_manifest()
        with mock.patch.object(sync, "_fetch_json_url", side_effect=[stale, fresh]), \
                mock.patch.object(sync, "_remote_size", return_value=16):
            sync.verify_manifest(self.BASE, "1.2.0", self.RELEASED_AT)

    def test_same_version_stale_build_never_passes(self):
        stale = make_manifest(released_at="2026-10-16T00:00:00+00:00")
        with mock.patch.object(sync, "_fetch_json_url", return_value=stale), \
                mock.patch.object(sync, "_remote_size", return_value=16):
            with self.assertRaises(sync.SyncError):
                sync.verify_manifest(self.BASE, "1.2.0", self.RELEASED_AT)

    def test_fetch_failures_exhausted(self):
        with mock.patch.object(sync, "_fetch_json_url",
                               side_effect=OSError("connection reset")):
            with self.assertRaises(sync.SyncError):
                sync.verify_manifest(self.BASE, "1.2.0", self.RELEASED_AT)


class ResolveParentIdTest(unittest.TestCase):
    """目录定位：钉住优先、按名搜索、翻页、回收站过滤与报错文案。"""

    NAME = "ScriptCue-update"

    def _entry(self, **over):
        entry = {"fileId": 100, "filename": self.NAME, "type": 1, "trashed": 0}
        entry.update(over)
        return entry

    def test_pinned_id_wins(self):
        with mock.patch.dict(os.environ, {"PAN123_PARENT_FILE_ID": "42"}):
            self.assertEqual(sync.resolve_parent_id(object(), self.NAME), 42)

    def test_invalid_pinned_id(self):
        with mock.patch.dict(os.environ, {"PAN123_PARENT_FILE_ID": "abc"}):
            with self.assertRaises(sync.SyncError):
                sync.resolve_parent_id(object(), self.NAME)

    def test_exact_name_match(self):
        client = FakeClient([
            {"lastFileId": -1, "fileList": [
                self._entry(),
                self._entry(fileId=200, filename=self.NAME + "-old"),
                self._entry(fileId=300, type=0),
            ]},
        ])
        with mock.patch.dict(os.environ, {"PAN123_PARENT_FILE_ID": ""}):
            self.assertEqual(sync.resolve_parent_id(client, self.NAME), 100)

    def test_pagination_uses_last_file_id(self):
        client = FakeClient([
            {"lastFileId": 55, "fileList": [self._entry(fileId=300, filename="other")]},
            {"lastFileId": -1, "fileList": [self._entry()]},
        ])
        with mock.patch.dict(os.environ, {"PAN123_PARENT_FILE_ID": ""}):
            self.assertEqual(sync.resolve_parent_id(client, self.NAME), 100)
        self.assertEqual(client.queries[1].get("lastFileId"), 55)

    def test_not_found_message(self):
        client = FakeClient([{"lastFileId": -1, "fileList": []}])
        with mock.patch.dict(os.environ, {"PAN123_PARENT_FILE_ID": ""}):
            with self.assertRaises(sync.SyncError) as ctx:
                sync.resolve_parent_id(client, self.NAME)
        self.assertIn("未找到", str(ctx.exception))

    def test_multiple_matches_ask_pinned_id(self):
        client = FakeClient([{"lastFileId": -1, "fileList": [
            self._entry(fileId=100), self._entry(fileId=200)]}])
        with mock.patch.dict(os.environ, {"PAN123_PARENT_FILE_ID": ""}):
            with self.assertRaises(sync.SyncError) as ctx:
                sync.resolve_parent_id(client, self.NAME)
        self.assertIn("PAN123_PARENT_FILE_ID", str(ctx.exception))

    def test_trashed_entries_filtered(self):
        client = FakeClient([{"lastFileId": -1,
                              "fileList": [self._entry(trashed=1)]}])
        with mock.patch.dict(os.environ, {"PAN123_PARENT_FILE_ID": ""}):
            with self.assertRaises(sync.SyncError):
                sync.resolve_parent_id(client, self.NAME)


class UploadSingleTest(unittest.TestCase):
    """单步上传请求体与大小护栏。"""

    def _temp_file(self, payload: bytes) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / "ScriptCueAgent-windows-x64.exe"
        path.write_bytes(payload)
        return path

    def test_etag_md5_and_duplicate(self):
        payload = b"x" * 32
        path = self._temp_file(payload)
        captured = {}

        def fake_raw(method, url, *, data=None, headers=None, timeout=None):
            captured["url"] = url
            captured["body"] = data
            payload_ok = json.dumps({"code": 0, "message": "ok",
                                     "data": {"fileID": 9, "completed": True}})
            return 200, payload_ok.encode()

        client = FakeClient([])
        client.token = "T"
        client.refresh_token = lambda: None
        with mock.patch.object(sync, "_raw_request", side_effect=fake_raw):
            result = sync.upload_single(client, "https://up.example.com", 7, path)

        self.assertTrue(result["completed"])
        self.assertTrue(captured["url"].endswith("/upload/v2/file/single/create"))
        body = captured["body"]
        self.assertIn(b'name="etag"', body)
        self.assertIn(hashlib.md5(payload).hexdigest().encode(), body)
        self.assertIn(b'name="duplicate"\r\n\r\n2\r\n', body)
        self.assertIn(b'name="parentFileID"\r\n\r\n7\r\n', body)

    def test_oversize_rejected_before_read(self):
        path = self._temp_file(b"x" * 64)
        with mock.patch.object(sync, "MAX_SINGLE_UPLOAD_BYTES", 10):
            with self.assertRaises(sync.SyncError):
                sync.upload_single(FakeClient([]), "https://up.example.com", 1, path)


class TokenHintTest(unittest.TestCase):
    """token 受限时的可操作提示。"""

    def test_limit_message_gets_hint(self):
        hint = sync._token_limit_hint(
            "HTTP 200 code=1234 message=tokens number has exceeded the limit")
        self.assertIn("3", hint)

    def test_other_errors_get_no_hint(self):
        self.assertEqual(sync._token_limit_hint("HTTP 401 code=401 "
                                                "message=invalid credential"), "")


class MainGuardTest(unittest.TestCase):
    """主流程护栏：全部在上传前失败，不触碰网络。"""

    def _write_manifest(self, manifest: dict) -> str:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        (Path(td.name) / "update-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return td.name

    def test_missing_credentials(self):
        with mock.patch.dict(os.environ, {"PAN123_CLIENT_ID": "",
                                          "PAN123_CLIENT_SECRET": ""}):
            with self.assertRaises(sync.SyncError) as ctx:
                sync.main(["--dist", "unused"])
        self.assertIn("PAN123_CLIENT_ID", str(ctx.exception))

    def test_missing_manifest_file(self):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"PAN123_CLIENT_ID": "i",
                                             "PAN123_CLIENT_SECRET": "s"}):
            with self.assertRaises(sync.SyncError) as ctx:
                sync.main(["--dist", td])
        self.assertIn("未找到清单文件", str(ctx.exception))

    def test_expect_version_mismatch(self):
        dist = self._write_manifest(make_manifest(version="1.0.0"))
        with mock.patch.dict(os.environ, {"PAN123_CLIENT_ID": "i",
                                          "PAN123_CLIENT_SECRET": "s"}):
            with self.assertRaises(sync.SyncError) as ctx:
                sync.main(["--dist", dist, "--expect-version", "2.0.0"])
        self.assertIn("不一致", str(ctx.exception))

    def test_empty_assets_rejected(self):
        dist = self._write_manifest({"schema": 1, "version": "1.2.0",
                                     "released_at": "2026-10-17T00:00:00+00:00",
                                     "assets": {}})
        with mock.patch.dict(os.environ, {"PAN123_CLIENT_ID": "i",
                                          "PAN123_CLIENT_SECRET": "s"}):
            with self.assertRaises(sync.SyncError) as ctx:
                sync.main(["--dist", dist])
        self.assertIn("assets 为空", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
