"""基本面因子：财务质量、现金流质量、估值、股息可持续性。

口径：
- 财务指标来自 fact_financial（新浪摘要 + Baostock 公告日），只使用 pub_date <= t 的记录；
- TTM = 最近 4 个单季之和；单季由累计值差分得到；
- 估值按 t 日收盘价 × 最新公告的股本（净资产 / 每股净资产）计算；
- 分红按公告日滚动 3 年，避免使用未来分红。

已知限制：
- 新浪摘要为当前可见值，未保留历史修订版本；公告日已按 point-in-time 处理。
- 无形资产/商誉等明细未纳入，质量因子以 ROE、现金流匹配度为主。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..common.logging import logger
from ..storage.repository import Repository

FUNDAMENTAL_VERSION = "v3"

CUMULATIVE = ("net_profit", "revenue", "ocf", "eps", "fcf_ps")


def _load_financials(repo: Repository, symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    ph = ",".join("?" for _ in symbols)
    rows = repo.query(
        f"SELECT symbol, stat_date, pub_date, metric, value FROM fact_financial "
        f"WHERE symbol IN ({ph}) ORDER BY symbol, stat_date",
        tuple(symbols),
    )
    return pd.DataFrame([dict(r) for r in rows])


def _load_dividends(repo: Repository, symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    ph = ",".join("?" for _ in symbols)
    rows = repo.query(
        f"SELECT symbol, announce_date, cash_ps FROM fact_dividend "
        f"WHERE symbol IN ({ph}) AND cash_ps > 0 ORDER BY symbol, announce_date",
        tuple(symbols),
    )
    return pd.DataFrame([dict(r) for r in rows])


def _load_closes(repo: Repository, symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    ph = ",".join("?" for _ in symbols)
    rows = repo.query(
        f"SELECT symbol, trade_date, close FROM fact_quote WHERE symbol IN ({ph})",
        tuple(symbols),
    )
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    return df.pivot(index="trade_date", columns="symbol", values="close").sort_index()


def _rolling(group: pd.Series, n: int, min_periods: int, how: str = "sum") -> pd.Series:
    fn = getattr(group.rolling(n, min_periods=min_periods), how)
    return fn()


def _report_panel(fin: pd.DataFrame) -> pd.DataFrame:
    """把长表转成报告期宽表，并派生 TTM 指标。"""
    if fin.empty:
        return pd.DataFrame()
    wide = fin.pivot_table(
        index=["symbol", "stat_date", "pub_date"], columns="metric", values="value"
    ).reset_index()
    wide = wide.sort_values(["symbol", "stat_date"]).reset_index(drop=True)
    wide["year"] = wide["stat_date"].str[:4]
    wide["quarter"] = wide["stat_date"].str[5:7].map({"03": 1, "06": 2, "09": 3, "12": 4})

    for m in CUMULATIVE:
        if m not in wide.columns:
            continue
        if m == "fcf_ps":
            # 每股自由现金流多为半年/年度披露，无法凑满 4 个单季，取最近一期值
            wide["fcf_ps_ttm"] = wide["fcf_ps"]
            continue
        q = f"{m}_q"
        wide[q] = wide.groupby(["symbol", "year"])[m].diff()
        wide.loc[wide["quarter"] == 1, q] = wide.loc[wide["quarter"] == 1, m]
        wide[f"{m}_ttm"] = wide.groupby("symbol")[q].transform(
            lambda s: _rolling(s, 4, 4)
        )

    if "roe" in wide.columns:
        wide["roe_std_8q"] = wide.groupby("symbol")["roe"].transform(
            lambda s: _rolling(s, 8, 4, how="std")
        )
    if "net_profit_ttm" in wide.columns and "equity" in wide.columns:
        wide["roe_ttm"] = np.where(
            wide["equity"] > 0, wide["net_profit_ttm"] / wide["equity"] * 100, np.nan
        )
        wide["shares"] = np.where(
            (wide["bps"] > 0) & (wide["equity"] > 0), wide["equity"] / wide["bps"], np.nan
        )
        wide["cfo_to_np"] = np.where(
            wide["net_profit_ttm"] > 0, wide["ocf_ttm"] / wide["net_profit_ttm"], np.nan
        )
        wide["cfo_to_np_std_8q"] = wide.groupby("symbol")["cfo_to_np"].transform(
            lambda s: _rolling(s, 8, 4, how="std")
        )
        wide["cfo_to_rev"] = np.where(
            wide["revenue_ttm"] > 0, wide["ocf_ttm"] / wide["revenue_ttm"], np.nan
        )
    if "eps_ttm" not in wide.columns and "eps" in wide.columns:
        wide["eps_ttm"] = wide["eps"]
    if "fcf_ps_ttm" not in wide.columns and "fcf_ps" in wide.columns:
        wide["fcf_ps_ttm"] = wide["fcf_ps"]

    return wide


def _dividend_daily(div: pd.DataFrame, symbol: str, dates: pd.Index) -> pd.DataFrame:
    """按公告日滚动 3 年，返回 div_yield / continuity / payout 的日序列。"""
    base = pd.DataFrame(index=dates)
    if div.empty or "symbol" not in div.columns:
        base["div_ttm"] = 0.0
        base["div_continuity_3y"] = 0.0
        return base
    sub = div[div["symbol"] == symbol].sort_values("announce_date")
    if sub.empty:
        base["div_ttm"] = 0.0
        base["div_continuity_3y"] = 0.0
        return base

    ann = pd.to_datetime(sub["announce_date"]).values
    cash = sub["cash_ps"].astype(float).values
    cum = np.concatenate([[0.0], np.cumsum(cash)])
    d = pd.to_datetime(dates)
    hi = np.searchsorted(ann, d.values, side="right")
    lo = np.searchsorted(ann, d.values - np.timedelta64(3 * 365, "D"), side="right")
    base["div_ttm"] = cum[hi] - cum[lo]

    years = pd.to_datetime(sub["announce_date"]).dt.year.values
    cont = []
    for t in d:
        window = [t.year - 2, t.year - 1, t.year]
        hit = {y for y, a in zip(years, ann) if y in window and a <= np.datetime64(t)}
        cont.append(len(hit) / 3.0)
    base["div_continuity_3y"] = cont
    return base


def compute_fundamental_factors(repo: Repository, symbols: list[str] | None = None) -> dict[str, int]:
    """计算基本面因子并写入 fact_factor(version='v3')。"""
    if symbols is None:
        symbols = [r["symbol"] for r in repo.query("SELECT DISTINCT symbol FROM fact_financial")]
    fin = _load_financials(repo, symbols)
    if fin.empty:
        logger.warning("无财务数据，跳过基本面因子（先运行 cli fundamentals）")
        return {}

    reports = _report_panel(fin)
    div = _load_dividends(repo, symbols)
    closes = _load_closes(repo, symbols)
    if closes.empty:
        return {}

    frames: list[pd.DataFrame] = []

    for sym in closes.columns:
        rep = reports[reports["symbol"] == sym].sort_values("pub_date")
        if rep.empty:
            continue
        px = closes[sym].dropna()
        px_df = pd.DataFrame({"trade_date": px.index, "close": px.values})
        px_df["_d"] = pd.to_datetime(px_df["trade_date"])
        rep2 = rep.copy()
        rep2 = rep2[rep2["pub_date"].notna() & (rep2["pub_date"] != "")]
        if rep2.empty:
            continue
        rep2["_d"] = pd.to_datetime(rep2["pub_date"])
        merged = pd.merge_asof(
            px_df.sort_values("_d"), rep2.sort_values("_d"),
            on="_d", direction="backward", suffixes=("", "_rep"),
        ).set_index("trade_date")

        out = pd.DataFrame(index=merged.index)
        nan = pd.Series(np.nan, index=merged.index)
        for col, name in [("roe_ttm", "roe_ttm"), ("roe_std_8q", "roe_std_8q"),
                          ("cfo_to_np", "cfo_to_np"), ("cfo_to_np_std_8q", "cfo_to_np_std_8q"),
                          ("cfo_to_rev", "cfo_to_rev"), ("liability_to_asset", "leverage")]:
            out[name] = merged[col] if col in merged.columns else nan

        shares = merged["shares"] if "shares" in merged.columns else nan
        equity = merged["equity"] if "equity" in merged.columns else nan
        np_ttm = merged["net_profit_ttm"] if "net_profit_ttm" in merged.columns else nan
        fcf_ps = merged["fcf_ps_ttm"] if "fcf_ps_ttm" in merged.columns else nan
        eps = merged["eps_ttm"] if "eps_ttm" in merged.columns else nan

        mcap = merged["close"] * shares
        out["ep_ttm"] = np.where(mcap > 0, np_ttm / mcap, np.nan)
        out["bp_mrq"] = np.where(mcap > 0, equity / mcap, np.nan)
        out["fcf_yield"] = np.where(merged["close"] > 0, fcf_ps / merged["close"], np.nan)

        dpanel = _dividend_daily(div, sym, out.index)
        out["div_yield_3y"] = np.where(merged["close"] > 0, dpanel["div_ttm"] / merged["close"], np.nan)
        out["div_continuity_3y"] = dpanel["div_continuity_3y"]
        out["div_payout"] = np.where(eps > 0, dpanel["div_ttm"] / eps, np.nan)
        out["div_payout"] = out["div_payout"].where(out["div_payout"].between(0, 2))

        out["symbol"] = sym
        out = out.reset_index()
        frames.append(out)

    if not frames:
        return {}
    panel = pd.concat(frames, ignore_index=True)
    value_cols = [c for c in panel.columns if c not in ("symbol", "trade_date")]
    panel[value_cols] = panel[value_cols].replace([np.inf, -np.inf], np.nan)
    stats: dict[str, int] = {}
    for c in value_cols:
        sub = panel[["symbol", "trade_date", c]].dropna()
        if sub.empty:
            stats[c] = 0
            continue
        rows = [{
            "factor": c, "symbol": s, "trade_date": d,
            "version": FUNDAMENTAL_VERSION, "value": float(v),
        } for s, d, v in sub.itertuples(index=False, name=None)]
        for i in range(0, len(rows), 50000):
            repo.upsert("fact_factor", rows[i:i + 50000], ["factor", "symbol", "trade_date", "version"])
        stats[c] = len(rows)
    logger.info(f"基本面因子完成：{len(stats)} 个，{sum(stats.values())} 行（version={FUNDAMENTAL_VERSION}）")
    return stats


__all__ = ["compute_fundamental_factors", "FUNDAMENTAL_VERSION"]
