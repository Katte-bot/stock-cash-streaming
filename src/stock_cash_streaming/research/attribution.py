"""组合收益归因：分年度、分行业、个股贡献。

口径：
- 使用与回测完全一致的 B3 评分、TopK、1/vol 加权、25% 波动目标与真实成本；
- 每日贡献 = 当日持仓权重 × 当日个股收益（毛贡献，未扣交易成本）；
- 年度贡献 = 该年每日贡献之和；与净值年收益的差额主要是交易成本与复利效应。
"""

from __future__ import annotations

import pandas as pd

from ..backtest.engine import (
    DEFAULT_COST_RATE, FACTOR_VERSION, prepare_panel, run_backtest,
)
from ..research.universe import C0, eligibility_panel, universe_members
from ..storage.repository import Repository


def run_attribution(repo: Repository, universe_id: str = C0, top_k: int = 12,
                    cost_model: str = "a_share", limit_model: str = "board") -> dict:
    from .experiment import B3_WEIGHTS, TECH_NAMES, score_from_weights

    members = universe_members(repo, universe_id)
    if not members:
        return {"error": f"股票池 {universe_id} 为空"}
    symbols = list(members)
    eligibility = eligibility_panel(repo, symbols)
    panel = prepare_panel(repo, TECH_NAMES, FACTOR_VERSION, universe=symbols,
                          eligibility=eligibility, include_fundamentals=True,
                          universe_sector_map=members)
    score = score_from_weights(panel["piv"], panel["close"], B3_WEIGHTS,
                               panel["sector_map"], neutral=False)
    res = run_backtest(
        repo, score_override=score, panel=panel, universe=symbols,
        eligibility=panel.get("eligibility"), persist=False, strategy="research",
        top_k=top_k, cost_rate=DEFAULT_COST_RATE, rebalance="monthly",
        hold_buffer=1.0, weighting="vol_inv", vol_target=0.25,
        cost_model=cost_model, limit_model=limit_model,
        universe_sector_map=members, return_weights=True,
    )
    contrib: pd.DataFrame = res["contrib"]
    nav: pd.Series = res["nav"]

    years = contrib.index.str[:4]
    # 行业贡献
    sectors = sorted(set(members.values()))
    sector_daily = {}
    for sec in sectors:
        cols = [s for s in contrib.columns if members.get(s) == sec]
        if cols:
            sector_daily[sec] = contrib[cols].sum(axis=1)
    sector_df = pd.DataFrame(sector_daily)
    sector_year = sector_df.groupby(years).sum()

    # 个股贡献
    stock_year = contrib.groupby(years).sum()
    stock_total = contrib.sum().sort_values(ascending=False)

    # 净收益（与实验口径一致：日收益按年连乘）
    ret = nav.pct_change().fillna(0)
    yearly = ret.groupby(ret.index.str[:4]).apply(lambda x: float((1 + x).prod() - 1))

    # 成本按年：净值年收益 - 贡献合计（近似）
    gross = contrib.sum(axis=1).groupby(years).apply(lambda x: float((1 + x).prod() - 1))

    return {
        "universe": universe_id,
        "years": list(yearly.index),
        "yearly_return": yearly,
        "yearly_gross": gross,
        "sector_year": sector_year,
        "stock_year": stock_year,
        "stock_total": stock_total,
        "metrics": res["metrics"],
        "nav": nav,
        "contrib": contrib,
    }


def format_tables(res: dict, top_n: int = 15) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """整理成三张可直接落 CSV 的表：年度、行业年度、个股年度（前/后 N）。"""
    yearly = pd.DataFrame({
        "year": res["years"],
        "net_return": res["yearly_return"].values,
        "gross_contrib": res["yearly_gross"].values,
        "cost_drag": (res["yearly_gross"] - res["yearly_return"]).values,
    })
    sector_year = res["sector_year"].T
    sector_year.index.name = "sector"
    stock_year = res["stock_year"].T
    totals = res["stock_total"]
    keep = list(totals.head(top_n).index) + list(totals.tail(top_n).index)
    keep = list(dict.fromkeys(keep))
    stock_year = stock_year.loc[keep]
    stock_year.index.name = "symbol"
    return yearly, sector_year, stock_year
