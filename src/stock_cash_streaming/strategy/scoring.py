"""横截面打分工具：缩尾、z-score、行业中性化、因子合成。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..features.registry import get as factor_spec


def winsorize(df: pd.DataFrame, lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    if df.empty:
        return df
    lo = df.quantile(lower, axis=1)
    hi = df.quantile(upper, axis=1)
    return df.clip(lo, hi, axis=0)


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0, np.nan)
    return (df.sub(mu, axis=0)).div(sd, axis=0)


def industry_neutral(z: pd.DataFrame, sector_map: dict[str, str]) -> pd.DataFrame:
    """行业内去均值（优先申万一级，缺失回退 UNKNOWN）。"""
    if z.empty:
        return z
    labels = pd.Series({c: sector_map.get(c, "UNKNOWN") for c in z.columns})
    means = z.T.groupby(labels).transform("mean").T
    return z - means


def standardize(df: pd.DataFrame, sector_map: dict[str, str] | None = None,
                neutral: bool = True) -> pd.DataFrame:
    z = zscore(winsorize(df))
    if neutral and sector_map:
        z = industry_neutral(z, sector_map)
    return z.replace([np.inf, -np.inf], np.nan)


def composite(factors: dict[str, pd.DataFrame], weights: dict[str, float],
              sector_map: dict[str, str] | None = None, neutral: bool = True,
              require_all: bool = False) -> pd.DataFrame:
    """按注册表方向合成多因子分数（分数越高越优先）。"""
    total: pd.DataFrame | None = None
    valid: pd.DataFrame | None = None
    for name, w in weights.items():
        if w == 0 or name not in factors:
            continue
        df = factors[name]
        spec = factor_spec(name)
        sign = 1.0 if (spec is None or spec.higher_is_better) else -1.0
        z = standardize(df, sector_map, neutral)
        if total is None:
            total = z * (w * sign)
            valid = z.notna()
        else:
            total = total.add(z * (w * sign), fill_value=0.0)
            valid = valid & z.notna() if require_all else valid | z.notna()
    if total is None:
        return pd.DataFrame()
    missing = ~valid
    return total.mask(missing)


def apply_mask(score: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """把不可交易的标的置为 NaN（引擎会跳过 NaN）。"""
    if score.empty or mask.empty:
        return score
    return score.where(mask.reindex(index=score.index, columns=score.columns).fillna(False))


def top_symbols(score_row: pd.Series, k: int) -> list[str]:
    s = score_row.dropna().sort_values(ascending=False)
    return list(s.index[:k])
