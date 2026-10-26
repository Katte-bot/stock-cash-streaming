"""策略变体实验：权重平滑、动态 TopK、动态波动目标。

全部基于 B3 评分（原策略 + 8% 估值）与真实成本，只改变执行层规则：
- 权重平滑：目标权重向昨日持仓靠拢，降低换手；
- 动态 TopK：市场高波动时扩大持仓数量（分散行业风险）；
- 动态波动目标：市场高波动时下调目标波动 20%。
"""

from __future__ import annotations

import pandas as pd

from ..backtest.engine import DEFAULT_COST_RATE
from ..research.universe import C0
from ..storage.repository import Repository

VARIANTS: dict[str, dict] = {
    "V0 基线": {},
    "V1 平滑0.3": {"weight_smoothing": 0.3},
    "V2 平滑0.5": {"weight_smoothing": 0.5},
    "V3 平滑0.7": {"weight_smoothing": 0.7},
    "V4 动态TopK": {"dynamic_top_k": True},
    "V5 动态波动目标": {"dynamic_vol_target": True},
    "V6 平滑0.3+动态TopK": {"weight_smoothing": 0.3, "dynamic_top_k": True},
    "V7 平滑0.3+动态波动": {"weight_smoothing": 0.3, "dynamic_vol_target": True},
}


def run_variants(repo: Repository, universe_id: str = C0,
                 cost_model: str = "a_share", limit_model: str = "board") -> pd.DataFrame:
    from .experiment import B3_WEIGHTS, _panel, _row, score_from_weights

    symbols, panel = _panel(repo, universe_id)
    score = score_from_weights(panel["piv"], panel["close"], B3_WEIGHTS,
                               panel["sector_map"], neutral=False)
    rows: list[dict] = []
    for name, extra in VARIANTS.items():
        rows.append(_row(repo, name, score, panel, symbols,
                         top_k=12, cost_rate=DEFAULT_COST_RATE, rebalance="monthly",
                         hold_buffer=1.0, weighting="vol_inv", vol_target=0.25,
                         cost_model=cost_model, limit_model=limit_model, **extra))
    return pd.DataFrame(rows)
