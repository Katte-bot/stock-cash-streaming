"""模拟跟踪：按已落库的调仓快照，用真实收盘价计算跟踪净值。

规则（与回测一致）：
- 只使用 `fact_target_portfolio` 中 `is_rebalance=1` 的快照作为建仓/调仓点
- 信号日次一交易日收盘执行（建仓/调仓均按该日收盘价）
- 调仓成本：单边 0.13% × 换手率
- 两次调仓之间按价格自然漂移，不主动调权
- 基准：板块成分等权买入持有，起点与跟踪起点一致

说明：本模块只做跟踪统计，不构成投资建议。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from ..backtest.metrics import compute_metrics
from ..common.logging import logger
from ..storage.repository import Repository
from .portfolio import STRATEGY_NAME

DEFAULT_COST_RATE = 0.0013


def _quote_dates(repo: Repository) -> list[str]:
    return [r["d"] for r in repo.query(
        "SELECT DISTINCT trade_date AS d FROM fact_quote ORDER BY trade_date")]


def _exec_date(dates: list[str], d: str) -> str | None:
    """信号日 → 次一交易日（不存在则返回 None）。"""
    for x in dates:
        if x > d:
            return x
    return None


def _snapshots(repo: Repository, strategy: str, start_date: str | None) -> dict[str, dict[str, float]]:
    sql = ("SELECT trade_date, symbol, weight FROM fact_target_portfolio "
           "WHERE strategy=? AND is_rebalance=1")
    params: list = [strategy]
    if start_date:
        sql += " AND trade_date>=?"
        params.append(start_date)
    sql += " ORDER BY trade_date, symbol"
    out: dict[str, dict[str, float]] = {}
    for r in repo.query(sql, tuple(params)):
        out.setdefault(r["trade_date"], {})[r["symbol"]] = float(r["weight"])
    return out


def _close_matrix(repo: Repository, symbols: list[str], start_date: str) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    ph = ",".join("?" for _ in symbols)
    rows = repo.query(
        f"SELECT symbol, trade_date, close FROM fact_quote "
        f"WHERE trade_date>=? AND symbol IN ({ph}) ORDER BY trade_date",
        tuple([start_date] + symbols))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    return df.pivot(index="trade_date", columns="symbol", values="close").sort_index().ffill()


def simulate_tracking(
    repo: Repository,
    strategy: str = STRATEGY_NAME,
    start_date: str | None = None,
    end_date: str | None = None,
    cost_rate: float = DEFAULT_COST_RATE,
    persist: bool = False,
) -> dict:
    """按调仓快照模拟跟踪净值。"""
    snaps = _snapshots(repo, strategy, start_date)
    if not snaps:
        return {"status": "waiting", "strategy": strategy,
                "message": "暂无调仓快照，请先运行 `cli portfolio` 建立基线"}

    dates = _quote_dates(repo)
    if not dates:
        return {"status": "waiting", "strategy": strategy, "message": "无行情数据"}

    # 信号日 → 执行日
    execs: dict[str, dict[str, float]] = {}
    pending = None
    for d, w in sorted(snaps.items()):
        e = _exec_date(dates, d)
        if e is None:
            pending = d
            continue
        execs[e] = w
    if not execs:
        return {"status": "waiting", "strategy": strategy,
                "baseline_date": sorted(snaps)[-1],
                "message": "已建立基线，等待次一交易日收盘行情"}

    start = min(execs)
    if end_date:
        execs = {d: w for d, w in execs.items() if d <= end_date}
        if not execs:
            return {"status": "waiting", "strategy": strategy, "baseline_date": start,
                    "message": "跟踪区间内无数据"}
        start = min(execs)

    symbols = sorted({s for w in execs.values() for s in w})
    close = _close_matrix(repo, symbols, start)
    if close.empty:
        return {"status": "waiting", "strategy": strategy, "baseline_date": start,
                "message": "等待行情数据"}

    # 基准：板块成分等权买入持有
    bench_syms = [r["symbol"] for r in repo.query(
        "SELECT DISTINCT symbol FROM dim_sector_member")]
    bclose = _close_matrix(repo, bench_syms, start)
    if bclose.empty:
        bench = pd.Series(1.0, index=close.index)
    else:
        base = bclose.iloc[0].replace(0, np.nan)
        bench = (bclose / base).mean(axis=1).reindex(close.index).ffill().fillna(1.0)

    # 从执行日开始逐日模拟
    idx = [d for d in close.index if d >= start]
    if end_date:
        idx = [d for d in idx if d <= end_date]
    if not idx:
        return {"status": "waiting", "strategy": strategy, "baseline_date": start,
                "message": "等待行情数据"}

    w = pd.Series(0.0, index=symbols)
    nav = 1.0
    navs, benches, exposures, dds, out_dates = [], [], [], [], []
    rebalances = []

    # 首个执行日：按目标组合建仓（计成本）
    target = pd.Series(execs[start]).reindex(symbols).fillna(0.0)
    turnover = 0.5 * float(target.abs().sum())
    nav *= (1 - turnover * cost_rate)
    w = target.copy()
    rebalances.append(start)

    for d in idx:
        if d > start:
            prev = close.loc[:d].iloc[-2]
            cur = close.loc[d]
            r = (cur / prev - 1).replace([np.inf, -np.inf], np.nan).fillna(0.0)
            port_r = float((w * r.reindex(symbols).fillna(0.0)).sum())
            nav *= (1 + port_r)
            if port_r > -1:
                w = w * (1 + r.reindex(symbols).fillna(0.0)) / (1 + port_r)

        # 调仓日（执行日）按目标权重重置
        if d in execs and d != start:
            target = pd.Series(execs[d]).reindex(symbols).fillna(0.0)
            turnover = 0.5 * float((target - w).abs().sum())
            nav *= (1 - turnover * cost_rate)
            w = target.copy()
            rebalances.append(d)

        out_dates.append(d)
        navs.append(nav)
        benches.append(float(bench.loc[d]))
        exposures.append(float(w.sum()))
        dds.append(nav / max(navs) - 1)

    nav_s = pd.Series(navs, index=pd.Index(out_dates, name="trade_date"))
    bench_s = pd.Series(benches, index=pd.Index(out_dates, name="trade_date"))
    metrics = compute_metrics(nav_s, bench_s)
    ret_s = nav_s.pct_change().dropna()
    ret_b = bench_s.pct_change().dropna()
    te = float((ret_s - ret_b).std() * np.sqrt(252)) if len(ret_s) > 2 else None

    series = [{"trade_date": d, "nav": float(nav_s.loc[d]),
               "benchmark": float(bench_s.loc[d]),
               "drawdown": float(dds[i]),
               "exposure": exposures[i]}
              for i, d in enumerate(out_dates)]

    result = {
        "status": "running" if len(nav_s) >= 2 else "waiting",
        "strategy": strategy,
        "baseline_date": min(snaps),
        "start_date": start,
        "last_date": out_dates[-1],
        "days": len(nav_s),
        "pending_rebalance": pending,
        "rebalances": rebalances,
        "metrics": metrics,
        "tracking_error": te,
        "series": series,
        "message": "等待更多交易日数据" if len(nav_s) < 2 else "",
    }

    if persist and len(nav_s) > 0:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        repo.upsert("fact_tracking_nav", [{
            "trade_date": x["trade_date"], "strategy": strategy, "nav": x["nav"],
            "benchmark": x["benchmark"], "exposure": x["exposure"],
            "drawdown": x["drawdown"], "created_at": now,
        } for x in series], ["trade_date", "strategy"])
        logger.info(f"模拟跟踪已落库 {strategy}：{len(series)} 个交易日")

    return result
