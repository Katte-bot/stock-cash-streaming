"""DeepSeek 网页搜索客户端（Search API）。

用途：为 AI 日报提供「消息面参考」的定性材料。
边界：只读、只做展示；不写入数据库，不参与因子、信号、组合与回测，
因为搜索引擎返回的是当前网页状态，无法还原历史某日的信息集。

文档：https://api-docs.deepseek.com/zh-cn/guides/search/
"""

import re
import time

import httpx

from ..common.logging import logger
from ..config.settings import get_settings


def _clean(text: str) -> str:
    """压成单行并去掉 Markdown 标记，便于日报纯文本展示。"""
    return re.sub(r"\s+", " ", re.sub(r"[#*`>|]", "", text or "")).strip()


def _endpoint(base_url: str, path: str) -> str:
    """拼接 /v1/tools/<path>；兼容 base_url 已带 /v1 的写法。"""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/v1/tools/{path}"


def _retryable(err: Exception) -> bool:
    """仅对超时、429 与 5xx 重试；4xx（除 429）重试无意义。"""
    if isinstance(err, httpx.HTTPStatusError):
        code = err.response.status_code
        return code in (408, 429) or code >= 500
    return isinstance(err, (httpx.TimeoutException, httpx.TransportError))


def search(query: str, count: int | None = None, need_content: bool = False) -> list[dict]:
    """调用 DeepSeek Search，返回规范化结果；失败或无结果时返回空列表。"""
    cfg = get_settings()
    if not cfg.search_enabled or not cfg.openai_api_key:
        return []

    payload = {
        "query": query,
        "count": count or cfg.search_count,
        "need_content": need_content,
    }
    last_err: Exception | None = None
    for i in range(max(1, cfg.max_retries)):
        try:
            resp = httpx.post(
                _endpoint(cfg.openai_base_url, "search"),
                headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
                json=payload,
                timeout=cfg.search_timeout,
            )
            resp.raise_for_status()
            raw = resp.json().get("results") or []
            return [
                {
                    "title": _clean(x.get("title") or ""),
                    "url": x.get("url") or "",
                    # 摘要常含 Markdown 标题与换行，压成单行便于日报展示
                    "summary": _clean(x.get("summary") or x.get("snippet") or ""),
                    "date": (x.get("date") or "")[:10],
                    "site": x.get("site_name") or "",
                    "score": x.get("rerank_score"),
                }
                for x in raw
                if x.get("url")
            ]
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(f"DeepSeek 搜索失败({query}) 第{i + 1}/{cfg.max_retries}次: "
                           f"{type(e).__name__}: {e}")
            if not _retryable(e):
                break
            time.sleep(1.0 * (i + 1))
    logger.warning(f"DeepSeek 搜索放弃({query}): {last_err}")
    return []


__all__ = ["search"]
