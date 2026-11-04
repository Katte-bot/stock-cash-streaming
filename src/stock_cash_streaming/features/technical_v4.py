"""v4 技术/交易因子（波动、Beta、特质波动、残差动量、趋势、反转、容量）。

计算口径（全部只用 t 及以前数据）：
- 市场收益 = 面板内等权日收益；行业收益 = 行业内等权日收益；
- 残差 = 个股收益 − β·市场收益 − 行业残差均值（行业固定效应）；
- 残差动量 = 残差累计（剔除最近 21 日）÷ 残差波动（信息比）；
- 不落地日频，按需计算并缓存（见 features/cache.py）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .panel import Panel

TRADING_DAYS = 252
RESID_WINDOW = 252
RESID_SKIP = 21


def _annual_vol(ret: pd.DataFrame, n: int, min_periods: int) -> pd.DataFrame:
    return ret.rolling(n, min_periods=min_periods).std() * np.sqrt(TRADING_DAYS)


def market_and_sector_returns(panel: Panel) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    """返回市场收益、行业收益宽表、行业代码（与股票列对齐）。"""
    ret = panel.ret
    market = ret.mean(axis=1, skipna=True)
    sec_codes = pd.Series(
        {sym: panel.sector_map.get(sym, "UNKNOWN") for sym in ret.columns}
    )
    sector_ret = panel.sector_return()
    wide = sector_ret.reindex(columns=sec_codes.values)
    wide.columns = ret.columns
    return market, wide, sec_codes


def residual_returns(panel: Panel, market: pd.Series, sector: pd.DataFrame,
                     window: int = RESID_WINDOW, min_periods: int = 120) -> pd.DataFrame:
    """市场 + 行业（等权均值）双重剔除后的残差收益。"""
    ret = panel.ret
    var_m = market.rolling(window, min_periods=min_periods).var()
    cov = ret.rolling(window, min_periods=min_periods).cov(market)
    beta = cov.div(var_m, axis=0).replace([np.inf, -np.inf], np.nan)
    resid_mkt = ret - beta.mul(market, axis=0)
    # 行业固定效应：行业内残差均值（用转置分组，避免逐列循环）
    sec_mean = resid_mkt.T.groupby(sector.columns.map(
        lambda s: panel.sector_map.get(s, "UNKNOWN"))).transform("mean").T
    return (resid_mkt - sec_mean).astype("float32")


def compute_v4(panel: Panel) -> dict[str, pd.DataFrame]:
    """返回 v4 因子宽表字典。"""
    ret = panel.ret
    close = panel.close
    out: dict[str, pd.DataFrame] = {}

    out["vol_60"] = _annual_vol(ret, 60, 30)
    out["vol_120"] = _annual_vol(ret, 120, 60)
    out["vol_250"] = _annual_vol(ret, 250, 120)

    neg = ret.where(ret < 0)
    out["downside_vol_120"] = neg.rolling(120, min_periods=60).std() * np.sqrt(TRADING_DAYS)

    market, sector, _ = market_and_sector_returns(panel)
    var_m = market.rolling(250, min_periods=120).var()
    cov = ret.rolling(250, min_periods=120).cov(market)
    beta = cov.div(var_m, axis=0).replace([np.inf, -np.inf], np.nan)
    out["beta_250"] = beta

    resid = residual_returns(panel, market, sector)
    out["ivol_60"] = resid.rolling(60, min_periods=30).std() * np.sqrt(TRADING_DAYS)

    out["ret_60"] = close.pct_change(60) * 100
    out["ret_120"] = close.pct_change(120) * 100
    out["ret_200"] = close.pct_change(200) * 100

    shifted = resid.shift(RESID_SKIP)
    for window, name in ((126, "resid_mom_126"), (252, "resid_mom_252")):
        cum = shifted.rolling(window, min_periods=int(window * 0.8)).sum()
        vol = shifted.rolling(window, min_periods=int(window * 0.8)).std()
        out[name] = cum / vol.replace(0, np.nan) / np.sqrt(window)

    for n in (5, 10, 20):
        out[f"resid_ret_{n}"] = resid.rolling(n, min_periods=max(3, n // 2)).sum() * 100

    for n in (60, 120):
        ma = close.rolling(n, min_periods=int(n * 0.8)).mean()
        rising = ma > ma.shift(5)
        out[f"ts_trend_{n}"] = ((close > ma) & rising).astype("float32")

    out["dist_high_60"] = close / close.rolling(60, min_periods=30).max() - 1

    if panel.open is not None and panel.high is not None and panel.low is not None:
        span = (panel.high - panel.low).replace(0, np.nan)
        lower = (panel.open.combine(panel.close, np.minimum) - panel.low) / span
        out["lower_shadow_20"] = lower.rolling(20, min_periods=10).mean()

    out["volume_shrink_5_20"] = (
        panel.volume.rolling(5, min_periods=3).mean()
        / panel.volume.rolling(20, min_periods=10).mean().replace(0, np.nan)
    )

    amihud_daily = (ret.abs() / panel.amount.replace(0, np.nan)) * 1e8
    out["amihud_std_20"] = amihud_daily.rolling(20, min_periods=10).std()

    return {k: v.astype("float32").replace([np.inf, -np.inf], np.nan) for k, v in out.items()}


def compute_v2_from_panel(panel: Panel) -> dict[str, pd.DataFrame]:
    """面板口径重算 v2 技术因子（资金流因子仍读库）。"""
    ret, close = panel.ret, panel.close
    out: dict[str, pd.DataFrame] = {}

    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()

    out["ret_5"] = close.pct_change(5) * 100
    out["ret_20"] = close.pct_change(20) * 100
    out["ma5_ratio"] = close / ma5 - 1
    out["ma20_ratio"] = close / ma20 - 1
    out["boll_pos"] = (close - (ma20 - 2 * std20)) / (4 * std20)
    delta = close.diff()
    up = delta.clip(lower=0).rolling(14).mean()
    down = (-delta.clip(upper=0)).rolling(14).mean()
    out["rsi14"] = 100 - 100 / (1 + up / down.replace(0, np.nan))
    out["macd_hist"] = (dif - dea) * 2
    out["vol_20"] = _annual_vol(ret, 20, 10)
    out["turnover_5"] = panel.turnover.rolling(5).mean()
    out["ma60_ratio"] = close / close.rolling(60).mean() - 1
    out["amount_20d"] = panel.amount.rolling(20).mean()
    out["max_ret_20"] = panel.pct.rolling(20).max()
    out["turnover_std_20"] = panel.turnover.rolling(20).std()
    out["amihud_20"] = (ret.abs() / panel.amount.replace(0, np.nan)).rolling(20).mean() * 1e8
    return {k: v.astype("float32").replace([np.inf, -np.inf], np.nan) for k, v in out.items()}


def compute_all_panel_factors(panel: Panel) -> dict[str, pd.DataFrame]:
    """v2 技术 + v4 合并（资金流/基本面单独加载）。"""
    out = compute_v2_from_panel(panel)
    out.update(compute_v4(panel))
    return out
