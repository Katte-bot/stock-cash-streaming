"""市场状态分组表现：上行 / 震荡 / 下行。

状态划分（只用当日及之前信息）：
- 基准等权净值过去 20 日涨幅 > +3%  → 上行
- 过去 20 日涨幅 < -3%             → 下行
- 其余                              → 震荡

输出各状态下的策略日均收益（按 252 日算术年化）、波动、胜率与超额。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest.engine import DEFAULT_COST_RATE
from ..research.universe import C0
from ..storage.repository import Repository


def run_regime(repo: Repository, universe_id: str = C0,
               cost_model: str = "a_share", limit_model: str = "board") -> pd.DataFrame:
    from .experiment import B3_WEIGHTS, _evaluate, _panel, score_from_weights

    symbols, panel = _panel(repo, universe_id)
    score = score_from_weights(panel["piv"], panel["close"], B3_WEIGHTS,
                               panel["sector_map"], neutral=False)
    res = _evaluate(repo, score, panel, symbols, None, None,
                    top_k=12, cost_rate=DEFAULT_COST_RATE, rebalance="monthly",
                    hold_buffer=1.0, weighting="vol_inv", vol_target=0.25,
                    cost_model=cost_model, limit_model=limit_model)
    nav, bench = res["nav"], res["benchmark"]
    ret_s = nav.pct_change().fillna(0.0)
    ret_b = bench.pct_change().fillna(0.0)
    trend = bench.pct_change(20)
    regime = pd.Series("震荡", index=bench.index)
    regime[trend > 0.03] = "上行"
    regime[trend < -0.03] = "下行"
    regime[trend.isna()] = "未定"

    rows: list[dict] = []
    for name in ("上行", "震荡", "下行"):
        mask = regime == name
        n = int(mask.sum())
        if n < 20:
            continue
        s, b = ret_s[mask], ret_b[mask]
        rows.append({
            "regime": name,
            "days": n,
            "share": float(mask.mean()),
            "strategy_ann": float(s.mean() * 252),
            "strategy_vol": float(s.std() * np.sqrt(252)),
            "benchmark_ann": float(b.mean() * 252),
            "excess_ann": float((s.mean() - b.mean()) * 252),
            "win_rate": float((s > 0).mean()),
        })
    return pd.DataFrame(rows)
