"""利润表关键科目回填（全 A，按报告期批量）。

数据源：东方财富「业绩报表」`stock_yjbb_em`，一次请求返回一个报告期全部股票，
避免逐票抓取（5,000+ 只 × 3 张报表的网络成本）。

入库指标（`src='em:yjbb'`）：
- `revenue`（营业总收入）、`net_profit`（净利润，接近归母口径）、`eps`（基本每股收益）、
  `bps`（每股净资产）、`roe`（加权 ROE，百分数）、`ocf_ps`（每股经营现金流量）。

时点处理：**不使用**接口返回的「最新公告日期」（实测为数据刷新/后续定期报告重述的日期，
普遍比真实首次披露晚约一年，会造成数据严重滞后），统一用法定披露截止日
`_deadline(stat_date)` 兜底（严格不早于真实公告日，避免前视）。

说明：`ocf_ps`/`bps` 用于在因子层派生经营现金流与股本（见 `features/fundamentals.py`
的 `_report_panel`），因此不单独落 `ocf`，避免覆盖已有逐票现金流量表口径。
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from ..common.logging import logger
from ..storage.repository import Repository
from .fundamentals import _deadline, _to_float

# 业绩报表列名 -> fact_financial.metric
METRIC_MAP = {
    "营业总收入-营业总收入": "revenue",
    "净利润-净利润": "net_profit",
    "每股收益": "eps",
    "每股净资产": "bps",
    "每股经营现金流量": "ocf_ps",
    "净资产收益率": "roe",
}

SUPPORTED_FIRST_YEAR = 2015
MAX_RETRY = 4


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def quarter_ends(start_year: int = SUPPORTED_FIRST_YEAR, end: str | None = None) -> list[str]:
    """报告期列表（YYYY-MM-DD），默认从 2015 年至今的已完成季度。"""
    today = datetime.now().strftime("%Y-%m-%d")
    end = end or today
    out: list[str] = []
    for y in range(start_year, datetime.now().year + 1):
        for mmdd in ("03-31", "06-30", "09-30", "12-31"):
            d = f"{y}-{mmdd}"
            if d <= end:
                out.append(d)
    return out


def _fetch(period: str) -> pd.DataFrame:
    import akshare as ak

    compact = period.replace("-", "")
    last_err = None
    for attempt in range(MAX_RETRY):
        try:
            df = ak.stock_yjbb_em(date=compact)
            if df is not None:
                return df
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"业绩报表请求失败 {period}: {last_err}")


def _rows_from(df: pd.DataFrame, period: str, symbols: set[str]) -> list[dict]:
    """宽表 -> 长表（只保留本库已收录的 A 股代码）。"""
    if df is None or df.empty or "股票代码" not in df.columns:
        return []
    now = _now()
    out: list[dict] = []
    for rec in df.to_dict("records"):
        code = str(rec.get("股票代码") or "").strip().zfill(6)
        if code not in symbols:
            continue
        pub = _deadline(period)
        for col, metric in METRIC_MAP.items():
            val = _to_float(rec.get(col))
            if val is None:
                continue
            out.append({"symbol": code, "stat_date": period, "pub_date": pub,
                        "metric": metric, "value": val,
                        "src": "em:yjbb", "fetched_at": now})
    return out


def sync_income_reports(repo: Repository, start_year: int = SUPPORTED_FIRST_YEAR,
                        end: str | None = None, force: bool = False,
                        sleep: float = 1.0, workers: int = 4) -> dict:
    """按报告期回填利润表关键科目（幂等，可中断续跑）。

    并发：线程只负责网络抓取（每个报告期一次请求），SQLite 写入统一在主线程完成。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .net import install_default_timeout

    install_default_timeout(90)

    symbols = {r["symbol"] for r in repo.query("SELECT symbol FROM dim_stock")}
    periods = quarter_ends(start_year, end)
    stats = {"periods": len(periods), "ok": 0, "fail": 0, "rows": 0, "skipped": 0}
    started = time.time()
    today = datetime.now().strftime("%Y-%m-%d")

    pending: list[str] = []
    for period in periods:
        if not force:
            wm = repo.get_watermark(f"yjbb:{period}")
            if wm and wm["last_run_at"] and str(wm["last_run_at"])[:10] >= today:
                stats["skipped"] += 1
                continue
        pending.append(period)

    def _one(period: str):
        try:
            return period, _fetch(period), None
        except RuntimeError as e:  # noqa: PERF203
            return period, None, str(e)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(_one, p) for p in pending]
        for i, fut in enumerate(as_completed(futures), 1):
            period, df, err = fut.result()
            if err is not None:
                stats["fail"] += 1
                logger.warning(err)
                repo.set_watermark(f"yjbb:{period}", None, "fail")
            else:
                rows = _rows_from(df, period, symbols)
                if rows:
                    repo.upsert("fact_financial", rows, ["symbol", "stat_date", "metric"])
                    stats["ok"] += 1
                    stats["rows"] += len(rows)
                    repo.set_watermark(f"yjbb:{period}", period)
                else:
                    stats["fail"] += 1
                    repo.set_watermark(f"yjbb:{period}", None, "empty")
            logger.info(
                f"  业绩报表 {i}/{len(pending)} {period} | ok={stats['ok']} fail={stats['fail']} "
                f"rows={stats['rows']} | {time.time() - started:.0f}s"
            )
            if sleep:
                time.sleep(sleep)
    return stats
