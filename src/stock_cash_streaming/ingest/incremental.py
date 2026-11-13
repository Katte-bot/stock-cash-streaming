"""每日增量拉取：行情（腾讯 hfq）、个股资金流快照、板块资金流（同花顺）。"""

import math
import os
import threading
from datetime import datetime, timedelta
from functools import lru_cache

import pandas as pd

from ..common.logging import logger
from ..datasource.base import DataSource
from ..storage.repository import Repository
from .universe import load_sectors, load_seed_members


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _with_timeout(fn, timeout: int, default):
    """在守护线程里执行数据源调用，超时直接放弃。

    腾讯全市场快照/同花顺板块接口偶发长连接不返回（requests 的 timeout 只覆盖
    连接与读取间隙），曾让每日流水线在最后一步挂住半小时。资金流快照只是辅助
    数据，宁可跳过也不要阻塞整条流水线。
    """
    box: dict = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.warning(f"数据源调用超过 {timeout}s 未返回，跳过本步")
        return default
    if "error" in box:
        raise box["error"]
    return box.get("value", default)


@lru_cache(maxsize=1)
def _trade_calendar() -> frozenset[str]:
    """A 股交易日历（新浪，覆盖 1990–2026 年末）；失败时返回空集。"""
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        return frozenset(df["trade_date"].astype(str))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"交易日历获取失败，回退工作日判断: {e}")
        return frozenset()


@lru_cache(maxsize=1)
def _calendar_last() -> str:
    cal = _trade_calendar()
    return max(cal) if cal else ""


def is_trading_day(dt: datetime | None = None) -> bool:
    """交易日判断：优先用交易所日历，日历未覆盖的未来日期回退工作日。"""
    dt = dt or datetime.now()
    day = dt.strftime("%Y-%m-%d")
    cal = _trade_calendar()
    last = _calendar_last()
    if cal and last and day <= last:
        return day in cal
    return dt.weekday() < 5


def _target_symbols(repo: Repository) -> list[str]:
    """增量目标：板块成分 + 所有自定义股票池成员（去重）。"""
    rows = repo.query("SELECT DISTINCT symbol FROM dim_sector_member")
    syms = [r["symbol"] for r in rows]
    extra = repo.query("SELECT DISTINCT symbol FROM dim_universe_member")
    syms = list(dict.fromkeys(syms + [r["symbol"] for r in extra]))
    if syms:
        return syms
    seeds = load_seed_members()
    return [s for items in seeds.values() for s, _ in items]


def latest_complete_quote_date(repo: Repository, symbols: list[str],
                               min_ratio: float = 0.9,
                               lookback_days: int = 40) -> str | None:
    """最近一个成员行情覆盖率 ≥ `min_ratio` 的交易日。

    用途：云主机上数据源收盘后分批发布，当日可能只更到一部分标的。与其让
    整条流水线空转（历史上导致回测连续 10 个交易日不推进），不如让每个股票池
    按「自己成员最近完整的那一天」降级运行。

    覆盖率分母取「库内至少有一条行情的成员数」，避免长期停牌/退市成员把比例
    永久压在阈值以下。
    """
    syms = list(dict.fromkeys(str(s) for s in symbols))
    if not syms:
        return None
    ph = ",".join("?" for _ in syms)
    rows = repo.query(
        f"SELECT trade_date, COUNT(*) AS n FROM fact_quote "
        f"WHERE symbol IN ({ph}) GROUP BY trade_date "
        f"ORDER BY trade_date DESC LIMIT ?",
        tuple(syms) + (max(1, int(lookback_days)),))
    if not rows:
        return None
    have = repo.query(
        f"SELECT COUNT(DISTINCT symbol) AS n FROM fact_quote WHERE symbol IN ({ph})",
        tuple(syms))[0]["n"]
    need = max(1, math.ceil(int(have) * min_ratio))
    for r in rows:
        if int(r["n"]) >= need:
            return r["trade_date"]
    return None


def _fill_pct_chg(repo: Repository, symbol: str, df: pd.DataFrame) -> None:
    """补齐请求窗口首日缺失的涨跌幅。

    腾讯/新浪接口只依据窗口内相邻收盘价计算涨跌幅，首行为空。若直接入库会
    覆盖历史有效值，导致 vol_20 / max_ret_20 / xl_ratio_5d 等滚动因子在最新
    交易日变为 NaN。这里用库内前收盘补齐首行，其余空值用窗口内递推补齐。
    """
    if "pct_chg" not in df.columns or df["pct_chg"].notna().all():
        return
    first_date = str(df.iloc[0]["trade_date"])
    prev = None
    r = repo.query(
        "SELECT close FROM fact_quote WHERE symbol=? AND trade_date<? "
        "ORDER BY trade_date DESC LIMIT 1",
        (symbol, first_date),
    )
    if r and r[0]["close"]:
        prev = float(r[0]["close"])

    pct = df["pct_chg"].astype(float).tolist()
    closes = df["close"].astype(float).tolist()
    for j, c in enumerate(closes):
        if pd.isna(pct[j]) and prev and prev > 0 and not pd.isna(c):
            pct[j] = (c / prev - 1) * 100
        if not pd.isna(c) and c > 0:
            prev = c
    df["pct_chg"] = pd.Series(pct, index=df.index).round(4)


def run_incremental(ds: DataSource, repo: Repository, lookback_days: int = 10) -> dict:
    """增量拉取并幂等入库。"""
    now = _now()
    today_compact = datetime.now().strftime("%Y%m%d")
    today_dash = datetime.now().strftime("%Y-%m-%d")
    if not is_trading_day():
        logger.info("非交易日，跳过增量")
        return {"skipped": True, "reason": "non-trading-day"}

    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    stats = {"price_ok": 0, "price_fail": 0, "flow_rows": 0, "sector_flow": 0}
    symbols = _target_symbols(repo)
    workers = max(1, int(os.getenv("SCS_INCREMENTAL_WORKERS", "1")))
    timeout = int(os.getenv("SCS_INCREMENTAL_TIMEOUT", "90"))
    logger.info(f"增量目标成分 {len(symbols)} 只（并发 {workers}）")

    # 1) 行情：先在 worker 里并发抓取（只做网络 IO），再在主线程串行入库，
    #    避免多线程写同一个 SQLite 连接。云主机单线程逐只抓 400+ 只经常跑不完。
    def _fetch(sym: str) -> tuple[str, pd.DataFrame | None, str | None]:
        try:
            df = _with_timeout(
                lambda s=sym: ds.daily_prices(s, start, today_compact), timeout,
                pd.DataFrame())
            return sym, df, None
        except Exception as e:  # noqa: BLE001
            return sym, None, str(e)

    fetched: list[tuple[str, pd.DataFrame | None, str | None]] = []
    if workers == 1:
        for sym in symbols:
            fetched.append(_fetch(sym))
            if len(fetched) % 10 == 0:
                logger.info(f"  行情进度 {len(fetched)}/{len(symbols)}")
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_fetch, s) for s in symbols]
            for i, fut in enumerate(as_completed(futures), 1):
                fetched.append(fut.result())
                if i % 10 == 0:
                    logger.info(f"  行情进度 {i}/{len(symbols)}")

    failed: list[str] = []
    for sym, df, err in fetched:
        if err or df is None or df.empty:
            stats["price_fail"] += 1
            failed.append(sym)
            if err:
                logger.warning(f"增量行情失败 {sym}: {err}")
            continue
        _fill_pct_chg(repo, sym, df)
        rows = df.to_dict("records")
        for r in rows:
            r.update({"symbol": sym, "src": "akshare:hfq", "fetched_at": now})
        repo.upsert("fact_quote", rows, ["symbol", "trade_date"])
        stats["price_ok"] += 1
    if failed:
        stats["failed_symbols"] = failed[:50]
        logger.warning(f"增量行情失败 {len(failed)} 只（示例 {failed[:8]}）")

    # 2) 个股资金流（腾讯全市场快照，一次调用）
    try:
        spot = _with_timeout(ds.spot, 180, pd.DataFrame())
        sub = spot[spot["symbol"].isin(set(symbols))]
        rows = [{
            "symbol": r.symbol,
            "trade_date": today_dash,
            "main_net": None if getattr(r, "main_net", None) is None else float(r.main_net) * 1e4,
            "close": getattr(r, "last", None),
            "pct_chg": getattr(r, "pct_chg", None),
            "src": "akshare:tx_spot",
            "fetched_at": now,
        } for r in sub.itertuples()]
        repo.upsert("fact_stock_flow", rows, ["symbol", "trade_date"])
        stats["flow_rows"] = len(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"增量个股资金流失败: {e}")

    # 3) 板块资金流（同花顺）
    for sec in load_sectors():
        cate = "concept" if sec["type"] == "concept" else "industry"
        try:
            sf = _with_timeout(
                lambda c=cate: ds.sector_fund_flow(cate=c, period="即时"), 90, pd.DataFrame())
            if not sf.empty and sec["name"] in set(sf["行业"]):
                row = sf[sf["行业"] == sec["name"]].iloc[0]
                net = row.get("净额")
                repo.upsert("fact_sector_flow", [{
                    "sector_code": sec["name"], "trade_date": today_dash,
                    "main_net": None if net is None else float(net) * 1e8,
                    "src": "akshare:ths", "fetched_at": now,
                }], ["sector_code", "trade_date"])
                stats["sector_flow"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"增量板块资金流失败 {sec['name']}: {e}")

    repo.set_watermark("fact_quote", today_compact)
    repo.set_watermark("fact_stock_flow", today_dash)
    repo.set_watermark("fact_sector_flow", today_dash)
    logger.info(f"增量完成: {stats}")
    return stats
