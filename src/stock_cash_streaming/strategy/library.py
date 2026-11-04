"""首批策略库：价值质量、残差动量、保守投资、PEAD、因子动量、防御基线。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..features.panel import Panel
from .registry import StrategySpec, register
from .scoring import composite

VALUE_QUALITY_WEIGHTS = {
    # 价值 50%
    "ep_ttm": 0.12, "bp_mrq": 0.12, "cfo_yield": 0.10,
    "fcf_yield": 0.08, "div_yield_3y": 0.08,
    # 质量 50%
    "roe_ttm": 0.12, "roa_ttm": 0.10, "roic": 0.10,
    "cfo_to_np": 0.10, "gross_margin": 0.05, "accruals": 0.03,
}
RESIDUAL_WEIGHTS = {"resid_mom_252": 0.6, "resid_mom_126": 0.4}
CONSERVATIVE_WEIGHTS = {
    "asset_growth": 0.35, "capex_to_assets": 0.25,
    "investment_intensity": 0.20, "asset_turnover_change": 0.20,
}
PEAD_WEIGHTS = {
    "sue": 0.40, "revenue_surprise": 0.25,
    "announce_ret_1d": 0.25, "attention_20": 0.10,
}
LOW_VOL_WEIGHTS = {"vol_120": 0.40, "downside_vol_120": 0.35, "ivol_60": 0.25}


def _coverage(factors: dict[str, pd.DataFrame], names: list[str], min_count: int) -> pd.DataFrame:
    count = None
    for n in names:
        df = factors.get(n)
        if df is None:
            continue
        ok = df.notna().astype("int8")
        count = ok if count is None else count + ok
    if count is None:
        return pd.DataFrame()
    return count >= min_count


def build_value_quality(panel: Panel, factors: dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
    weights = params.get("weights") or VALUE_QUALITY_WEIGHTS
    score = composite(factors, weights, panel.sector_map, neutral=True)
    if score.empty:
        return score
    mask = _coverage(factors, list(weights), int(params.get("min_factors", 4)))
    score = score.where(mask)
    # 剔除亏损企业：EP<=0 不进入价值组合
    ep = factors.get("ep_ttm")
    if ep is not None:
        score = score.mask(ep <= 0)
    return score


def build_residual_momentum(panel: Panel, factors: dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
    weights = params.get("weights") or RESIDUAL_WEIGHTS
    score = composite(factors, weights, panel.sector_map, neutral=True)
    if score.empty:
        return score
    mask = _coverage(factors, list(weights), int(params.get("min_factors", 2)))
    return score.where(mask)


def build_conservative(panel: Panel, factors: dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
    weights = params.get("weights") or CONSERVATIVE_WEIGHTS
    score = composite(factors, weights, panel.sector_map, neutral=True)
    if score.empty:
        return score
    mask = _coverage(factors, list(weights), int(params.get("min_factors", 2)))
    return score.where(mask)


def build_pead(panel: Panel, factors: dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
    weights = params.get("weights") or PEAD_WEIGHTS
    score = composite(factors, weights, panel.sector_map, neutral=True)
    if score.empty:
        return score
    event = factors.get("announce_ret_1d")
    if event is not None:
        score = score.where(event.notna())
    mask = _coverage(factors, list(weights), int(params.get("min_factors", 2)))
    return score.where(mask)


def build_low_vol(panel: Panel, factors: dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
    weights = params.get("weights") or LOW_VOL_WEIGHTS
    score = composite(factors, weights, panel.sector_map, neutral=True)
    if score.empty:
        return score
    mask = _coverage(factors, list(weights), int(params.get("min_factors", 2)))
    return score.where(mask)


def build_factor_momentum(panel: Panel, factors: dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
    """由 runner/factor_momentum 注入 component_scores 与 weights。"""
    components: dict[str, pd.DataFrame] = params.get("component_scores") or {}
    weights: dict[str, float] = params.get("weights") or {}
    if not components:
        return pd.DataFrame()
    total = None
    for sid, score in components.items():
        w = float(weights.get(sid, 0.0))
        if w <= 0 or score is None or score.empty:
            continue
        z = score.sub(score.mean(axis=1), axis=0).div(score.std(axis=1).replace(0, np.nan), axis=0)
        part = z * w
        total = part if total is None else total.add(part, fill_value=0.0)
    return total if total is not None else pd.DataFrame()


def build_custom(panel: Panel, factors: dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
    """自然语言因子配置：params.factor_weights = {因子名: 权重}。"""
    weights = params.get("factor_weights") or {}
    if not weights:
        return pd.DataFrame()
    score = composite(factors, weights, panel.sector_map,
                      neutral=bool(params.get("neutral", True)))
    if score.empty:
        return score
    mask = _coverage(factors, list(weights), int(params.get("min_factors", 3)))
    return score.where(mask)


def build_defensive(panel: Panel, factors: dict[str, pd.DataFrame], params: dict) -> pd.DataFrame:
    from ..backtest.engine import defensive_enhanced_score

    return defensive_enhanced_score(factors, panel.close, st_symbols=panel.st_symbols)


_DEFAULT_PORTFOLIO = {
    "top_k": 12, "rebalance": "monthly", "weighting": "equal",
    "hold_buffer": 1.0, "vol_target": 0.0,
}


def _register_all() -> None:
    register(StrategySpec(
        id="value_quality", name="价值质量", category="fundamental",
        description="估值（EP/BP/现金流/股息）与盈利能力（ROE/ROA/ROIC/现金利润匹配）行业中性合成，剔除亏损与高应计。",
        factors=list(VALUE_QUALITY_WEIGHTS),
        defaults={**_DEFAULT_PORTFOLIO, "top_k": 12},
        builder=build_value_quality,
    ))
    register(StrategySpec(
        id="residual_momentum", name="残差动量", category="price",
        description="剔除市场与行业等权收益后的残差强弱，6/12 个月信息比合成。",
        factors=list(RESIDUAL_WEIGHTS),
        defaults={**_DEFAULT_PORTFOLIO, "top_k": 12},
        builder=build_residual_momentum,
    ))
    register(StrategySpec(
        id="conservative_investment", name="保守投资", category="fundamental",
        description="低资产扩张、低资本开支强度、周转率改善，行业中性。",
        factors=list(CONSERVATIVE_WEIGHTS),
        defaults={**_DEFAULT_PORTFOLIO, "top_k": 12},
        builder=build_conservative,
    ))
    register(StrategySpec(
        id="pead", name="财报超预期漂移", category="event",
        description="SUE、营收惊喜、公告日异常收益与关注度合成；仅在公告后 60 个交易日内有效。",
        factors=list(PEAD_WEIGHTS),
        defaults={**_DEFAULT_PORTFOLIO, "top_k": 10},
        builder=build_pead,
    ))
    register(StrategySpec(
        id="factor_momentum", name="因子动量", category="meta",
        description="按候选策略近 3/6/12 个月表现动态分配权重，加权各策略横截面分数。",
        factors=[],
        defaults={**_DEFAULT_PORTFOLIO, "top_k": 12,
                  "components": ["value_quality", "residual_momentum",
                                 "conservative_investment", "pead", "defensive"]},
        builder=build_factor_momentum,
    ))
    register(StrategySpec(
        id="low_vol_multidim", name="多维低波动", category="price",
        description="普通波动、下行波动与特质波动合成，对照现有防御策略。",
        factors=list(LOW_VOL_WEIGHTS),
        defaults={**_DEFAULT_PORTFOLIO, "top_k": 12},
        builder=build_low_vol,
    ))
    register(StrategySpec(
        id="defensive", name="防御增强（基线）", category="baseline",
        description="现有生产策略：低波动、低流动性、低彩票、低换手等加权，月频 Top12。",
        factors=["vol_20", "turnover_5", "turnover_std_20", "amount_20d",
                 "max_ret_20", "ret_20", "boll_pos", "amihud_20", "ma60_ratio"],
        defaults={**_DEFAULT_PORTFOLIO, "top_k": 12, "weighting": "vol_inv",
                  "vol_target": 0.25},
        builder=build_defensive,
    ))
    register(StrategySpec(
        id="nl_factor", name="自然语言因子配置", category="meta",
        description="由自然语言生成的因子权重配置（白名单校验后确定性回测）。",
        factors=[],
        defaults={**_DEFAULT_PORTFOLIO, "top_k": 10, "min_factors": 3},
        builder=build_custom,
    ))


_register_all()
