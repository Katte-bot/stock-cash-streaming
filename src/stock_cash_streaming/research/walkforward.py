"""滚动验证（walk-forward）：训练 3 年 → 测试 1 年 → 前滚。

说明：
- 本平台策略参数是规则化的（不做全样本寻优），walk-forward 只用于检查
  「规则在时间上是否稳定」，因此训练窗口只作为参照期，不参与调参；
- 每个测试窗口独立计算年化/夏普/回撤/超额，避免全样本指标掩盖阶段性失效。
"""

from __future__ import annotations

import pandas as pd

from ..backtest.metrics import compute_metrics

TRAIN_YEARS = 3
TEST_YEARS = 1


def _slice(series: pd.Series, start: str, end: str) -> pd.Series:
    return series[(series.index >= start) & (series.index <= end)]


def walk_forward(nav: pd.Series, bench: pd.Series,
                 train_years: int = TRAIN_YEARS,
                 test_years: int = TEST_YEARS) -> list[dict]:
    """按自然年滚动切分，返回每段测试窗口指标。"""
    if nav.empty or bench.empty or train_years < 1 or test_years < 1:
        return []
    years = sorted({int(y) for y in pd.to_datetime(pd.Index(nav.index)).year})
    if len(years) < train_years + test_years:
        return []

    out: list[dict] = []
    for i in range(train_years, len(years), test_years):
        test_years_list = years[i:i + test_years]
        if not test_years_list:
            break
        train_years_list = years[max(0, i - train_years):i]
        train_start = f"{train_years_list[0]}-01-01"
        train_end = f"{train_years_list[-1]}-12-31"
        test_start = f"{test_years_list[0]}-01-01"
        test_end = f"{test_years_list[-1]}-12-31"

        tr_nav = _slice(nav, train_start, train_end)
        tr_bench = _slice(bench, train_start, train_end)
        te_nav = _slice(nav, test_start, test_end)
        te_bench = _slice(bench, test_start, test_end)
        if len(te_nav) < 20:
            continue

        tr = compute_metrics(tr_nav, tr_bench) if len(tr_nav) > 2 else {}
        te = compute_metrics(te_nav, te_bench)
        out.append({
            "train_start": train_start, "train_end": train_end,
            "test_start": test_start, "test_end": test_end,
            "train_annual": round(float(tr.get("annual_return", 0.0)), 4),
            "test_annual": round(float(te.get("annual_return", 0.0)), 4),
            "test_sharpe": round(float(te.get("sharpe", 0.0)), 3),
            "test_max_drawdown": round(float(te.get("max_drawdown", 0.0)), 4),
            "test_excess": round(float(te.get("excess_annual", 0.0)), 4),
            "passed": bool(te.get("annual_return", 0.0) > 0
                           and te.get("excess_annual", 0.0) > 0),
        })
    return out


def summarize(segments: list[dict]) -> dict:
    """汇总 walk-forward 结果：通过率与测试窗口均值。"""
    if not segments:
        return {}
    passed = sum(1 for s in segments if s["passed"])
    return {
        "segments": len(segments),
        "passed": passed,
        "pass_rate": round(passed / len(segments), 3),
        "avg_test_annual": round(sum(s["test_annual"] for s in segments) / len(segments), 4),
        "avg_test_excess": round(sum(s["test_excess"] for s in segments) / len(segments), 4),
    }
