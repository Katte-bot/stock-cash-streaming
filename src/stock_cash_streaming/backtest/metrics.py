"""回测指标与因子 IC。"""

import math

import numpy as np
import pandas as pd

from ..common.logging import logger
from ..storage.repository import Repository


def compute_metrics(nav: pd.Series, benchmark: pd.Series | None = None) -> dict:
    """由净值序列计算常用指标。"""
    nav = nav.replace([np.inf, -np.inf], np.nan).dropna()
    if len(nav) < 2 or nav.iloc[0] <= 0:
        return {}
    ret = nav.pct_change().dropna()
    n = len(nav)
    years = n / 252
    total = nav.iloc[-1] / nav.iloc[0] - 1
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    vol = ret.std() * np.sqrt(252)
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() else np.nan
    dd_series = nav / nav.cummax() - 1
    max_dd = dd_series.min()
    calmar = ann / abs(max_dd) if max_dd else np.nan
    win = (ret > 0).mean()

    out = {
        "total_return": float(total),
        "annual_return": float(ann),
        "annual_vol": float(vol),
        "sharpe": float(sharpe),
        "max_drawdown": float(max_dd),
        "calmar": float(calmar),
        "win_rate": float(win),
        "days": int(n),
    }
    if benchmark is not None and len(benchmark) > 1:
        b = benchmark.dropna()
        b_total = b.iloc[-1] / b.iloc[0] - 1
        b_ann = (1 + b_total) ** (1 / years) - 1 if years > 0 else np.nan
        out["benchmark_annual"] = float(b_ann)
        out["excess_annual"] = float(ann - b_ann)
    return out


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """标准正态分位数（Acklam 近似，误差 < 1e-9）。"""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p <= 0 or p >= 1:
        return float("nan")
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def deflated_sharpe(nav: pd.Series, trial_sharpes: list[float] | None = None,
                    n_trials: int | None = None) -> dict:
    """Deflated Sharpe Ratio（Bailey & López de Prado, 2014）。

    用于修正「多次试验后择优」带来的选择偏差：
    - trial_sharpes 为同一研究过程中所有候选方案的**年化**夏普；
    - 返回 DSR（0-1，越高说明夏普越不可能是运气）。
    """
    ret = nav.pct_change().dropna()
    n = len(ret)
    if n < 30 or ret.std() == 0:
        return {}
    sr = float(ret.mean() / ret.std())          # 每期夏普
    skew = float(ret.skew())
    kurt = float(ret.kurtosis()) + 3.0           # 非超额峰度
    trials = [s / math.sqrt(252) for s in (trial_sharpes or [sr * math.sqrt(252)])]
    k = n_trials or len(trials)
    var_trials = float(np.var(trials, ddof=1)) if len(trials) > 1 else 0.0
    gamma = 0.5772156649
    sr0 = math.sqrt(max(var_trials, 0.0)) * (
        (1 - gamma) * _norm_ppf(1 - 1.0 / k) + gamma * _norm_ppf(1 - 1.0 / (k * math.e)))
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4.0 * sr ** 2))
    dsr = _norm_cdf((sr - sr0) * math.sqrt(n - 1) / denom)
    return {"dsr": float(dsr), "sharpe_annual": float(sr * math.sqrt(252)),
            "sr0_annual": float(sr0 * math.sqrt(252)), "n_trials": int(k),
            "n_obs": int(n), "skew": skew, "kurtosis": kurt}


def factor_ic(repo: Repository, factor: str, horizon: int = 5, version: str = "v1") -> dict | None:
    """计算单因子 RankIC（与未来 horizon 日收益的 Spearman 相关）。"""
    frows = repo.query(
        "SELECT trade_date, symbol, value FROM fact_factor WHERE factor=? AND version=?", (factor, version)
    )
    if not frows:
        return None
    fp = pd.DataFrame([dict(r) for r in frows]).pivot(index="trade_date", columns="symbol", values="value")

    qrows = repo.query("SELECT trade_date, symbol, close FROM fact_quote")
    cp = pd.DataFrame([dict(r) for r in qrows]).pivot(index="trade_date", columns="symbol", values="close").sort_index()
    fwd = cp.pct_change(horizon).shift(-horizon)

    ics = []
    for d in fp.index:
        if d not in fwd.index:
            continue
        a, b = fp.loc[d], fwd.loc[d]
        m = a.notna() & b.notna()
        if m.sum() >= 4:
            aa, bb = a[m], b[m]
            if aa.nunique() < 2 or bb.nunique() < 2:
                continue  # 常数截面无法计算相关
            # 用秩的 Pearson 相关近似 Spearman，避免额外依赖 scipy
            ic = aa.rank().corr(bb.rank())
            if pd.notna(ic):
                ics.append(ic)
    if not ics:
        return None
    s = pd.Series(ics)
    return {
        "factor": factor,
        "mean_ic": float(s.mean()),
        "ic_std": float(s.std()),
        "icir": float(s.mean() / s.std()) if s.std() else np.nan,
        "win_rate": float((s > 0).mean()),
        "n": int(len(s)),
    }
