"""因子 IC / RankIC / ICIR 与分组收益。"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from ..common.logging import logger
from ..storage.repository import Repository

HORIZONS = (1, 5, 20)
SAMPLE_EVERY = 5  # 每 5 个交易日采样，降低计算量


def _row_rank_ic(f: pd.DataFrame, fwd: pd.DataFrame, min_n: int = 30) -> pd.Series:
    a = f.rank(axis=1)
    b = fwd.rank(axis=1)
    a = a.sub(a.mean(axis=1), axis=0)
    b = b.sub(b.mean(axis=1), axis=0)
    num = (a * b).sum(axis=1)
    den = np.sqrt((a ** 2).sum(axis=1) * (b ** 2).sum(axis=1)).replace(0, np.nan)
    corr = num / den
    n = f.notna().sum(axis=1)
    return corr.where(n >= min_n)


def _sample_index(index: pd.Index) -> pd.Index:
    return index[::SAMPLE_EVERY]


def compute_ic(panel, factors: dict[str, pd.DataFrame], horizon: int = 5,
               min_n: int | None = None, names: list[str] | None = None) -> pd.DataFrame:
    close = panel.close
    fwd = close.pct_change(horizon).shift(-horizon)
    if min_n is None:
        min_n = max(5, min(30, int(close.shape[1] * 0.3)))
    out: dict[str, pd.Series] = {}
    for name, f in factors.items():
        if names and name not in names:
            continue
        if isinstance(f, pd.DataFrame):
            out[name] = _row_rank_ic(f, fwd, min_n)
    return pd.DataFrame(out)


def summarize(ic_df: pd.DataFrame) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in ic_df.columns:
        s = ic_df[c].dropna()
        if len(s) < 10:
            continue
        out[c] = {
            "mean_ic": round(float(s.mean()), 4),
            "std_ic": round(float(s.std()), 4),
            "icir": round(float(s.mean() / s.std()), 3) if s.std() else 0.0,
            "positive_ratio": round(float((s > 0).mean()), 3),
            "n": int(len(s)),
        }
    return out


def quantile_returns(panel, factor: pd.DataFrame, horizon: int = 20,
                     q: int = 10) -> pd.DataFrame:
    """分位数组合的远期收益（研究展示，不做多空交易）。"""
    close = panel.close
    fwd = close.pct_change(horizon).shift(-horizon)
    dates = list(factor.index[::20])
    rows: list[dict] = []
    for d in dates:
        if d not in fwd.index:
            continue
        f = factor.loc[d].dropna()
        r = fwd.loc[d].reindex(f.index).dropna()
        if len(r) < q * 5:
            continue
        ranks = f.reindex(r.index).rank(pct=True)
        for g in range(q):
            lo, hi = g / q, (g + 1) / q
            mask = (ranks > lo) & (ranks <= hi) if g else (ranks >= lo) & (ranks <= hi)
            val = r[mask].mean()
            if pd.notna(val):
                rows.append({"trade_date": d, "group": g + 1, "ret": float(val)})
    if not rows:
        return pd.DataFrame(columns=["trade_date", "group", "ret"])
    df = pd.DataFrame(rows)
    return df.pivot(index="trade_date", columns="group", values="ret")


def persist_ic(repo: Repository, universe: str, ic_df: pd.DataFrame,
               horizon: int) -> int:
    rows = []
    for d in _sample_index(ic_df.index):
        for c in ic_df.columns:
            v = ic_df.at[d, c]
            if pd.isna(v):
                continue
            rows.append({"factor": c, "universe_id": universe, "trade_date": d,
                         "horizon": horizon, "ic": float(v), "rank_ic": float(v), "n": None})
    if rows:
        repo.upsert("fact_factor_ic", rows, ["factor", "universe_id", "trade_date", "horizon"])
    return len(rows)


def run_ic(repo: Repository, universe: str = "A0", start: str | None = None,
           end: str | None = None, horizons: tuple[int, ...] = HORIZONS,
           persist: bool = True) -> dict:
    """计算指定池全部因子的 IC 摘要。"""
    from ..features.cache import load_cached_factors

    panel, factors, cache_info = load_cached_factors(repo, universe, start, end)
    result: dict[str, dict] = {}
    for h in horizons:
        ic_df = compute_ic(panel, factors, horizon=h)
        if persist:
            persist_ic(repo, universe, ic_df, h)
        result[f"h{h}"] = summarize(ic_df)
    logger.info(f"IC 计算完成：{universe}，因子 {len(factors)} 个，缓存 {cache_info.get('cache')}")
    return {"universe": universe, "cache": cache_info, "horizons": result,
            "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
