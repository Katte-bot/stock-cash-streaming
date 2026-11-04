"""财务报表关键科目入库（现金流量表 / 资产负债表）。

数据源：新浪财务报表（`stock_financial_report_sina`），一次请求返回全部报告期。
时点处理：
- `stat_date` 取「报告日」；
- `pub_date` 优先取「公告日期」，缺失时用法定披露截止日兜底（不早于真实公告日）。

只补充新指标（不覆盖既有财务摘要口径）：
- 现金流量表：`capex`（购建固定资产、无形资产和其他长期资产所支付的现金）、
  `dividends_paid`（分配股利、利润或偿付利息所支付的现金）；
- 资产负债表：`total_assets`、`total_liabilities`、`short_debt`、`long_debt`、`bond_payable`。
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from ..common.logging import logger
from ..datasource.akshare_src import infer_market
from ..storage.repository import Repository
from .fundamentals import _deadline

CF_MAP = {
    "购建固定资产、无形资产和其他长期资产所支付的现金": "capex",
    "分配股利、利润或偿付利息所支付的现金": "dividends_paid",
}
BS_MAP = {
    "资产总计": "total_assets",
    "负债合计": "total_liabilities",
    "短期借款": "short_debt",
    "长期借款": "long_debt",
    "应付债券": "bond_payable",
}

DEFAULT_SLEEP = 0.25
MAX_RETRY = 3


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_float(x) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "--", "-", "None", "nan", "NaN"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date8(x) -> str | None:
    s = str(x).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) == 10 and s[4] == "-":
        return s
    return None


def _fetch(symbol: str, kind: str) -> pd.DataFrame:
    import akshare as ak

    stock = f"{infer_market(symbol)}{symbol}"
    last_err = None
    for attempt in range(MAX_RETRY):
        try:
            df = ak.stock_financial_report_sina(stock=stock, symbol=kind)
            return df if df is not None else pd.DataFrame()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{kind} 请求失败 {symbol}: {last_err}")


def _latest_report(repo: Repository, symbol: str) -> str | None:
    rows = repo.query(
        "SELECT MAX(stat_date) AS d FROM fact_financial WHERE symbol=? AND metric='total_assets'",
        (symbol,),
    )
    return rows[0]["d"] if rows else None


def _rows_from(df: pd.DataFrame, mapping: dict[str, str], symbol: str) -> list[dict]:
    out: list[dict] = []
    if df is None or df.empty or "报告日" not in df.columns:
        return out
    now = _now()
    for rec in df.to_dict("records"):
        stat = _date8(rec.get("报告日"))
        if not stat:
            continue
        pub = _date8(rec.get("公告日期")) or _deadline(stat)
        for col, metric in mapping.items():
            if col not in rec:
                continue
            val = _to_float(rec.get(col))
            if val is None:
                continue
            out.append({"symbol": symbol, "stat_date": stat, "pub_date": pub,
                        "metric": metric, "value": val,
                        "src": "sina:statement", "fetched_at": now})
    return out


def _latest_quarter_end() -> str:
    today = datetime.now()
    m = today.month
    q = ((m - 1) // 3) * 3
    if q == 0:
        return f"{today.year - 1}-12-31"
    day = {3: 31, 6: 30, 9: 30, 12: 31}[q]
    return f"{today.year}-{q:02d}-{day:02d}"


def sync_statements(repo: Repository, symbols: list[str] | None = None,
                    limit: int | None = None, sleep: float = DEFAULT_SLEEP,
                    only_missing: bool = False, workers: int = 4) -> dict:
    """按股票回填现金流量表/资产负债表关键科目。

    并发：多线程只负责网络抓取，SQLite 写入统一在主线程完成。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .net import install_default_timeout

    install_default_timeout(60)

    if symbols is None:
        symbols = [r["symbol"] for r in repo.query("SELECT symbol FROM dim_stock ORDER BY symbol")]
    if only_missing:
        symbols = [s for s in symbols if _latest_report(repo, s) is None]
    if limit:
        symbols = symbols[:limit]

    latest_q = _latest_quarter_end()
    stats = {"total": len(symbols), "ok": 0, "fail": 0, "rows": 0, "skipped": 0}
    started = time.time()
    today = datetime.now().strftime("%Y-%m-%d")

    pending: list[str] = []
    for sym in symbols:
        wm = repo.get_watermark(f"stmt:{sym}")
        last = str(wm["last_date"]) if wm and wm["last_date"] else None
        fetched_at = str(wm["last_run_at"]) if wm and wm["last_run_at"] else ""
        # 最新报告期已入库且当天拉取过 → 跳过（避免重复请求）
        if last and last >= latest_q and fetched_at >= today:
            stats["skipped"] += 1
            continue
        pending.append(sym)

    def _fetch_one(sym: str):
        try:
            return sym, (_rows_from(_fetch(sym, "现金流量表"), CF_MAP, sym)
                         + _rows_from(_fetch(sym, "资产负债表"), BS_MAP, sym)), None
        except Exception as e:  # noqa: BLE001
            return sym, None, str(e)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(_fetch_one, sym) for sym in pending]
        for i, fut in enumerate(as_completed(futures), 1):
            sym, rows, err = fut.result()
            if err is not None:
                stats["fail"] += 1
                logger.warning(f"财报回填失败 {sym}: {err}")
                repo.set_watermark(f"stmt:{sym}", None, "fail")
            elif not rows:
                repo.set_watermark(f"stmt:{sym}", None, "empty")
            else:
                repo.upsert("fact_financial", rows, ["symbol", "stat_date", "metric"])
                stats["ok"] += 1
                stats["rows"] += len(rows)
                repo.set_watermark(f"stmt:{sym}", max(r["stat_date"] for r in rows))
            if i % 50 == 0 or i == len(pending):
                logger.info(
                    f"  财报复权 {i}/{len(pending)} | ok={stats['ok']} fail={stats['fail']} "
                    f"rows={stats['rows']} | {time.time() - started:.0f}s"
                )
    repo.set_watermark("fact_financial_statement", datetime.now().strftime("%Y-%m-%d"))
    return stats


def coverage(repo: Repository) -> dict:
    total = repo.count("dim_stock")
    covered = repo.query(
        "SELECT COUNT(DISTINCT symbol) AS n FROM fact_financial WHERE metric='total_assets'"
    )[0]["n"]
    income = repo.query(
        "SELECT COUNT(DISTINCT symbol) AS n FROM fact_financial WHERE metric='net_profit'"
    )[0]["n"]
    ocf = repo.query(
        "SELECT COUNT(DISTINCT symbol) AS n FROM fact_financial "
        "WHERE metric='ocf' OR metric='ocf_ps'"
    )[0]["n"]
    return {
        "symbols_total": int(total),
        "symbols_covered": int(covered or 0),
        "coverage": round((covered or 0) / total, 4) if total else 0.0,
        "income_covered": int(income or 0),
        "income_coverage": round((income or 0) / total, 4) if total else 0.0,
        "ocf_covered": int(ocf or 0),
    }
