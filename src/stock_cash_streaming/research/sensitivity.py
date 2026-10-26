"""参数敏感性与多重检验。

做两件事：
1. 单变量敏感性：TopK / 持仓缓冲 / 波动目标 / 调仓频率 / 加权方式；
2. Deflated Sharpe：把同一轮所有候选方案的夏普作为试验集合，修正择优偏差。
"""

from __future__ import annotations

import pandas as pd

from ..backtest.engine import DEFAULT_COST_RATE
from ..backtest.metrics import deflated_sharpe
from ..research.universe import C0
from ..storage.repository import Repository


def run_cost_sensitivity(repo: Repository, universe_id: str = C0,
                         limit_model: str = "board") -> pd.DataFrame:
    """交易成本敏感性：flat 费率 5–30bp + A 股真实税费模型。"""
    from .experiment import B3_WEIGHTS, _panel, _row, score_from_weights

    symbols, panel = _panel(repo, universe_id)
    score = score_from_weights(panel["piv"], panel["close"], B3_WEIGHTS,
                               panel["sector_map"], neutral=False)
    base = dict(top_k=12, hold_buffer=1.0, vol_target=0.25, rebalance="monthly",
                weighting="vol_inv")
    rows: list[dict] = []
    for label, rate in (("5bp", 0.0005), ("10bp", 0.001), ("13bp 默认", 0.0013),
                        ("20bp", 0.002), ("30bp", 0.003)):
        row = _row(repo, f"flat {label}", score, panel, symbols,
                   cost_rate=rate, cost_model="flat", limit_model=limit_model, **base)
        row["param"] = "flat_cost"
        row["value"] = label
        rows.append(row)
    row = _row(repo, "A股税费模型", score, panel, symbols,
               cost_rate=DEFAULT_COST_RATE, cost_model="a_share",
               limit_model=limit_model, **base)
    row["param"] = "cost_model"
    row["value"] = "a_share"
    rows.append(row)
    return pd.DataFrame(rows)


def run_sensitivity(repo: Repository, universe_id: str = C0,
                    cost_model: str = "a_share", limit_model: str = "board") -> dict:
    """B3 评分下的单变量参数扫描 + Deflated Sharpe。"""
    from .experiment import B3_WEIGHTS, _evaluate, _panel, _row, score_from_weights

    symbols, panel = _panel(repo, universe_id)
    score = score_from_weights(panel["piv"], panel["close"], B3_WEIGHTS,
                               panel["sector_map"], neutral=False)

    base = dict(top_k=12, hold_buffer=1.0, vol_target=0.25,
                rebalance="monthly", weighting="vol_inv")
    configs: list[tuple[str, str, str, dict]] = [
        ("基线 Top12/月频/25%", "baseline", "12/1/25%/月频", {}),
    ]
    for v in (8, 10, 15, 20, 30):
        configs.append((f"TopK={v}", "top_k", str(v), {"top_k": v}))
    for v in (0.0, 2.0):
        configs.append((f"buffer={v:g}", "hold_buffer", f"{v:g}", {"hold_buffer": v}))
    for v in (0.0, 0.15, 0.20, 0.30):
        label = "关闭" if v == 0 else f"{v:.0%}"
        configs.append((f"波动目标={label}", "vol_target", label, {"vol_target": v}))
    for v, label in (("bimonthly", "双月"), ("quarterly", "季度")):
        configs.append((f"调仓={label}", "rebalance", label, {"rebalance": v}))
    configs.append(("等权", "weighting", "equal", {"weighting": "equal"}))

    rows: list[dict] = []
    for name, param, value, over in configs:
        kw = {**base, **over}
        row = _row(repo, name, score, panel, symbols, cost_rate=DEFAULT_COST_RATE,
                   cost_model=cost_model, limit_model=limit_model, **kw)
        row.update({"param": param, "value": value})
        rows.append(row)
    df = pd.DataFrame(rows)

    base_res = _evaluate(repo, score, panel, symbols, None, None,
                         top_k=12, cost_rate=DEFAULT_COST_RATE, rebalance="monthly",
                         hold_buffer=1.0, weighting="vol_inv", vol_target=0.25,
                         cost_model=cost_model, limit_model=limit_model)
    sharpes = [float(x) for x in df["sharpe"].dropna().tolist()]
    dsr = deflated_sharpe(base_res["nav"], trial_sharpes=sharpes,
                          n_trials=len(sharpes))
    return {"table": df, "dsr": dsr, "universe": universe_id}
