"""指数行情入库（回测基准与市场收益）。

指数：
- sh000300 沪深300
- sh000905 中证500
- sh000852 中证1000
- sh000985 中证全指

数据源：腾讯指数日线（`stock_zh_index_daily_tx`）。
"""

from __future__ import annotations

from datetime import datetime

from ..common.logging import logger
from ..storage.repository import Repository

INDEXES = {
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "sh000985": "中证全指",
}


def sync_index_quotes(repo: Repository, start_date: str = "2018-01-01",
                      end_date: str | None = None) -> dict:
    """拉取并写入 fact_index_quote（幂等 upsert）。"""
    from ..datasource.akshare_src import tx_index_daily

    end_date = end_date or datetime.now().strftime("%Y-%m-%d")
    total = 0
    stats: dict[str, int] = {}
    for code in INDEXES:
        try:
            df = tx_index_daily(code)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"指数行情失败 {code}: {e}")
            stats[code] = 0
            continue
        if df is None or df.empty:
            stats[code] = 0
            continue
        df = df[(df["trade_date"] >= start_date) & (df["trade_date"] <= end_date)].copy()
        if df.empty:
            stats[code] = 0
            continue
        df["close"] = df["close"].astype(float)
        df["pct_chg"] = (df["close"].pct_change() * 100).round(4)
        rows = [{"index_code": code, "trade_date": r.trade_date,
                 "close": float(r.close), "pct_chg": None if r.pct_chg != r.pct_chg else float(r.pct_chg)}
                for r in df.itertuples()]
        repo.upsert("fact_index_quote", rows, ["index_code", "trade_date"])
        stats[code] = len(rows)
        total += len(rows)
        logger.info(f"指数 {code} 入库 {len(rows)} 行")
    repo.set_watermark("fact_index_quote", end_date)
    return {"rows": total, **stats}


def index_series(repo: Repository, code: str = "sh000300"):
    """读取指数收盘价序列（pandas.Series，索引为交易日）。"""
    import pandas as pd

    rows = repo.query(
        "SELECT trade_date, close FROM fact_index_quote WHERE index_code=? ORDER BY trade_date",
        (code,),
    )
    return pd.Series({r["trade_date"]: float(r["close"]) for r in rows}, dtype="float64")
