"""zju_env.py — 运行环境自适应

浙大各服务（统一认证、教务网、学在浙大、智云课堂）在**校园网内是直连**的，
校外才需要 WebVPN。但很多 Host 环境（IDE / Agent 运行时 / 企业代理）会自动注入
`HTTP_PROXY` / `HTTPS_PROXY` 环境变量，而 httpx 默认 `trust_env=True`，
于是直连请求会被强行送到那个代理上，表现为：

    httpx.ConnectError / httpcore.ConnectError

而实际上校园网直连是通的。这个模块负责在**直连模式**下清理代理干扰，
让 httpx 不去读环境变量里的代理。

注意：**WebVPN 模式不受影响**——WebVPN 走的是 convert_url 后的
`webvpn.zju.edu.cn`，那段代理由 WebVpnSession 自行管理。
"""

from __future__ import annotations

import os

# 会干扰 httpx 的环境变量名（大小写都覆盖，httpx 实际只认小写，但保险起见）
_PROXY_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)

# 浙大域名 —— 走这些域名时不应使用外部代理
ZJU_DOMAINS = (
    "zju.edu.cn",
    "zjuam.zju.edu.cn",
    "zdbk.zju.edu.cn",
    "courses.zju.edu.cn",
    "classroom.zju.edu.cn",
    "cmc.zju.edu.cn",
    "education.cmc.zju.edu.cn",
    "yjapi.cmc.zju.edu.cn",
    "tgmedia.cmc.zju.edu.cn",
    "webvpn.zju.edu.cn",
    "mirrors.zju.edu.cn",
)


def is_zju_url(url: str) -> bool:
    """判断 URL 是否指向浙大域名。"""
    if not url:
        return False
    return any(d in url for d in ZJU_DOMAINS)


def without_proxy_env() -> dict:
    """在直连模式下清理代理环境变量，返回被清掉的键值（便于调试）。

    设 ZJU_USE_ENV_PROXY=1 可跳过（保留环境代理）。
    """
    if os.environ.get("ZJU_USE_ENV_PROXY") == "1":
        return {}
    removed = {}
    for k in _PROXY_VARS:
        v = os.environ.pop(k, None)
        if v is not None:
            removed[k] = v
    # 显式声明不走代理，双保险
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    return removed


def should_ignore_env_proxy() -> bool:
    """是否应忽略环境代理（默认 True）。"""
    return os.environ.get("ZJU_USE_ENV_PROXY") != "1"


def describe_proxy_env() -> dict:
    """返回当前环境中的代理变量（用于 --status / 诊断）。"""
    return {k: os.environ[k] for k in sorted(os.environ)
            if "proxy" in k.lower()}
