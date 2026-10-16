"""更新清单源：从 CDN 对象存储拉取 update-manifest.json，缓存后供下发。

数据流：CDN（维护者手动同步）→ 本模块（启动即刷 + 定时刷新 + 落盘缓存）
→ 被控端（GET /update/manifest.json 轮询，或经 ws 收到 update.available 推送）。

设计约束：
- 纯标准库实现（urllib），不新增依赖；
- 环境变量：
    SC_UPDATE_MANIFEST_URL       CDN 清单地址（如 https://cdn.example.com/app/update-manifest.json）
    SC_UPDATE_MANIFEST_FILE      本地清单文件（存在且可解析时完全接管并停用 CDN 刷新，用于钉版本）
    SC_UPDATE_ASSET_BASE_URL     产物 CDN 根地址（如 https://cdn.example.com/app/），下发时给每个资产注入绝对 url
    SC_UPDATE_DISABLED=1         整体关闭更新通道（不拉取、不下发、/update/manifest.json 返回 404）
- 覆盖优先级：SC_UPDATE_MANIFEST_FILE > 内存/磁盘缓存 > CDN；CDN 挂掉不影响已有缓存继续服务；
- 全程失败只记日志（logger.warning），绝不向请求路径抛异常。

改动清单 schema 时必须同步：docs/auto-update.md、.github/workflows/release.yml 的
清单生成步骤、被控端 agent/agent/updater.py 的校验逻辑。
"""

import copy
import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path

from .timebase import now_ms

logger = logging.getLogger("scriptcue.update")

# 清单拉取超时（秒）与体积上限（防御性：清单应远小于此）
FETCH_TIMEOUT_S = 10
MAX_MANIFEST_BYTES = 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_SCHEMA = 1


def validate_manifest(m: dict) -> dict:
    """校验清单结构（schema/version/assets），非法时抛 ValueError。

    只做结构校验，不判断版本新旧、不检查资产文件是否存在。
    """
    if not isinstance(m, dict):
        raise ValueError("清单必须是 JSON 对象")
    if m.get("schema") != _SUPPORTED_SCHEMA:
        raise ValueError(f"不支持的清单 schema: {m.get('schema')!r}")
    version = m.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("清单缺少 version")
    assets = m.get("assets")
    if not isinstance(assets, dict) or not assets:
        raise ValueError("清单缺少 assets")
    for key, asset in assets.items():
        if not isinstance(asset, dict):
            raise ValueError(f"资产 {key} 不是对象")
        if not isinstance(asset.get("asset"), str) or not asset["asset"]:
            raise ValueError(f"资产 {key} 缺少 asset 文件名")
        if not _SHA256_RE.fullmatch(str(asset.get("sha256", ""))):
            raise ValueError(f"资产 {key} 的 sha256 非法")
        if not isinstance(asset.get("size"), int) or asset["size"] <= 0:
            raise ValueError(f"资产 {key} 的 size 非法")
    return m


def ver_tuple(s: str | None) -> tuple[int, ...] | None:
    """把 "1.0.3" 解析为 (1, 0, 3)；含非数字段返回 None（视为开发版）。"""
    if not isinstance(s, str) or not s.strip():
        return None
    parts = s.strip().split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _http_get_json(url: str, timeout: float = FETCH_TIMEOUT_S) -> dict:
    """GET 一个 JSON 对象；失败抛异常由调用方降级。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "ScriptCueServer/update-feed",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("清单体积超过上限")
    return json.loads(raw.decode("utf-8"))


class UpdateFeed:
    """更新清单的拉取、缓存与下发。线程内使用（由调用方放 asyncio.to_thread）。"""

    def __init__(self, cache_path: Path, *,
                 manifest_url: str | None = None,
                 override_file: str | None = None,
                 asset_base_url: str | None = None,
                 disabled: bool | None = None):
        """参数默认读环境变量；显式传参供测试注入。"""
        env = os.environ.get
        self.manifest_url = (manifest_url if manifest_url is not None
                             else env("SC_UPDATE_MANIFEST_URL", "")).strip()
        self.override_file = (override_file if override_file is not None
                              else env("SC_UPDATE_MANIFEST_FILE", "")).strip()
        self.asset_base_url = (asset_base_url if asset_base_url is not None
                               else env("SC_UPDATE_ASSET_BASE_URL", "")).strip()
        if disabled is None:
            disabled = env("SC_UPDATE_DISABLED", "") == "1"
        self.disabled = bool(disabled)

        self._cache_path = Path(cache_path)
        self._manifest: dict | None = None
        self._source = "none"          # file | cdn | cache | none
        self._fetched_at_s: float | None = None
        self._load_cache()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def enabled(self) -> bool:
        """是否启用更新通道（无任何清单来源或显式关闭时为 False）。"""
        return not self.disabled and bool(self.override_file or self.manifest_url)

    def refresh(self) -> dict:
        """同步拉取/复核清单。返回 {ok, source, version}，供调用方记审计。

        覆盖文件优先且完全接管（停用 CDN）；无文件时走 CDN，失败保留旧缓存。
        """
        if self.disabled:
            return self._result(True)
        # 1) 覆盖文件（钉版本）：存在且可解析则完全接管，不再访问 CDN
        if self.override_file:
            m = self._load_override_file()
            if m is not None:
                self._apply(m, "file", persist=False)
                return self._result(True)
        # 2) CDN：成功则写内存 + 落盘缓存；失败保留旧缓存（ok=False 仅表示本次拉取失败）
        if self.manifest_url:
            try:
                m = _http_get_json(self.manifest_url)
                m = validate_manifest(m)
            except Exception as exc:  # noqa: BLE001 - 更新通道的任何失败都不得外传
                logger.warning("拉取更新清单失败（%s）: %s", self.manifest_url, exc)
                return self._result(False)
            self._apply(m, "cdn", persist=True)
            return self._result(True)
        # 既无文件也无 URL：保持现状（缓存/无清单）
        return self._result(self._manifest is not None)

    def serve(self) -> dict | None:
        """返回下发用清单副本：给资产注入绝对 url 并附加 server_time。无清单时 None。"""
        if self._manifest is None:
            return None
        served = copy.deepcopy(self._manifest)
        for asset in served.get("assets", {}).values():
            if not isinstance(asset.get("url"), str) or not asset["url"].startswith(("http://", "https://")):
                url = self._resolve_asset_url(asset.get("asset", ""))
                if url:
                    asset["url"] = url
        served["server_time"] = now_ms()
        return served

    def latest_version(self) -> str | None:
        if self._manifest is None:
            return None
        return self._manifest.get("version")

    def is_behind(self, build_version: str | None) -> bool:
        """build_version 是否落后于当前清单版本；任一版本无法解析时为 False。"""
        latest = ver_tuple(self.latest_version())
        current = ver_tuple(build_version)
        if latest is None or current is None:
            return False
        return current < latest

    def status(self) -> dict:
        """供 /healthz 展示：{version, source, age_s}。"""
        age_s = None
        if self._fetched_at_s is not None:
            age_s = round(time.monotonic() - self._fetched_at_s, 1)
        return {"version": self.latest_version(), "source": self._source, "age_s": age_s}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _result(self, ok: bool) -> dict:
        return {"ok": ok, "source": self._source, "version": self.latest_version()}

    def _apply(self, m: dict, source: str, persist: bool) -> bool:
        """校验并替换当前清单；非法时保留旧值并返回 False。"""
        try:
            validated = validate_manifest(m)
        except ValueError as exc:
            logger.warning("更新清单无效（来源 %s）: %s", source, exc)
            return False
        self._manifest = validated
        self._source = source
        self._fetched_at_s = time.monotonic()
        logger.info("更新清单已更新: v%s（来源 %s）", validated.get("version"), source)
        if persist:
            self._persist_cache()
        return True

    def _load_override_file(self) -> dict | None:
        # utf-8-sig：兼容 Windows 编辑器/PowerShell 写出的带 BOM 文件（手工放清单的常见情形）
        path = Path(self.override_file)
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取覆盖清单失败（%s）: %s", path, exc)
            return None

    def _load_cache(self) -> None:
        if self.disabled:
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取更新清单缓存失败（%s）: %s", self._cache_path, exc)
            return
        if self._apply(data, "cache", persist=False):
            logger.info("已从磁盘缓存加载更新清单: v%s", data.get("version"))

    def _persist_cache(self) -> None:
        """原子写缓存（tmp + replace），供重启后 CDN 不可用时继续服务。"""
        tmp = self._cache_path.with_name(self._cache_path.name + ".tmp")
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._manifest, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self._cache_path)
        except OSError as exc:
            logger.warning("写入更新清单缓存失败: %s", exc)

    def _resolve_asset_url(self, asset_name: str) -> str:
        """资产绝对地址：SC_UPDATE_ASSET_BASE_URL 优先，否则用清单 URL 同目录兜底。"""
        if not asset_name:
            return ""
        if self.asset_base_url:
            return self.asset_base_url.rstrip("/") + "/" + asset_name
        if self.manifest_url:
            base = self.manifest_url.rsplit("/", 1)[0]
            return base + "/" + asset_name
        return ""
