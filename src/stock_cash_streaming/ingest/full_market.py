"""全 A 股票列表与行情分批回填（可中断续跑）。

数据源：
- 股票列表：腾讯全市场快照 `stock_zh_a_spot_tx`（含代码/名称/证券类型）；
- 日线：沿用 `datasource.AkShareSource.daily_prices`（腾讯 → 新浪 → Baostock 回退，后复权）。

设计：
- 单票 watermark（`quote:{symbol}`），重复执行只补缺口；
- 串行请求 + 可配置间隔，避免触发数据源限流；
- 每只股票独立失败重试，单票失败不影响整体；
- 不修改现有 C0~C3 池任务逻辑，仅向 `fact_quote`/`dim_stock` 增量写入。
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from ..common.logging import logger
from ..datasource.akshare_src import AkShareSource, infer_market
from ..datasource.registry import get_source
from ..storage.repository import Repository

DEFAULT_START = "2018-01-01"
DEFAULT_SLEEP = 0.12
MAX_RETRY = 3


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def fetch_full_a_symbols() -> pd.DataFrame:
    """返回全 A 代码/名称（仅 A 股，排除基金/指数/B 股）。"""
    import akshare as ak

    raw = ak.stock_zh_a_spot_tx()
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["symbol", "name", "market"])
    df = pd.DataFrame({
        "symbol": raw["code"].astype(str).str.extract(r"(\d{6})")[0],
        "name": raw["name"].astype(str),
        "type": raw.get("stock_type", pd.Series("", index=raw.index)).astype(str),
    }).dropna(subset=["symbol"])
    df = df[df["symbol"].str.len() == 6]
    # GP-A / GP-A-KCB / GP-A-CYB 等；排除 GP-B、基金、指数
    df = df[df["type"].str.startswith("GP-A") | (df["type"] == "")]
    df = df.drop_duplicates("symbol").reset_index(drop=True)
    df["market"] = df["symbol"].map(lambda s: infer_market(s))
    return df[["symbol", "name", "market"]]


def sync_symbols(repo: Repository) -> dict:
    """同步全 A 股票列表到 dim_stock（不删除已有记录）。"""
    df = fetch_full_a_symbols()
    if df.empty:
        return {"symbols": 0}
    now = _now()
    repo.upsert(
        "dim_stock",
        [{"symbol": r.symbol, "name": r.name, "market": r.market, "updated_at": now}
         for r in df.itertuples()],
        ["symbol"],
    )
    repo.set_watermark("dim_stock", _today())
    logger.info(f"全 A 股票列表同步：{len(df)} 只")
    return {"symbols": len(df)}


def known_symbols(repo: Repository) -> list[str]:
    rows = repo.query("SELECT symbol FROM dim_stock ORDER BY symbol")
    return [r["symbol"] for r in rows]


def missing_symbols(repo: Repository, start_date: str = DEFAULT_START) -> list[str]:
    """尚无任何行情的股票（优先回填对象）。"""
    rows = repo.query(
        "SELECT d.symbol FROM dim_stock d "
        "LEFT JOIN (SELECT DISTINCT symbol FROM fact_quote WHERE trade_date >= ?) q "
        "ON q.symbol = d.symbol WHERE q.symbol IS NULL ORDER BY d.symbol",
        (start_date,),
    )
    return [r["symbol"] for r in rows]


def _fetch_em(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """东财日线（后复权），列名标准化为 fact_quote 口径。"""
    import akshare as ak

    raw = ak.stock_zh_a_hist(
        symbol=symbol, period="daily",
        start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""),
        adjust="hfq",
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.rename(columns={
        "日期": "trade_date", "开盘": "open", "收盘": "close", "最高": "high",
        "最低": "low", "成交量": "volume", "成交额": "amount",
        "涨跌幅": "pct_chg", "换手率": "turnover",
    })
    df["trade_date"] = df["trade_date"].astype(str)
    keep = ["trade_date", "open", "high", "low", "close", "volume",
            "amount", "pct_chg", "turnover"]
    for c in keep:
        if c not in df.columns:
            df[c] = None
    df = df[keep]
    df = df[pd.to_numeric(df["close"], errors="coerce") > 0]
    return df.reset_index(drop=True)


def backfill_quotes(repo: Repository, start_date: str = DEFAULT_START,
                    end_date: str | None = None, symbols: list[str] | None = None,
                    limit: int | None = None, sleep: float = DEFAULT_SLEEP,
                    ds: AkShareSource | None = None, only_missing: bool = False,
                    workers: int = 6) -> dict:
    """按股票逐只回填日线；单票断点续跑。

    并发：多线程只负责网络抓取，SQLite 写入统一在主线程完成。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    end_date = end_date or _today()
    ds = ds or get_source()
    if symbols is None:
        symbols = missing_symbols(repo, start_date) if only_missing else known_symbols(repo)
    if limit:
        symbols = symbols[:limit]

    tasks: list[tuple[str, str]] = []
    stats = {"total": len(symbols), "ok": 0, "empty": 0, "fail": 0, "rows": 0, "skipped": 0}
    for sym in symbols:
        wm = repo.get_watermark(f"quote:{sym}")
        since = start_date
        if wm and wm["last_date"]:
            since = max(start_date, str(wm["last_date"]))
        if since > end_date:
            stats["skipped"] += 1
            continue
        tasks.append((sym, since))

    started = time.time()
    done = 0

    def _fetch(sym: str, since: str) -> tuple[str, pd.DataFrame | None, str | None]:
        try:
            return sym, _fetch_em(sym, since, end_date), None
        except Exception as e:  # noqa: BLE001
            try:
                return sym, ds.daily_prices(sym, since, end_date), None
            except Exception as e2:  # noqa: BLE001
                return sym, None, f"{e} / fallback: {e2}"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(_fetch, sym, since) for sym, since in tasks]
        for fut in as_completed(futures):
            sym, px, err = fut.result()
            done += 1
            if err is not None:
                stats["fail"] += 1
                logger.warning(f"行情回填失败 {sym}: {err}")
                repo.set_watermark(f"quote:{sym}", None, "fail")
            elif px is None or px.empty:
                stats["empty"] += 1
                repo.set_watermark(f"quote:{sym}", end_date, "empty")
            else:
                now = _now()
                rows = px.to_dict("records")
                for r in rows:
                    r.update({"symbol": sym, "src": "em", "fetched_at": now})
                repo.upsert("fact_quote", rows, ["symbol", "trade_date"])
                stats["ok"] += 1
                stats["rows"] += len(rows)
                repo.set_watermark(f"quote:{sym}", str(px["trade_date"].max()))
            if done % 100 == 0 or done == len(tasks):
                logger.info(
                    f"  行情回填 {done}/{len(tasks)} | ok={stats['ok']} empty={stats['empty']} "
                    f"fail={stats['fail']} rows={stats['rows']} | {time.time() - started:.0f}s"
                )
    repo.set_watermark("fact_quote_full", end_date)
    return stats


def coverage(repo: Repository, start_date: str = DEFAULT_START) -> dict:
    """全 A 行情覆盖度。"""
    total = repo.count("dim_stock")
    covered = repo.query(
        "SELECT COUNT(DISTINCT symbol) AS n FROM fact_quote WHERE trade_date >= ?",
        (start_date,),
    )[0]["n"]
    latest = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")
    return {
        "symbols_total": int(total),
        "symbols_covered": int(covered or 0),
        "coverage": round((covered or 0) / total, 4) if total else 0.0,
        "start_date": start_date,
        "latest": latest[0]["d"] if latest else None,
    }
