"""无未来函数校验：截断到 t 计算的结果，应与全量计算在 t 处一致。"""

import numpy as np
import pandas as pd

from stock_cash_streaming.features.engine import compute_flow, compute_technical


def _quotes(periods: int = 80, seed: int = 0) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=periods).strftime("%Y-%m-%d")
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.normal(0, 0.1, periods))
    return pd.DataFrame(
        {
            "symbol": "600000",
            "trade_date": dates,
            "close": close,
            "amount": 1e8,
            "turnover": 1.0,
            "pct_chg": pd.Series(close).pct_change() * 100,
        }
    )


def test_technical_no_lookahead():
    q = _quotes()
    full = compute_technical(q)
    t = "2024-03-01"
    trunc = compute_technical(q[q["trade_date"] <= t])
    a = full[full["trade_date"] == t].iloc[0]
    b = trunc[trunc["trade_date"] == t].iloc[0]
    for col in ["ret_5", "ret_20", "ma5_ratio", "ma20_ratio", "boll_pos",
                "rsi14", "macd_hist", "vol_20", "turnover_5", "ma60_ratio",
                "amount_20d", "max_ret_20", "turnover_std_20", "amihud_20"]:
        assert np.isclose(a[col], b[col], equal_nan=True), f"未来函数泄漏: {col}"


def test_flow_no_lookahead():
    q = _quotes(periods=60)
    f = pd.DataFrame(
        {
            "symbol": "600000",
            "trade_date": q["trade_date"],
            "main_net": np.linspace(-1e7, 1e7, len(q)),
            "xl_net": np.linspace(-5e6, 5e6, len(q)),
        }
    )
    full = compute_flow(q, f)
    t = "2024-02-20"
    trunc = compute_flow(q[q["trade_date"] <= t], f[f["trade_date"] <= t])
    a = full[full["trade_date"] == t].iloc[0]
    b = trunc[trunc["trade_date"] == t].iloc[0]
    for col in ["main_net_5d", "main_net_ratio_5d", "inflow_days_5",
                "institution_trend", "xl_ratio_5d"]:
        assert np.isclose(a[col], b[col], equal_nan=True), f"未来函数泄漏: {col}"
