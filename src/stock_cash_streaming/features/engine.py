"""因子引擎：从 fact_quote / fact_stock_flow 计算因子并写入 fact_factor。

所有因子均为**滚动计算**，只使用 t 及之前的数据（严格无未来函数）。
因子口径变更时递增 FACTOR_VERSION，保证可复现。
"""

from typing import Callable

import numpy as np
import pandas as pd

from ..common.logging import logger
from ..storage.repository import Repository

FACTOR_VERSION = "v2"

# 研究/行业池只需要的技术因子（避免为上千只股票写入全部因子）
RESEARCH_FACTORS = ["vol_20", "turnover_5", "turnover_std_20", "amount_20d",
                    "max_ret_20", "ret_20", "boll_pos", "amihud_20", "ma60_ratio"]


# ---------- 数据加载 ----------

def _load_quotes(repo: Repository) -> pd.DataFrame:
    rows = repo.query(
        "SELECT symbol, trade_date, close, amount, turnover, pct_chg FROM fact_quote ORDER BY symbol, trade_date"
    )
    return pd.DataFrame([dict(r) for r in rows])


def _load_flows(repo: Repository) -> pd.DataFrame:
    rows = repo.query(
        "SELECT symbol, trade_date, main_net, xl_net FROM fact_stock_flow ORDER BY symbol, trade_date"
    )
    return pd.DataFrame([dict(r) for r in rows])


# ---------- 技术因子 ----------

def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta.clip(upper=0)).rolling(n).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_technical(q: pd.DataFrame) -> pd.DataFrame:
    """技术因子（价量），返回长表：symbol, trade_date, <factor>..."""
    if q.empty:
        return pd.DataFrame()
    out = []
    for sym, g in q.groupby("symbol", sort=False):
        g = g.sort_values("trade_date").copy()
        c = g["close"]
        ma5 = c.rolling(5).mean()
        ma20 = c.rolling(20).mean()
        std20 = c.rolling(20).std()
        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()

        g["ret_5"] = c.pct_change(5) * 100
        g["ret_20"] = c.pct_change(20) * 100
        g["ma5_ratio"] = c / ma5 - 1
        g["ma20_ratio"] = c / ma20 - 1
        g["boll_pos"] = (c - (ma20 - 2 * std20)) / (4 * std20)
        g["rsi14"] = _rsi(c, 14)
        g["macd_hist"] = (dif - dea) * 2
        g["vol_20"] = g["pct_chg"].rolling(20).std() * np.sqrt(252)
        g["turnover_5"] = g["turnover"].rolling(5).mean()
        g["ma60_ratio"] = c / c.rolling(60).mean() - 1
        g["amount_20d"] = g["amount"].rolling(20).mean()
        # v2 新增：彩票效应（单日最大涨幅）、换手波动、非流动性（Amihud）
        g["max_ret_20"] = g["pct_chg"].rolling(20).max()
        g["turnover_std_20"] = g["turnover"].rolling(20).std()
        g["amihud_20"] = (c.pct_change().abs() / g["amount"]).rolling(20).mean() * 1e8

        cols = ["symbol", "trade_date", "ret_5", "ret_20", "ma5_ratio", "ma20_ratio",
                "boll_pos", "rsi14", "macd_hist", "vol_20", "turnover_5",
                "ma60_ratio", "amount_20d", "max_ret_20", "turnover_std_20", "amihud_20"]
        out.append(g[cols])
    return pd.concat(out, ignore_index=True)


# ---------- 资金流因子 ----------

def compute_flow(q: pd.DataFrame, f: pd.DataFrame) -> pd.DataFrame:
    """资金流因子。q 提供成交额用于占比；f 为个股资金流。"""
    if f.empty or q.empty:
        return pd.DataFrame()
    df = f.merge(q[["symbol", "trade_date", "amount"]], on=["symbol", "trade_date"], how="left")
    df = df.sort_values(["symbol", "trade_date"])
    out = []
    for sym, g in df.groupby("symbol", sort=False):
        g = g.copy()
        mn = g["main_net"]
        g["main_net_5d"] = mn.rolling(5).sum()
        amt5 = g["amount"].rolling(5).sum()
        g["main_net_ratio_5d"] = np.where(amt5 > 0, g["main_net_5d"] / amt5 * 100, np.nan)
        g["inflow_days_5"] = (mn > 0).rolling(5).sum()
        absmean = mn.abs().rolling(10).mean()
        mean = mn.rolling(10).mean()
        g["institution_trend"] = np.where(absmean > 0, mean / absmean, np.nan)
        g["xl_ratio_5d"] = (g["xl_net"] / g["amount"] * 100).rolling(5).mean()
        out.append(g[["symbol", "trade_date", "main_net_5d", "main_net_ratio_5d",
                      "inflow_days_5", "institution_trend", "xl_ratio_5d"]])
    return pd.concat(out, ignore_index=True)


# ---------- 写入 ----------

def _write_factors(repo: Repository, df: pd.DataFrame) -> dict[str, int]:
    value_cols = [c for c in df.columns if c not in ("symbol", "trade_date")]
    stats: dict[str, int] = {}
    for c in value_cols:
        sub = df[["symbol", "trade_date", c]].dropna()
        if sub.empty:
            stats[c] = 0
            continue
        rows = [
            {"factor": c, "symbol": x.symbol, "trade_date": x.trade_date,
             "version": FACTOR_VERSION, "value": float(getattr(x, c))}
            for x in sub.itertuples(index=False)
        ]
        repo.upsert("fact_factor", rows, ["factor", "symbol", "trade_date", "version"])
        stats[c] = len(rows)
    return stats


def compute_all(repo: Repository, include_flow: bool = True,
                keep_factors: list[str] | None = None) -> dict[str, int]:
    """计算全部因子并入库，返回各因子写入行数。

    include_flow : 是否计算资金流因子
    keep_factors : 仅保留指定因子（研究模式，减少写入量）
    """
    q = _load_quotes(repo)
    if q.empty:
        logger.warning("无行情数据，跳过因子计算")
        return {}
    tech = compute_technical(q)
    frames = [tech]
    if include_flow:
        f = _load_flows(repo)
        flow = compute_flow(q, f)
        if not flow.empty:
            frames.append(flow)
    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    if keep_factors:
        cols = ["symbol", "trade_date"] + [c for c in keep_factors if c in df.columns]
        df = df[cols]
    stats = _write_factors(repo, df)
    logger.info(f"因子计算完成：{len(stats)} 个因子，{sum(stats.values())} 行")
    return stats


# ---------- 注册表（便于扩展） ----------

FACTORS: dict[str, Callable] = {}
