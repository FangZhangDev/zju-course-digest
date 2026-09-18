"""zju_console.py — 控制台编码兼容（Windows / 各类 AI Agent CLI 环境下避免 UnicodeEncodeError）"""

from __future__ import annotations

import os
import sys


def ensure_utf8_io() -> None:
    """确保 stdout/stderr 使用 UTF-8 输出，避免打印中文时 UnicodeEncodeError。"""
    try:
        enc = (sys.stdout.encoding or "").lower()
        if enc in ("utf-8", "utf8"):
            return
    except Exception:
        pass
    try:
        import io

        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
        if hasattr(sys.stderr, "buffer"):
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", errors="replace"
            )
    except Exception:
        pass


def ensure_direct_network() -> list[str]:
    """直连模式下清理环境代理变量，返回被清理的变量名列表。

    部分 Host（IDE / Agent 运行时 / 企业代理）会注入 HTTP_PROXY / HTTPS_PROXY，
    而 httpx 与 urllib 默认都会读取。浙大服务在校园网内是直连的，
    被代理后会 ConnectError。CLI 入口调用本函数统一兜底。

    各 API 客户端内部另有 trust_env=False / ProxyHandler({}) 双保险。

    如确需保留环境代理（极少见），设 ZJU_USE_ENV_PROXY=1。
    """
    if os.environ.get("ZJU_USE_ENV_PROXY") == "1":
        return []
    removed = []
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "http_proxy", "https_proxy", "all_proxy"):
        if os.environ.pop(k, None) is not None:
            removed.append(k)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    return removed


def env_proxy_disabled() -> bool:
    """是否应忽略环境代理（供各客户端统一判断）。"""
    return os.environ.get("ZJU_USE_ENV_PROXY") != "1"
