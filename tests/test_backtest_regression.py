"""回测核心口径的回归测试（不依赖数据库）。

覆盖：
- 指标计算与正态分位数；
- Deflated Sharpe 的方向性（强信号 > 噪声）；
- 权重平滑确实降低换手成本。
"""

import numpy as np
import pandas as pd

from stock_cash_streaming.backtest.engine import run_backtest
from stock_cash_streaming.backtest.metrics import (
    _norm_ppf, compute_metrics, deflated_sharpe,
)


def _nav_from_returns(rets: np.ndarray, start: str = "2020-01-01") -> pd.Series:
    dates = pd.bdate_range(start, periods=len(rets))
    return pd.Series(np.cumprod(1 + rets), index=dates.strftime("%Y-%m-%d"))


def test_norm_ppf():
    assert abs(_norm_ppf(0.5)) < 1e-9
    assert abs(_norm_ppf(0.975) - 1.959964) < 1e-4
    assert abs(_norm_ppf(0.025) + 1.959964) < 1e-4


def test_compute_metrics_known_series():
    # 252 个交易日、每日 +0.04% ≈ 年化 10.7%
    nav = _nav_from_returns(np.full(252, 0.0004))
    m = compute_metrics(nav)
    assert 0.09 < m["annual_return"] < 0.12
    assert m["max_drawdown"] <= 0
    assert abs(m["win_rate"] - 1.0) < 1e-9


def test_deflated_sharpe_direction():
    rng = np.random.default_rng(42)
    noise = rng.normal(0, 0.01, 1500)
    strong = rng.normal(0.0012, 0.008, 1500)
    d_noise = deflated_sharpe(_nav_from_returns(noise), n_trials=20)
    d_strong = deflated_sharpe(_nav_from_returns(strong), n_trials=20)
    assert 0 <= d_noise["dsr"] <= 1
    assert 0 <= d_strong["dsr"] <= 1
    assert d_strong["dsr"] > d_noise["dsr"]
    assert d_strong["dsr"] > 0.5


def _synthetic_panel(n_days: int = 250, n_syms: int = 20, seed: int = 7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days).strftime("%Y-%m-%d")
    syms = [f"{i:06d}" for i in range(1, n_syms + 1)]
    rets = rng.normal(0.0003, 0.015, size=(n_days, n_syms))
    close = pd.DataFrame(100 * np.cumprod(1 + rets, axis=0), index=dates, columns=syms)
    ret = close.pct_change().fillna(0.0)
    pct = ret * 100
    score = pd.DataFrame(rng.random((n_days, n_syms)), index=dates, columns=syms)
    panel = {
        "close": close, "ret": ret, "pct": pct,
        "piv": {"vol_20": pd.DataFrame(
            rng.uniform(10, 40, size=(n_days, n_syms)), index=dates, columns=syms)},
        "sector_map": {s: "S1" if i % 2 else "S2" for i, s in enumerate(syms)},
        "st_symbols": set(), "eligibility": None,
    }
    return panel, score


def test_weight_smoothing_reduces_cost():
    panel, score = _synthetic_panel()
    common = dict(
        panel=panel, score_override=score, persist=False, top_k=5,
        rebalance="weekly", hold_buffer=1.0, weighting="equal", vol_target=0.0,
        cost_model="flat", limit_model="flat", universe=list(panel["close"].columns),
    )
    base = run_backtest(None, **common)
    smooth = run_backtest(None, **common, weight_smoothing=0.8)
    assert smooth["metrics"]["cost_paid"] < base["metrics"]["cost_paid"]
    assert 0 < smooth["metrics"]["avg_exposure"] <= 1.0
