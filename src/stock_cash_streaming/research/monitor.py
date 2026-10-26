"""因子有效性监控：滚动 RankIC、ICIR 与衰减告警。

口径与研究实验一致：
- RankIC = 截面因子秩与未来 horizon 日收益秩的 Pearson 相关（等价 Spearman）；
- 只使用当日有因子、且未来收益可得的样本；
- 全期统计用于判断方向，最近 window 日统计用于判断是否衰减。

说明：监控只输出诊断信息，不自动调整策略权重。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..common.logging import logger
from ..research.universe import C0, universe_symbols
from ..storage.repository import Repository

TECH_FACTORS = [
    "vol_20", "turnover_5", "turnover_std_20", "amount_20d",
    "max_ret_20", "ret_20", "boll_pos", "amihud_20", "ma60_ratio",
]
FLOW_FACTORS = ["main_net_5d", "main_net_ratio_5d", "inflow_days_5", "institution_trend"]
FUND_FACTORS = [
    "roe_ttm", "roe_std_8q", "cfo_to_np", "cfo_to_np_std_8q", "cfo_to_rev",
    "ep_ttm", "bp_mrq", "fcf_yield", "div_yield_3y", "div_continuity_3y",
    "div_payout", "leverage",
]

MIN_CROSS_SECTION = 10


def _load_factor(repo: Repository, factor: str, version: str,
                 symbols: list[str], start: str) -> pd.DataFrame | None:
    if not symbols:
        return None
    ph = ",".join("?" for _ in symbols)
    rows = repo.query(
        f"SELECT trade_date, symbol, value FROM fact_factor "
        f"WHERE factor=? AND version=? AND trade_date>=? AND symbol IN ({ph})",
        tuple([factor, version, start] + symbols),
    )
    if not rows:
        return None
    df = pd.DataFrame([dict(r) for r in rows])
    return df.pivot(index="trade_date", columns="symbol", values="value").sort_index()


def _load_close(repo: Repository, symbols: list[str], start: str) -> pd.DataFrame:
    ph = ",".join("?" for _ in symbols)
    rows = repo.query(
        f"SELECT trade_date, symbol, close FROM fact_quote "
        f"WHERE trade_date>=? AND symbol IN ({ph}) ORDER BY trade_date",
        tuple([start] + symbols),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    return df.pivot(index="trade_date", columns="symbol", values="close").sort_index()


def compute_ic_series(repo: Repository, universe_id: str = C0,
                      horizon: int = 5, start: str = "2018-01-01",
                      factors: list[str] | None = None) -> pd.DataFrame:
    """返回 dates × factors 的逐日 RankIC 序列。"""
    symbols = universe_symbols(repo, universe_id)
    close = _load_close(repo, symbols, start)
    if close.empty:
        return pd.DataFrame()
    fwd = close.pct_change(horizon).shift(-horizon)

    groups = factors or (TECH_FACTORS + FLOW_FACTORS + FUND_FACTORS)
    out: dict[str, pd.Series] = {}
    for f in groups:
        version = "v3" if f in FUND_FACTORS else "v2"
        mat = _load_factor(repo, f, version, symbols, start)
        if mat is None:
            continue
        mat = mat.reindex(index=close.index, columns=close.columns)
        valid = (mat.notna() & fwd.notna()).sum(axis=1) >= MIN_CROSS_SECTION
        if not valid.any():
            continue
        ic = mat.rank(axis=1).corrwith(fwd.rank(axis=1), axis=1)
        out[f] = ic.where(valid)
        logger.info(f"  IC 完成 {f}（{version}）：有效 {int(valid.sum())} 日")
    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).sort_index()


def summarize_ic(ic: pd.DataFrame, window: int = 120) -> pd.DataFrame:
    """全期 vs 最近 window 日的 IC 统计与状态判断。"""
    rows = []
    for f in ic.columns:
        s = ic[f].dropna()
        if s.empty:
            continue
        recent = s.tail(window)
        full_mean = float(s.mean())
        full_std = float(s.std()) if len(s) > 1 else np.nan
        rec_mean = float(recent.mean()) if len(recent) else np.nan
        rec_std = float(recent.std()) if len(recent) > 1 else np.nan
        full_icir = full_mean / full_std if full_std else np.nan
        rec_icir = rec_mean / rec_std if rec_std else np.nan
        decay = (rec_mean / full_mean) if full_mean else np.nan
        status = "ok"
        note = ""
        # 只对「全期有一定有效性」的因子做衰减判断，避免低 IC 因子的噪声告警
        meaningful = abs(full_mean) >= 0.03
        if meaningful and np.sign(rec_mean) != np.sign(full_mean) and abs(rec_mean) >= 0.01:
            status, note = "alert", "近端 IC 方向反转"
        elif meaningful and abs(rec_mean) < 0.5 * abs(full_mean):
            status, note = "watch", "近端 IC 衰减超过一半"
        elif meaningful and not np.isnan(rec_icir) and abs(rec_icir) < 0.2:
            status, note = "watch", "近端 ICIR 绝对值 < 0.2"
        rows.append({
            "factor": f,
            "days": int(len(s)),
            "full_mean_ic": full_mean,
            "full_icir": full_icir,
            "full_win_rate": float((s > 0).mean()),
            "recent_mean_ic": rec_mean,
            "recent_icir": rec_icir,
            "recent_win_rate": float((recent > 0).mean()) if len(recent) else np.nan,
            "decay_ratio": decay,
            "status": status,
            "note": note,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    order = {"alert": 0, "watch": 1, "ok": 2}
    df["_o"] = df["status"].map(order).fillna(3)
    return df.sort_values(["_o", "full_mean_ic"], ascending=[True, False]).drop(columns="_o")


def run_ic_monitor(repo: Repository, universe_id: str = C0, horizon: int = 5,
                   window: int = 120, start: str = "2018-01-01") -> dict:
    ic = compute_ic_series(repo, universe_id=universe_id, horizon=horizon, start=start)
    if ic.empty:
        return {"error": "无可用因子数据", "summary": pd.DataFrame(), "ic": ic}
    summary = summarize_ic(ic, window=window)
    alerts = summary[summary["status"] != "ok"].to_dict("records")
    return {"ic": ic, "summary": summary, "alerts": alerts,
            "horizon": horizon, "window": window, "universe": universe_id}


def run_drawdown_monitor(repo: Repository, dd_alert: float = -0.10,
                         recent_days: int = 20) -> list[dict]:
    """策略回撤监控：读取跟踪净值，输出当前回撤与近端跌幅告警。"""
    from ..strategy.portfolio import STRATEGY_NAMES

    out: list[dict] = []
    for kind, name in STRATEGY_NAMES.items():
        rows = repo.query(
            "SELECT trade_date, nav FROM fact_tracking_nav WHERE strategy=? ORDER BY trade_date",
            (name,))
        if len(rows) < 2:
            out.append({"kind": kind, "strategy": name, "status": "no_data",
                        "message": "跟踪净值不足 2 个交易日"})
            continue
        nav = pd.Series([float(r["nav"]) for r in rows],
                        index=[r["trade_date"] for r in rows])
        peak = float(nav.cummax().iloc[-1])
        cur = float(nav.iloc[-1])
        dd = cur / peak - 1 if peak else 0.0
        base = float(nav.iloc[max(0, len(nav) - recent_days)])
        recent = cur / base - 1 if base else 0.0
        status = "alert" if dd <= dd_alert else "ok"
        out.append({
            "kind": kind, "strategy": name, "status": status,
            "last_date": rows[-1]["trade_date"], "nav": cur,
            "drawdown": dd, "recent_return": recent,
            "message": (f"回撤 {dd:.2%} 触发阈值 {dd_alert:.0%}" if status == "alert"
                        else "正常"),
        })
    return out
