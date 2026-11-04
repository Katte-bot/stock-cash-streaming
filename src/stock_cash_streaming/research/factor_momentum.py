"""因子动量 / 因子择时。

规则：
- 在每 20 个交易日（与月度调仓一致）评估候选策略近 3/6/12 个月表现；
- 表现用「TopK 组合的近 20 日收益」近似，严格使用评估日之前的数据；
- 得分 = 0.5·Sharpe(6M) + 0.3·Sharpe(12M) + 0.2·Sharpe(3M)，仅保留正得分；
- 权重归一化，单策略上限 40%；全部为负时退化为等权（防御倾向由候选策略本身决定）；
- 当期权重只用于下一调仓周期，避免未来函数。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..common.logging import logger

PERIOD = 20
MAX_WEIGHT = 0.40


def _monthly_returns(score: pd.DataFrame, close: pd.DataFrame, top_k: int) -> pd.Series:
    """按 20 交易日调仓的 TopK 等权组合收益序列（索引为调仓日）。"""
    dates = list(score.index)
    out: dict[str, float] = {}
    for i in range(0, len(dates) - PERIOD - 1, PERIOD):
        d = dates[i]
        row = score.loc[d].dropna()
        if row.empty:
            continue
        picks = list(row.sort_values(ascending=False).index[:top_k])
        entry = i + 1
        exit_ = min(i + 1 + PERIOD, len(dates) - 1)
        p0 = close.iloc[entry][picks]
        p1 = close.iloc[exit_][picks]
        ret = (p1 / p0 - 1).replace([np.inf, -np.inf], np.nan).dropna()
        if len(ret):
            out[d] = float(ret.mean())
    return pd.Series(out).sort_index()


def _sharpe(series: pd.Series, months: int) -> float:
    s = series.tail(months)
    if len(s) < 2 or s.std() == 0:
        return 0.0
    return float(s.mean() / s.std() * np.sqrt(12))


def build_weights(component_returns: dict[str, pd.Series],
                  eval_dates: list[str]) -> dict[str, dict[str, float]]:
    """为每个评估日计算权重（只使用该日之前的收益）。"""
    out: dict[str, dict[str, float]] = {}
    for d in eval_dates:
        scores: dict[str, float] = {}
        for sid, series in component_returns.items():
            hist = series[series.index < d]
            s3 = _sharpe(hist, 3)
            s6 = _sharpe(hist, 6)
            s12 = _sharpe(hist, 12)
            scores[sid] = 0.5 * s6 + 0.3 * s12 + 0.2 * s3
        pos = {k: max(0.0, v) for k, v in scores.items()}
        total = sum(pos.values())
        if total <= 0:
            n = len(component_returns)
            w = {k: 1.0 / n for k in component_returns} if n else {}
        else:
            w = {k: v / total for k, v in pos.items()}
            # 单策略上限，超出部分再分配
            for _ in range(3):
                over = {k: v for k, v in w.items() if v > MAX_WEIGHT}
                if not over:
                    break
                excess = sum(v - MAX_WEIGHT for v in over.values())
                for k in over:
                    w[k] = MAX_WEIGHT
                rest = {k: v for k, v in w.items() if k not in over}
                base = sum(rest.values())
                if base <= 0:
                    break
                for k in rest:
                    w[k] += excess * rest[k] / base
        out[d] = w
    return out


def build_meta_score(components: dict[str, pd.DataFrame], weights: dict[str, dict[str, float]],
                     dates: list[str]) -> pd.DataFrame:
    """把每个调仓周期的权重应用到对应日期区间。"""
    if not components:
        return pd.DataFrame()
    base = next(iter(components.values()))
    score = pd.DataFrame(np.nan, index=base.index, columns=base.columns, dtype="float64")
    eval_dates = sorted(weights)
    for i, d in enumerate(eval_dates):
        end = eval_dates[i + 1] if i + 1 < len(eval_dates) else None
        mask = score.index >= d
        if end:
            mask &= score.index < end
        w = weights[d]
        part = None
        for sid, weight in w.items():
            comp = components.get(sid)
            if comp is None or weight <= 0:
                continue
            z = comp.sub(comp.mean(axis=1), axis=0).div(comp.std(axis=1).replace(0, np.nan), axis=0)
            piece = z.loc[score.index[mask]] * weight
            part = piece if part is None else part.add(piece, fill_value=0)
        if part is not None:
            score.loc[score.index[mask]] = part
    return score


def compute(repo, panel, factors: dict[str, pd.DataFrame], component_ids: list[str],
            top_k: int = 12) -> tuple[pd.DataFrame, dict[str, dict[str, float]], dict]:
    """构建因子动量评分与权重历史。"""
    from ..strategy import library  # noqa: F401
    from ..strategy.registry import get as get_strategy

    components: dict[str, pd.DataFrame] = {}
    component_returns: dict[str, pd.Series] = {}
    for sid in component_ids:
        spec = get_strategy(sid)
        if spec is None or spec.builder is None:
            continue
        score = spec.builder(panel, factors, dict(spec.defaults))
        if score is None or score.empty:
            continue
        components[sid] = score
        component_returns[sid] = _monthly_returns(score, panel.close, top_k)

    dates = list(panel.close.index)
    eval_dates = [dates[i] for i in range(0, len(dates) - PERIOD, PERIOD)]
    weights = build_weights(component_returns, eval_dates)
    meta = build_meta_score(components, weights, dates)
    logger.info(f"因子动量：候选 {len(components)} 个，评估日 {len(weights)} 个")
    return meta, weights, components
