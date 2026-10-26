"""因子诊断：全期 / 分年度 / 分市场状态 IC，以及分位数收益。"""

from pathlib import Path

import numpy as np
import pandas as pd

from ..datasource.akshare_src import tx_index_daily
from ..features.engine import FACTOR_VERSION
from ..storage.repository import open_repository

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REPORT_PATH = PROJECT_ROOT / "docs" / "research_factor_diagnostics.md"

FACTORS = [
    "ret_5", "ret_20", "ma5_ratio", "ma20_ratio", "boll_pos", "rsi14", "macd_hist",
    "vol_20", "turnover_5", "ma60_ratio", "amount_20d",
    "max_ret_20", "turnover_std_20", "amihud_20",
    "main_net_ratio_5d", "inflow_days_5", "institution_trend", "xl_ratio_5d",
]


def _load_pivots(repo, version: str = FACTOR_VERSION):
    q = pd.DataFrame([dict(r) for r in repo.query(
        "SELECT symbol, trade_date, close FROM fact_quote")])
    close = q.pivot(index="trade_date", columns="symbol", values="close").sort_index()

    rows = [dict(r) for r in repo.query(
        "SELECT symbol, trade_date, factor, value FROM fact_factor WHERE version=?", (version,))]
    fdf = pd.DataFrame(rows)
    piv = {
        f: fdf[fdf["factor"] == f].pivot(index="trade_date", columns="symbol", values="value")
        for f in FACTORS if not fdf.empty and (fdf["factor"] == f).any()
    }
    return close, piv


def _ic_series(f: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    out = {}
    for d in f.index:
        if d not in fwd.index:
            continue
        a, b = f.loc[d], fwd.loc[d]
        m = a.notna() & b.notna()
        if m.sum() >= 8:
            aa, bb = a[m], b[m]
            if aa.nunique() < 2 or bb.nunique() < 2:
                continue
            ic = aa.rank().corr(bb.rank())
            if pd.notna(ic):
                out[d] = ic
    return pd.Series(out).sort_index()


def _regime(dates: pd.Index) -> pd.Series:
    idx = tx_index_daily("sh000300").set_index("trade_date")["close"].sort_index()
    ma = idx.rolling(200, min_periods=60).mean()
    reg = pd.Series(np.where(idx > ma, "bull", "bear"), index=idx.index)
    return reg.reindex(dates).ffill()


def _quantile_returns(f: pd.DataFrame, fwd: pd.DataFrame, q: int = 5) -> pd.Series:
    acc = []
    for d in f.index:
        if d not in fwd.index:
            continue
        a, b = f.loc[d], fwd.loc[d]
        m = a.notna() & b.notna()
        if m.sum() < q * 2:
            continue
        aa, bb = a[m], b[m]
        try:
            bins = pd.qcut(aa.rank(method="first"), q, labels=False)
        except ValueError:
            continue
        acc.append(bb.groupby(bins).mean())
    if not acc:
        return pd.Series(dtype=float)
    return pd.concat(acc, axis=1).mean(axis=1)


def run_diagnostics(horizon: int = 5, version: str = FACTOR_VERSION) -> pd.DataFrame:
    repo = open_repository()
    try:
        close, piv = _load_pivots(repo, version)
    finally:
        repo.close()

    if close.empty or not piv:
        return pd.DataFrame()

    fwd = close.pct_change(horizon).shift(-horizon)
    dates = close.index
    reg = _regime(dates)

    records = []
    for f, mat in piv.items():
        ics = _ic_series(mat, fwd)
        if ics.empty:
            continue
        qr = _quantile_returns(mat, fwd)
        years = pd.Series(ics.values, index=pd.to_datetime(ics.index)).groupby(
            pd.to_datetime(ics.index).year).mean()
        bull = ics[reg.reindex(ics.index) == "bull"]
        bear = ics[reg.reindex(ics.index) == "bear"]
        rec = {
            "factor": f,
            "mean_ic": ics.mean(),
            "icir": ics.mean() / ics.std() if ics.std() else np.nan,
            "win_rate": (ics > 0).mean(),
            "n": len(ics),
            "bull_ic": bull.mean() if len(bull) else np.nan,
            "bear_ic": bear.mean() if len(bear) else np.nan,
            "q1": qr.get(0, np.nan) if not qr.empty else np.nan,
            "q5": qr.get(4, np.nan) if not qr.empty else np.nan,
            "q5_q1": (qr.get(4, np.nan) - qr.get(0, np.nan)) if not qr.empty else np.nan,
        }
        for y, v in years.items():
            rec[str(y)] = v
        records.append(rec)

    df = pd.DataFrame(records).sort_values("mean_ic", ascending=False)
    return df


def write_report(df: pd.DataFrame, horizon: int = 5) -> Path:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 因子诊断报告",
        "",
        f"- 预测目标：未来 {horizon} 日收益（RankIC）",
        f"- 样本：{len(df)} 个因子",
        "- 说明：IC 为截面秩相关；bull/bear 按沪深300 是否在 200 日均线之上划分。",
        "",
        "## 汇总",
        "",
        "| 因子 | mean_IC | ICIR | 胜率 | n | 牛市IC | 熊市IC | Q1 | Q5 | Q5-Q1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, r in df.iterrows():
        lines.append(
            f"| {r['factor']} | {r['mean_ic']:.4f} | {r['icir']:.2f} | {r['win_rate']:.1%} | "
            f"{int(r['n'])} | {r['bull_ic']:.4f} | {r['bear_ic']:.4f} | "
            f"{r['q1']:.4f} | {r['q5']:.4f} | {r['q5_q1']:.4f} |"
        )

    year_cols = [c for c in df.columns if c.isdigit()]
    if year_cols:
        lines += ["", "## 分年度 IC", "",
                  "| 因子 | " + " | ".join(year_cols) + " |",
                  "| --- | " + " | ".join("---:" for _ in year_cols) + " |"]
        for _, r in df.iterrows():
            lines.append(f"| {r['factor']} | " + " | ".join(f"{r[c]:.3f}" for c in year_cols) + " |")

    lines += ["", "> 本报告为量化研究结果，不构成投资建议。"]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return REPORT_PATH
