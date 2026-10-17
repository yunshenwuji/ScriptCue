#!/usr/bin/env python3
"""发布流水线 CDN 同步：把产物与 update-manifest.json 覆盖上传到 123云盘直链空间。

由 .github/workflows/release.yml 的 release job 在创建 GitHub Release 后调用
（ubuntu-latest / python3），只依赖标准库。机制细节与手动兜底见 docs/auto-update.md §3.1。

环境变量：
    PAN123_CLIENT_ID / PAN123_CLIENT_SECRET   必填：开放平台凭据（网盘站内信获取）
    PAN123_PARENT_FILE_ID                     可选：目标目录 fileID；缺省按名称搜索

流程（以 2026-10 POC 实测结论为设计依据）：
1. 校验 dist/ 内清单引用的每个产物 size/sha256 与清单一致（产物与清单严格同批）；
2. 逐文件单步上传（upload/v2/file/single/create，单文件 ≤1GB）+ duplicate=2
   原地覆盖——fileID 与直链地址跨版本稳定、不产生回收站残留；
3. 顺序：先上传全部产物、后 update-manifest.json——避免清单指向从未上传过的文件名。
   注意：产物为同名原地覆盖，产物换新与清单覆盖之间存在短暂窗口，期间旧清单的
   sha256 会与 CDN 实际可下载内容暂时不一致（被控端自带 sha256 双取重试，属预期
   现象，见 docs/auto-update.md §3.1/§8）；
4. best-effort 调直链缓存刷新（实测覆盖即生效，刷新仅作保险）；
5. 回读 CDN 清单比对 version 与 released_at，并抽查最小产物直链大小；不一致时
   非零退出（流水线红灯）。

用法：
    python3 scripts/sync_to_123pan.py --dist dist --expect-version 1.1.2
"""

import argparse
import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API_BASE = "https://open-api.123pan.com"
PLATFORM_HEADER = "open_platform"
REQUEST_TIMEOUT_S = 60
UPLOAD_TIMEOUT_S = 600                 # 单文件 ≤1GB 的保守超时
RETRIES = 3                            # 网络类失败的最大尝试次数
RETRY_BACKOFF_S = 5
MAX_SINGLE_UPLOAD_BYTES = 1024 ** 3    # 单步上传接口限制（1GB）
DEFAULT_BASE_URL = "https://cdn.kadaiad.fun/www/ScriptCue-update/"
DEFAULT_FOLDER_NAME = "ScriptCue-update"
VERIFY_ATTEMPTS = 8                    # 回读校验最大轮数（CDN 边缘缓存可能滞后，见 §3.1）
VERIFY_INTERVAL_S = 15                 # 单轮间隔（总计约 105 秒）


class SyncError(RuntimeError):
    """同步流程的可预期失败：打印 ::error:: 注解并以非零退出。"""


def log(message: str) -> None:
    print(f"[sync] {message}", flush=True)


def annotate(kind: str, message: str) -> None:
    """GitHub Actions 注解（必须位于行首才能被识别）。"""
    print(f"::{kind}::{message}", flush=True)


class ApiError(RuntimeError):
    def __init__(self, http_status: int | None, code, message, trace_id=None):
        self.http_status = http_status
        self.code = code
        self.message = message
        self.trace_id = trace_id
        super().__init__(f"HTTP {http_status} code={code} message={message}"
                         + (f" x-traceID={trace_id}" if trace_id else ""))


# ---------------------------------------------------------------------------
# HTTP 基础
# ---------------------------------------------------------------------------

def _raw_request(method: str, url: str, *, data: bytes | None = None,
                 headers: dict | None = None,
                 timeout: float = REQUEST_TIMEOUT_S) -> tuple[int, bytes]:
    """发一次请求；HTTP 层错误也返回 (status, body)，网络错误抛 OSError。"""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json_data(status: int, raw: bytes):
    """解析 OpenAPI 响应；code != 0 抛 ApiError。"""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(status, None, f"响应不是合法 JSON: {raw[:200]!r}") from None
    if payload.get("code") != 0:
        raise ApiError(status, payload.get("code"), payload.get("message"),
                       payload.get("x-traceID"))
    return payload.get("data")


def _retryable(exc: ApiError) -> bool:
    return (exc.http_status is None or exc.http_status >= 500
            or exc.http_status == 429 or exc.code == 429)


def _with_retry(what: str, action):
    """执行 action()；对网络类与 5xx/429 失败退避重试，其余异常直接抛。"""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            return action()
        except ApiError as exc:
            if not _retryable(exc):
                raise
            last = exc
        except (OSError, http.client.HTTPException) as exc:
            # 连接失败 / 超时（URLError 等），以及响应中途断连（IncompleteRead）
            last = ApiError(None, None, f"网络错误: {exc}")
        if attempt < RETRIES:
            delay = RETRY_BACKOFF_S * attempt
            log(f"{what}失败（第 {attempt}/{RETRIES} 次: {last}），{delay}s 后重试")
            time.sleep(delay)
    raise SyncError(f"{what}重试 {RETRIES} 次仍失败：{last}")


def api(method: str, path: str, *, body: dict | None = None,
        query: dict | None = None, token: str | None = None,
        base: str = API_BASE, timeout: float = REQUEST_TIMEOUT_S):
    """单次请求 OpenAPI，返回响应 data 字段；失败抛 ApiError / OSError。"""
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Platform": PLATFORM_HEADER, "Accept": "application/json",
               "User-Agent": "ScriptCueReleaseSync/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, raw = _raw_request(method, url, data=data, headers=headers,
                               timeout=timeout)
    return _json_data(status, raw)


_TOKEN_LIMIT_PATTERNS = ("exceed", "limit", "too many", "上限", "超限", "频繁")


def _token_limit_hint(message: str) -> str:
    """token 数量/频次受限时追加可操作提示（同 client_id 最多 3 个 token 并存）。"""
    lowered = (message or "").lower()
    if any(pattern in lowered for pattern in _TOKEN_LIMIT_PATTERNS):
        return ("；可能达到同一 client_id 最多 3 个 access_token 并存的上限或触发频控，"
                "可稍后重试（旧 token 到期释放），或到开放平台侧处理/联系客服")
    return ""


class Client:
    """OpenAPI 客户端：token 管理（401 自动重取一次）+ 网络类失败重试。"""

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self.token = self._fetch_token()

    def _fetch_token(self) -> str:
        try:
            data = _with_retry("获取 access_token", lambda: api(
                "POST", "/api/v1/access_token",
                body={"clientID": self._client_id,
                      "clientSecret": self._client_secret})) or {}
        except (SyncError, ApiError) as exc:
            raise SyncError(f"获取 access_token 失败：{exc}"
                            f"{_token_limit_hint(str(exc))}") from None
        token = data.get("accessToken") or ""
        if not token:
            raise SyncError("access_token 响应缺少 accessToken 字段")
        log(f"access_token 获取成功（有效期至 {data.get('expiredAt')}）")
        return token

    def refresh_token(self) -> None:
        self.token = self._fetch_token()

    def call(self, method: str, path: str, *, body: dict | None = None,
             query: dict | None = None, base: str = API_BASE,
             timeout: float = REQUEST_TIMEOUT_S):
        def attempt():
            return api(method, path, body=body, query=query, token=self.token,
                       base=base, timeout=timeout)
        try:
            return _with_retry(f"接口 {path}", attempt)
        except ApiError as exc:
            if exc.code != 401:
                raise
            log("token 已失效（401），重新获取后重试一次")
            self.refresh_token()
            return _with_retry(f"接口 {path}", attempt)


# ---------------------------------------------------------------------------
# 纯函数与上传原语
# ---------------------------------------------------------------------------

def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _multipart_body(fields: dict, files: dict):
    """构造 multipart/form-data 请求体（产物量级 ~10MB，整体进内存）。"""
    boundary = "----ScriptCueSync" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            .encode("utf-8"))
    for name, (filename, content) in files.items():
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _search_all(client: Client, keyword: str) -> list[dict]:
    """按关键字全盘搜索并翻页收全（searchData 无视目录参数；lastFileId=-1 为末页）。"""
    entries: list[dict] = []
    last_file_id = None
    for _ in range(10):          # 防御性上限：最多 10 页（1000 条）
        query = {"parentFileId": 0, "limit": 100, "searchData": keyword}
        if last_file_id is not None:
            query["lastFileId"] = last_file_id
        data = client.call("GET", "/api/v2/file/list", query=query) or {}
        entries.extend(data.get("fileList") or [])
        last_file_id = data.get("lastFileId")
        if last_file_id in (None, -1):
            break
    return entries


def resolve_parent_id(client: Client, folder_name: str) -> int:
    """定位目标目录 fileID：PAN123_PARENT_FILE_ID 优先，否则按名称全盘搜索。"""
    pinned = os.environ.get("PAN123_PARENT_FILE_ID", "").strip()
    if pinned:
        try:
            parent_id = int(pinned)
        except ValueError:
            raise SyncError(f"PAN123_PARENT_FILE_ID 不是合法数字：{pinned!r}") from None
        log(f"使用 PAN123_PARENT_FILE_ID 指定的目标目录 fileID={parent_id}")
        return parent_id
    hits = [f for f in _search_all(client, folder_name)
            if f.get("type") == 1 and f.get("trashed") in (0, None)
            and f.get("filename") == folder_name]
    if not hits:
        raise SyncError(f"未找到名为「{folder_name}」的目录：请先在直链空间创建该目录，"
                        "或用环境变量 PAN123_PARENT_FILE_ID 指定目标目录 fileID")
    if len(hits) > 1:
        detail = ", ".join(f"{f.get('filename')}({f.get('fileId')})" for f in hits)
        raise SyncError(f"按名称搜索「{folder_name}」匹配到 {len(hits)} 个同名目录"
                        f"（{detail}），请用环境变量 PAN123_PARENT_FILE_ID 显式指定")
    parent_id = int(hits[0]["fileId"])
    log(f"目标目录「{folder_name}」fileID={parent_id}")
    return parent_id


def get_upload_domain(client: Client) -> str:
    domains = client.call("GET", "/upload/v2/file/domain")
    if not isinstance(domains, list) or not domains:
        raise SyncError("获取上传域名失败：返回为空")
    return str(domains[0])


def upload_single(client: Client, domain: str, parent_id: int, path: Path) -> dict:
    """单步上传（≤1GB）+ duplicate=2 原地覆盖；返回响应 data。"""
    if path.stat().st_size > MAX_SINGLE_UPLOAD_BYTES:
        raise SyncError(f"{path.name} 超过单步上传 1GB 上限，需改用分片上传（未实现）")
    content = path.read_bytes()
    content_type, body = _multipart_body(
        {
            "parentFileID": str(parent_id),
            "filename": path.name,
            "etag": hashlib.md5(content).hexdigest(),
            "size": str(len(content)),
            "duplicate": "2",
        },
        {"file": (path.name, content)},
    )
    url = domain.rstrip("/") + "/upload/v2/file/single/create"

    def attempt():
        headers = {"Authorization": f"Bearer {client.token}",
                   "Platform": PLATFORM_HEADER,
                   "Content-Type": content_type,
                   "User-Agent": "ScriptCueReleaseSync/1.0"}
        status, raw = _raw_request("POST", url, data=body, headers=headers,
                                   timeout=UPLOAD_TIMEOUT_S)
        return _json_data(status, raw)

    try:
        return _with_retry(f"上传 {path.name}", attempt) or {}
    except ApiError as exc:
        if exc.code != 401:
            raise
        log("token 已失效（401），重新获取后重试上传")
        client.refresh_token()
        return _with_retry(f"上传 {path.name}", attempt) or {}


def _fetch_json_url(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "ScriptCueReleaseSync/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def _remote_size(url: str) -> int | None:
    """探测直链产物大小：HEAD 优先，不支持时退回 GET 只读响应头；失败返回 None。"""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(
                url, method=method,
                headers={"User-Agent": "ScriptCueReleaseSync/1.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                length = resp.headers.get("Content-Length")
                return int(length) if length and length.isdigit() else None
        except Exception:  # noqa: BLE001 - 探测失败不参与判定
            continue
    return None


def _manifest_mismatch(data: dict, expect_version: str,
                       expect_released_at: str | None,
                       base_url: str) -> str | None:
    """返回"远端清单/产物与本次发布不一致"的原因；None 表示校验通过。

    判据三层：version（跨版本）→ released_at（区分同版本的旧构建）→
    最小产物的直链大小（识别"清单已新、产物仍是旧内容"）。
    """
    got_version = data.get("version")
    if got_version != expect_version:
        return f"CDN 清单 version={got_version}，期望 {expect_version}"
    if expect_released_at and data.get("released_at") != expect_released_at:
        return (f"CDN 清单 released_at={data.get('released_at')}，"
                f"期望 {expect_released_at}（可能是上一轮生成的同版本清单）")
    sizes = {}
    for asset in (data.get("assets") or {}).values():
        name = str(asset.get("asset") or "")
        if name and isinstance(asset.get("size"), int):
            sizes[name] = asset["size"]
    if sizes:
        name, size = min(sizes.items(), key=lambda kv: kv[1])
        remote = _remote_size(base_url.rstrip("/") + "/" + name)
        if remote is not None and remote != size:
            return f"产物 {name} 直链大小 {remote} 与清单 {size} 不一致"
    return None


def verify_manifest(base_url: str, expect_version: str,
                    expect_released_at: str | None = None) -> None:
    """回读 CDN 清单并抽查产物（权威判据）；连续不一致时抛 SyncError。"""
    url = base_url.rstrip("/") + "/update-manifest.json"
    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        try:
            data = _fetch_json_url(url)
        except Exception as exc:  # noqa: BLE001 - 传播窗口内可能读取失败 / 404
            log(f"回读校验第 {attempt} 轮读取失败：{exc}")
        else:
            reason = _manifest_mismatch(data, expect_version,
                                        expect_released_at, base_url)
            if reason is None:
                log(f"回读校验通过：CDN 清单 version={expect_version}（第 {attempt} 轮）")
                return
            log(f"回读校验第 {attempt} 轮未通过：{reason}，等待传播")
        if attempt < VERIFY_ATTEMPTS:
            time.sleep(VERIFY_INTERVAL_S)
    raise SyncError(f"回读校验未通过：{url} 在 {VERIFY_ATTEMPTS} 轮内未与本次发布一致"
                    f"（version={expect_version}）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="把发布产物与更新清单同步到 123云盘直链空间"
                    "（见 docs/auto-update.md §3.1）")
    parser.add_argument("--dist", default="dist",
                        help="产物目录（含 update-manifest.json），默认 dist")
    parser.add_argument("--base-url",
                        default=os.environ.get("SC_CDN_BASE_URL") or DEFAULT_BASE_URL,
                        help=f"CDN 直链根地址（回读校验用），默认 {DEFAULT_BASE_URL}")
    parser.add_argument("--folder-name", default=DEFAULT_FOLDER_NAME,
                        help=f"目标目录名（按名称搜索定位 fileID），"
                             f"默认 {DEFAULT_FOLDER_NAME}")
    parser.add_argument("--expect-version", default=None,
                        help="期望版本号（如 1.1.2）；缺省取清单内 version")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)
    client_id = os.environ.get("PAN123_CLIENT_ID", "").strip()
    client_secret = os.environ.get("PAN123_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SyncError("缺少环境变量 PAN123_CLIENT_ID / PAN123_CLIENT_SECRET")

    dist = Path(args.dist)
    manifest_path = dist / "update-manifest.json"
    if not manifest_path.is_file():
        raise SyncError(f"未找到清单文件：{manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    version = str(manifest.get("version") or "")
    if not version:
        raise SyncError("清单缺少 version 字段")
    if args.expect_version and args.expect_version != version:
        raise SyncError(f"清单 version={version} 与期望 {args.expect_version} 不一致")

    # 1) 产物与清单严格同批（size + sha256）
    asset_paths: list[Path] = []
    for key, asset in (manifest.get("assets") or {}).items():
        name = str(asset.get("asset") or "")
        path = dist / name
        if not name or not path.is_file():
            raise SyncError(f"缺少清单引用的产物：{name or key}")
        if path.stat().st_size != asset.get("size"):
            raise SyncError(f"产物 {name} 大小与清单不一致")
        digest = _sha256_of(path)
        if digest != asset.get("sha256"):
            raise SyncError(f"产物 {name} sha256 与清单不一致（实际 {digest[:12]}…）")
        asset_paths.append(path)
    if not asset_paths:
        raise SyncError("清单 assets 为空：拒绝只上传一份没有产物的清单")
    log(f"入参校验通过：v{version}，待上传产物 {len(asset_paths)} 个 + 清单 1 份")

    # 2) 鉴权 + 定位目标目录与上传域名
    client = Client(client_id, client_secret)
    parent_id = resolve_parent_id(client, args.folder_name)
    domain = get_upload_domain(client)
    log(f"上传域名：{domain}")

    # 3) 产物先行、清单最后（避免清单指向从未上传过的文件名；同名覆盖的短暂窗口见模块说明）
    for path in asset_paths:
        started = time.monotonic()
        result = upload_single(client, domain, parent_id, path)
        if not result.get("completed"):
            raise SyncError(f"{path.name} 上传未完成（completed={result.get('completed')}）")
        log(f"已上传 {path.name}（{path.stat().st_size} 字节，"
            f"fileID={result.get('fileID')}，{time.monotonic() - started:.1f}s）")
    result = upload_single(client, domain, parent_id, manifest_path)
    if not result.get("completed"):
        raise SyncError(f"清单上传未完成（completed={result.get('completed')}）")
    log(f"已上传 update-manifest.json（fileID={result.get('fileID')}）")

    # 4) 缓存刷新（best-effort）与回读校验（权威判据）
    try:
        client.call("POST", "/api/v1/direct-link/cache/refresh", body={})
        log("直链缓存刷新已提交")
    except Exception as exc:  # noqa: BLE001 - 实测覆盖即时生效，刷新失败仅告警
        annotate("warning", f"直链缓存刷新失败（不影响发布判定，以回读校验为准）：{exc}")
    verify_manifest(args.base_url, version, manifest.get("released_at"))
    log("CDN 同步完成")


if __name__ == "__main__":
    try:
        main()
    except SyncError as exc:
        annotate("error", f"CDN 同步失败：{exc}")
        raise SystemExit(1)
    except ApiError as exc:
        annotate("error", f"CDN 同步接口错误：{exc}")
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - 兜底：保证总有 ::error:: 注解
        annotate("error", f"CDN 同步异常终止：{type(exc).__name__}: {exc}")
        raise SystemExit(1)
