"""策略实验室：批量构造评分、样本内/样本外评估、参数搜索。

结论来源见 docs/research_factor_diagnostics.md：本样本 15 个因子 IC 全部为负，
因此实验室的重点是「反向/防御型」组合，以及不同加权、频率、篮子趋势择时的组合。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..backtest.engine import run_backtest
from ..storage.repository import Repository

# 因子分组：核心低风险因子 / 技术因子 / 资金流因子
CORE = ["vol_20", "turnover_5", "amount_20d", "ret_20", "boll_pos"]
TECH = ["ret_5", "ret_20", "ma5_ratio", "ma20_ratio", "boll_pos", "rsi14", "macd_hist",
        "vol_20", "turnover_5", "ma60_ratio", "amount_20d"]
FLOW = ["main_net_ratio_5d", "inflow_days_5", "institution_trend", "xl_ratio_5d"]


@dataclass
class Panel:
    piv: dict[str, pd.DataFrame]
    close: pd.DataFrame
    ret: pd.DataFrame
    pct: pd.DataFrame
    sector: pd.Series  # symbol -> sector_code
    st_symbols: set[str]

    def as_engine_panel(self) -> dict:
        return {"close": self.close, "ret": self.ret, "pct": self.pct, "piv": self.piv,
                "sector_map": self.sector.to_dict(), "st_symbols": self.st_symbols}


def load_panel(repo: Repository, version: str = "v2") -> Panel:
    names = sorted(set(CORE + TECH + FLOW))
    placeholders = ",".join("?" for _ in names)
    rows = repo.query(
        f"SELECT trade_date, symbol, factor, value FROM fact_factor "
        f"WHERE version=? AND factor IN ({placeholders})",
        tuple([version] + names),
    )
    fdf = pd.DataFrame([dict(r) for r in rows])
    piv = {
        f: fdf[fdf["factor"] == f].pivot(index="trade_date", columns="symbol", values="value")
        for f in names
    }
    q = pd.DataFrame([dict(r) for r in repo.query(
        "SELECT symbol, trade_date, close, pct_chg, amount, turnover FROM fact_quote")])
    close = q.pivot(index="trade_date", columns="symbol", values="close").sort_index()
    pct = q.pivot(index="trade_date", columns="symbol", values="pct_chg").sort_index()
    ret = close.pct_change().replace([np.inf, -np.inf], np.nan)

    # 仅补充 fact_factor 中没有的派生因子；max_ret_20 / turnover_std_20 / amihud_20 直接取 v2
    piv["downside_vol_20"] = ret.clip(upper=0).rolling(20).std() * np.sqrt(252)
    piv["ret_60"] = close.pct_change(60) * 100

    sector = {}
    for r in repo.query("SELECT symbol, sector_code FROM dim_sector_member"):
        sector.setdefault(r["symbol"], r["sector_code"])
    st_symbols = {
        r["symbol"] for r in repo.query("SELECT symbol, name FROM dim_stock")
        if "ST" in (r["name"] or "") or "退" in (r["name"] or "")
    }
    return Panel(piv=piv, close=close, ret=ret, pct=pct,
                 sector=pd.Series(sector), st_symbols=st_symbols)


def _rank(x: pd.DataFrame) -> pd.DataFrame:
    return x.rank(axis=1, pct=True)


def _sector_rank(x: pd.DataFrame, sector: pd.Series) -> pd.DataFrame:
    """组内百分位：每个板块内部单独排名。"""
    out = pd.DataFrame(np.nan, index=x.index, columns=x.columns)
    for sec in sector.dropna().unique():
        cols = [c for c in x.columns if sector.get(c) == sec]
        if cols:
            out[cols] = x[cols].rank(axis=1, pct=True)
    return out


def build_score(name: str, panel: Panel, min_amount: float = 1e8,
                st_symbols: set[str] | None = None) -> pd.DataFrame:
    """根据名称构造评分矩阵（越高越优先），并施加流动性/ST 过滤。"""
    piv, close = panel.piv, panel.close
    idx, cols = close.index, close.columns

    weights: dict[str, float]
    sector_neutral = name.endswith("_sn")
    if name.startswith("core2"):
        # 最终策略权重（与 backtest.engine.DEFENSIVE_WEIGHTS 一致）
        weights = {"vol_20": 0.20, "turnover_5": 0.13, "turnover_std_20": 0.06,
                   "amount_20d": 0.11, "max_ret_20": 0.17, "ret_20": 0.09,
                   "boll_pos": 0.06, "amihud_20": 0.18}
    elif name.startswith("core3"):
        # 在最终权重上叠加资金流因子（IC 较弱，权重较低）
        weights = {"vol_20": 0.18, "turnover_5": 0.12, "turnover_std_20": 0.05,
                   "amount_20d": 0.10, "max_ret_20": 0.15, "ret_20": 0.08,
                   "boll_pos": 0.05, "amihud_20": 0.17,
                   "main_net_ratio_5d": 0.06, "institution_trend": 0.04}
    elif name.startswith("core"):
        weights = {"vol_20": 0.30, "turnover_5": 0.25, "amount_20d": 0.20,
                   "ret_20": 0.15, "boll_pos": 0.10}
    elif name.startswith("volonly"):
        weights = {"vol_20": 1.0}
    elif name.startswith("tech"):
        weights = {f: 1.0 for f in TECH}
    elif name.startswith("all"):
        weights = {f: 1.0 for f in TECH + FLOW}
    elif name.startswith("coremix"):
        weights = {"vol_20": 0.28, "turnover_5": 0.22, "amount_20d": 0.18,
                   "ret_20": 0.12, "boll_pos": 0.10, "main_net_ratio_5d": 0.06,
                   "institution_trend": 0.04}
    else:
        raise ValueError(f"未知评分方案: {name}")

    # 方向：amihud_20 的 IC 为正（非流动性溢价），其余因子 IC 为负 → 取 1-rank
    positive = {"amihud_20"}

    total = 0.0
    score = pd.DataFrame(0.0, index=idx, columns=cols)
    for f, w in weights.items():
        mat = piv.get(f)
        if mat is None:
            continue
        mat = mat.reindex(index=idx, columns=cols)
        r = _sector_rank(mat, panel.sector) if sector_neutral else _rank(mat)
        score += w * (r if f in positive else (1 - r))
        total += w
    if total == 0:
        return pd.DataFrame(index=idx, columns=cols, dtype=float)
    score /= total

    valid = pd.DataFrame(True, index=idx, columns=cols)
    if "amount_20d" in piv:
        valid &= piv["amount_20d"].reindex(index=idx, columns=cols) > min_amount
    if "ma60_ratio" in piv:
        valid &= piv["ma60_ratio"].reindex(index=idx, columns=cols).notna()
    if st_symbols:
        for s in st_symbols:
            if s in valid.columns:
                valid[s] = False
    return score.where(valid)


def evaluate(repo: Repository, panel: Panel, score: pd.DataFrame, *, top_k: int, rebalance: str,
             weighting: str = "equal", hold_buffer: float = 1.0,
             basket_trend: int = 0, basket_trend_frac: float = 0.0,
             vol_target: float = 0.0, start: str | None = None,
             end: str | None = None, strategy_name: str = "lab") -> dict:
    return run_backtest(
        repo, top_k=top_k, rebalance=rebalance, hold_buffer=hold_buffer,
        score_override=score, weighting=weighting, basket_trend=basket_trend,
        basket_trend_frac=basket_trend_frac, vol_target=vol_target,
        start_date=start, end_date=end, persist=False, strategy=strategy_name,
        panel=panel.as_engine_panel(),
    )


IS_START, IS_END = "2018-01-01", "2022-12-31"
OOS_START, OOS_END = "2023-01-01", "2026-10-19"


def search(repo: Repository, panel: Panel,
           score_names: list[str] | None = None,
           top_ks: tuple[int, ...] = (5, 8, 12, 20, 30),
           rebalances: tuple[str, ...] = ("weekly", "monthly"),
           weightings: tuple[str, ...] = ("equal", "score", "vol_inv"),
           basket_trends: tuple[int, ...] = (0, 60),
           min_amounts: tuple[float, ...] = (5e7, 1e8, 2e8),
           ) -> pd.DataFrame:
    """网格搜索，返回含样本内/样本外指标的结果表（按样本外超额排序）。"""
    score_names = score_names or ["core", "core_sn", "coremix", "tech", "all"]
    records = []
    for sname, min_amt in itertools.product(score_names, min_amounts):
        score = build_score(sname, panel, min_amount=min_amt, st_symbols=panel.st_symbols)
        for k, reb, wt, bt in itertools.product(top_ks, rebalances, weightings, basket_trends):
            full = evaluate(repo, panel, score, top_k=k, rebalance=reb, weighting=wt,
                            basket_trend=bt, strategy_name=sname)
            oos = evaluate(repo, panel, score, top_k=k, rebalance=reb, weighting=wt,
                           basket_trend=bt, start=OOS_START, end=OOS_END, strategy_name=sname)
            records.append({
                "score": sname, "min_amt": min_amt, "top_k": k, "reb": reb,
                "wt": wt, "trend": bt,
                "ann": full["metrics"]["annual_return"],
                "sharpe": full["metrics"]["sharpe"],
                "dd": full["metrics"]["max_drawdown"],
                "excess": full["metrics"]["excess_annual"],
                "oos_ann": oos["metrics"]["annual_return"],
                "oos_sharpe": oos["metrics"]["sharpe"],
                "oos_dd": oos["metrics"]["max_drawdown"],
                "oos_excess": oos["metrics"]["excess_annual"],
            })
    df = pd.DataFrame(records)
    return df.sort_values(["oos_excess", "oos_sharpe"], ascending=False).reset_index(drop=True)
