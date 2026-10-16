"""共享 TLS 信任链构造：系统证书库 ∪ certifi 根证书（加法）。

wss 长连接（engine.py）与更新下载请求（updater.py）共用本模块，保证两条
通道走同一套信任链——macOS 上尤其关键：Python 的 OpenSSL 不读系统钥匙串，
系统库常为空，靠 certifi 补齐，否则对任何 https/wss 都会报
"unable to get local issuer certificate"。
"""

import logging
import ssl

try:
    import certifi
except ImportError:  # pragma: no cover - certifi 应随依赖一并安装
    certifi = None

logger = logging.getLogger("scriptcue.agent")


def build_ssl_context() -> ssl.SSLContext:
    """构造 TLS 上下文：create_default_context() 后追加 certifi 根证书。

    - Windows：系统证书库已加载，再追加 certifi 只增不减，现有可连的服务器不会退化；
    - macOS：靠 certifi 补齐系统库缺失的公共根证书。
    """
    ctx = ssl.create_default_context()
    if certifi is None:
        logger.warning("SSL: certifi 不可用，仅使用系统证书库")
        return ctx
    try:
        ctx.load_verify_locations(certifi.where())
        logger.info("SSL: certifi CA 包已加载")
    except OSError:  # certifi 包缺失/损坏时退回系统库，至少不影响 Windows
        logger.warning("SSL: 无法加载 certifi CA 包，仅使用系统证书库")
    return ctx
