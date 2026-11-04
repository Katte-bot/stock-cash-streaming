"""v5 基本面/事件因子（价值、质量、投资、PEAD）。

口径：
- 财务数据来自 `fact_financial`（含现金流量表/资产负债表扩充指标），
  只使用 `pub_date <= t` 的记录；
- TTM = 最近 4 个单季之和；单季由累计值差分；
- 估值按 t 日收盘价 × 最新公告股本计算；
- SUE = 单季净利润同比惊喜 ÷ 过去 8 季惊喜标准差；
- 公告日异常收益 = 公告后首个交易日收益 − 当日等权市场收益。

说明：所有因子按需计算并缓存，不写 SQLite 日频。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..common.logging import logger
from ..storage.repository import Repository
from .fundamentals import (
    _dividend_daily,
    _load_dividends,
    _load_financials,
    _report_panel,
)
from .panel import Panel

TRADING_DAYS = 252
EVENT_WINDOW = 60


def _ttm_sum(series: pd.Series) -> pd.Series:
    return series.rolling(4, min_periods=4).sum()


def _report_frame(repo: Repository, symbols: list[str]) -> pd.DataFrame:
    """报告期宽表 + v5 派生指标（仍以报告期为粒度）。"""
    fin = _load_financials(repo, symbols)
    if fin.empty:
        return pd.DataFrame()
    wide = _report_panel(fin)
    if wide.empty:
        return wide

    wide = wide.sort_values(["symbol", "stat_date"]).reset_index(drop=True)
    g = wide.groupby("symbol", sort=False)

    # 单季 CAPEX（累计值差分），再滚动 4 季得到 TTM
    if "capex" in wide.columns:
        wide["capex_q"] = g["capex"].diff()
        wide.loc[wide["quarter"] == 1, "capex_q"] = wide.loc[wide["quarter"] == 1, "capex"]
        wide["capex_ttm"] = wide.groupby("symbol", sort=False)["capex_q"].transform(_ttm_sum)
    else:
        wide["capex_ttm"] = np.nan

    # 总资产：优先用资产负债表；缺失时用 净资产/(1-资产负债率) 估算
    if "total_assets" in wide.columns:
        assets = wide["total_assets"].astype(float)
    else:
        assets = pd.Series(np.nan, index=wide.index)
    if "equity" in wide.columns and "liability_to_asset" in wide.columns:
        lev = wide["liability_to_asset"].astype(float) / 100.0
        derived = wide["equity"] / (1 - lev).replace(0, np.nan)
        assets = assets.fillna(derived)
    wide["total_assets_v5"] = assets
    wide["assets_prev4"] = wide.groupby("symbol", sort=False)["total_assets_v5"].shift(4)
    wide["revenue_prev4"] = wide.groupby("symbol", sort=False)["revenue_ttm"].shift(4)

    wide["asset_growth"] = wide["total_assets_v5"] / wide["assets_prev4"] - 1
    turnover = wide["revenue_ttm"] / wide["total_assets_v5"].replace(0, np.nan)
    wide["asset_turnover_change"] = turnover / turnover.groupby(wide["symbol"]).shift(4) - 1
    wide["capex_growth"] = wide["capex_ttm"] / wide.groupby("symbol", sort=False)["capex_ttm"].shift(4) - 1
    wide["capex_to_assets"] = wide["capex_ttm"] / wide["total_assets_v5"].replace(0, np.nan)
    added = wide["total_assets_v5"] - wide["assets_prev4"]
    wide["investment_intensity"] = (
        (wide["capex_ttm"].fillna(0) + added) / wide["revenue_ttm"].replace(0, np.nan)
    )
    wide["roa_ttm"] = wide["net_profit_ttm"] / wide["total_assets_v5"].replace(0, np.nan) * 100
    debt = wide.get("interest_debt", pd.Series(np.nan, index=wide.index))
    wide["roic_calc"] = wide["net_profit_ttm"] / (
        wide["equity"] + debt.fillna(0)
    ).replace(0, np.nan) * 100
    wide["accruals"] = (
        (wide["net_profit_ttm"] - wide["ocf_ttm"]) / wide["total_assets_v5"].replace(0, np.nan)
    )

    # SUE / 营收惊喜（季节随机游走）
    if "net_profit_q" in wide.columns:
        np4 = wide.groupby("symbol", sort=False)["net_profit_q"].shift(4)
        surprise = wide["net_profit_q"] - np4
        std = wide.groupby("symbol", sort=False)["net_profit_q"].transform(
            lambda s: s.diff(4).rolling(8, min_periods=4).std())
        wide["sue"] = surprise / std.replace(0, np.nan)
    if "revenue_q" in wide.columns:
        rev4 = wide.groupby("symbol", sort=False)["revenue_q"].shift(4)
        surprise_r = wide["revenue_q"] - rev4
        std_r = wide.groupby("symbol", sort=False)["revenue_q"].transform(
            lambda s: s.diff(4).rolling(8, min_periods=4).std())
        wide["revenue_surprise"] = surprise_r / std_r.replace(0, np.nan)
    return wide


def _align(rep: pd.DataFrame, dates: pd.Index, cols: list[str]) -> pd.DataFrame:
    """按 pub_date 把报告期指标对齐到交易日（严格 backward）。"""
    sub = rep.dropna(subset=["pub_date"]).sort_values("pub_date")
    if sub.empty:
        return pd.DataFrame(np.nan, index=dates, columns=cols)
    pub = pd.to_datetime(sub["pub_date"]).values.astype("datetime64[D]")
    d = pd.to_datetime(pd.Index(dates)).values.astype("datetime64[D]")
    pos = np.searchsorted(pub, d, side="right") - 1
    out = pd.DataFrame(index=dates)
    safe = np.clip(pos, 0, None)
    valid = pos >= 0
    for c in cols:
        vals = sub[c].to_numpy(dtype="float64") if c in sub.columns else np.full(len(sub), np.nan)
        arr = np.where(valid, vals[safe], np.nan)
        out[c] = arr
    return out


def compute_v5(repo: Repository, panel: Panel) -> dict[str, pd.DataFrame]:
    """返回 v5 因子宽表字典（索引与 panel 一致）。"""
    dates = panel.close.index
    symbols = list(panel.close.columns)
    rep = _report_frame(repo, symbols)
    if rep.empty:
        logger.warning("无财务数据，跳过 v5 因子")
        return {}

    div = _load_dividends(repo, symbols)
    out: dict[str, pd.DataFrame] = {
        k: pd.DataFrame(np.nan, index=dates, columns=symbols, dtype="float32")
        for k in ("ep_ttm", "bp_mrq", "cfo_yield", "fcf_yield", "div_yield_3y",
                  "size_mcap", "gross_margin", "net_margin", "roa_ttm", "roic",
                  "accruals", "asset_growth", "asset_turnover_change", "capex_growth",
                  "capex_to_assets", "investment_intensity", "sue", "revenue_surprise",
                  "announce_ret_1d", "attention_20")
    }
    report_cols = [
        "net_profit_ttm", "ocf_ttm", "equity", "shares", "bps", "fcf_ps_ttm",
        "gross_margin", "net_margin", "roa_ttm", "roic_calc", "accruals",
        "asset_growth", "asset_turnover_change", "capex_growth", "capex_to_assets",
        "investment_intensity", "sue", "revenue_surprise",
    ]
    market_pct = panel.pct.mean(axis=1, skipna=True)

    for sym in symbols:
        sub = rep[rep["symbol"] == sym]
        if sub.empty:
            continue
        aligned = _align(sub, dates, report_cols)
        close = panel.close[sym]
        shares = aligned["shares"] if "shares" in aligned else np.nan
        mcap = close * shares
        out["size_mcap"][sym] = mcap

        out["ep_ttm"][sym] = aligned["net_profit_ttm"] / mcap.replace(0, np.nan)
        out["bp_mrq"][sym] = aligned["equity"] / mcap.replace(0, np.nan)
        out["cfo_yield"][sym] = aligned["ocf_ttm"] / mcap.replace(0, np.nan)
        out["fcf_yield"][sym] = aligned["fcf_ps_ttm"] / close.replace(0, np.nan)
        for src, dst in (("gross_margin", "gross_margin"), ("net_margin", "net_margin"),
                         ("roa_ttm", "roa_ttm"), ("roic_calc", "roic"),
                         ("accruals", "accruals"), ("asset_growth", "asset_growth"),
                         ("asset_turnover_change", "asset_turnover_change"),
                         ("capex_growth", "capex_growth"), ("capex_to_assets", "capex_to_assets"),
                         ("investment_intensity", "investment_intensity"),
                         ("sue", "sue"), ("revenue_surprise", "revenue_surprise")):
            out[dst][sym] = aligned[src]

        dpanel = _dividend_daily(div, sym, dates)
        out["div_yield_3y"][sym] = dpanel["div_ttm"] / close.replace(0, np.nan)

        # PEAD：公告后 1 日异常收益与 20 日关注度
        pub_dates = sub["pub_date"].dropna().sort_values().unique()
        if len(pub_dates):
            idx = pd.Index(dates)
            ann = pd.Series(np.nan, index=idx)
            att = pd.Series(np.nan, index=idx)
            turn = panel.turnover[sym]
            base_turn = turn.rolling(250, min_periods=60).median()
            att_series = turn.rolling(20, min_periods=10).mean() / base_turn.replace(0, np.nan)
            for pub in pub_dates:
                pos = idx.searchsorted(pub, side="left")
                if pos >= len(idx):
                    continue
                end = min(len(idx), pos + EVENT_WINDOW)
                r = panel.pct[sym].iloc[pos] - market_pct.iloc[pos]
                ann.iloc[pos:end] = r
                att.iloc[pos:end] = att_series.iloc[pos:end]
            out["announce_ret_1d"][sym] = ann
            out["attention_20"][sym] = att

    return {k: v.astype("float32").replace([np.inf, -np.inf], np.nan) for k, v in out.items()}
