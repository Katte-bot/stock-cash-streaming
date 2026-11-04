"""网络请求兜底超时。

背景：akshare 内部直接调用 `requests.get(...)` 且不传 timeout，数据源偶发
长连接不返回会让多线程回填永久卡在 `as_completed`。这里给
`requests.Session.request` 注入默认超时，进程内幂等。
"""

from __future__ import annotations

import requests

DEFAULT_TIMEOUT = 60
_installed = False


def install_default_timeout(seconds: int = DEFAULT_TIMEOUT) -> None:
    """为未显式指定 timeout 的 requests 请求注入默认超时（只生效一次）。"""
    global _installed
    if _installed:
        return
    original = requests.Session.request

    def _request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = seconds
        return original(self, method, url, **kwargs)

    requests.Session.request = _request  # type: ignore[assignment]
    _installed = True
