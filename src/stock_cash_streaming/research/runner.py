"""统一策略回测编排：样本内/样本外、分年/分状态、评分与落库。"""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd

from ..backtest.engine import DEFAULT_COST_RATE, run_backtest
from ..backtest.metrics import compute_metrics
from ..common.logging import logger
from ..features.cache import load_cached_factors
from ..research.pool_admin import portfolio_target_name
from ..storage.repository import Repository
from ..strategy.registry import ensure_registered, get as get_strategy, sync_to_db
from .scorecard import score_strategy
from .walkforward import summarize as summarize_walkforward
from .walkforward import walk_forward

DEFAULT_OOS_START = "2024-01-01"


def _yearly(nav: pd.Series) -> dict[str, float]:
    if nav.empty:
        return {}
    years = pd.to_datetime(pd.Index(nav.index)).year
    out: dict[str, float] = {}
    for y, g in nav.groupby(years):
        if len(g) > 1 and g.iloc[0] > 0:
            out[str(int(y))] = round(float(g.iloc[-1] / g.iloc[0] - 1), 6)
    return out


def _regime_metrics(repo: Repository, nav: pd.Series, bench: pd.Series) -> dict:
    from ..ingest.index_quote import index_series

    idx = index_series(repo, "sh000300")
    if idx.empty:
        return {}
    trend = idx.pct_change(20)
    regime = pd.Series("range", index=idx.index)
    regime[trend > 0.03] = "up"
    regime[trend < -0.03] = "down"
    regime = regime.reindex(nav.index).ffill()
    ret_s = nav.pct_change().fillna(0.0)
    ret_b = bench.pct_change().fillna(0.0)
    out: dict[str, dict] = {}
    for name in ("up", "range", "down"):
        mask = regime == name
        if int(mask.sum()) < 20:
            continue
        out[name] = {
            "days": int(mask.sum()),
            "strategy_ann": round(float(ret_s[mask].mean() * 252), 4),
            "benchmark_ann": round(float(ret_b[mask].mean() * 252), 4),
            "win_rate": round(float((ret_s[mask] > 0).mean()), 4),
        }
    return out


def _apply_params(spec, params: dict | None) -> dict:
    merged = dict(spec.defaults)
    if params:
        merged.update({k: v for k, v in params.items() if v is not None})
    return merged


def run_strategy(repo: Repository, strategy_id: str, universe: str = "A0",
                 start: str | None = None, end: str | None = None,
                 oos_start: str | None = None, params: dict | None = None,
                 persist: bool = True, force_refresh: bool = False) -> dict:
    """运行一个注册策略并返回标准化结果。"""
    ensure_registered()
    sync_to_db(repo)
    spec = get_strategy(strategy_id)
    if spec is None:
        raise ValueError(f"未知策略：{strategy_id}")
    if spec.builder is None:
        raise ValueError(f"策略缺少评分函数：{strategy_id}")

    p = _apply_params(spec, params)
    panel, factors, cache_info = load_cached_factors(
        repo, universe, start, end, force=force_refresh, with_v5=True)

    weight_history: dict[str, dict[str, float]] = {}
    components_meta: dict = {}
    if strategy_id == "factor_momentum":
        from .factor_momentum import compute as compute_meta

        comps = p.get("components") or []
        score, weight_history, _ = compute_meta(repo, panel, factors, comps, top_k=int(p["top_k"]))
        if weight_history:
            components_meta["last_weights"] = weight_history[max(weight_history)]
            # 用最新权重回填 params，便于结果复现
            p = {**p, "weights": components_meta["last_weights"]}
    else:
        score = spec.builder(panel, factors, p)

    if score is None or score.empty:
        raise ValueError("评分矩阵为空：请检查因子数据是否就绪")

    engine_panel = {
        "close": panel.close, "ret": panel.ret, "pct": panel.pct,
        "piv": factors, "sector_map": panel.sector_map,
        "st_symbols": panel.st_symbols, "eligibility": panel.eligibility,
        "amount": panel.amount,
    }
    res = run_backtest(
        repo, top_k=int(p["top_k"]), cost_rate=DEFAULT_COST_RATE,
        start_date=start, end_date=end, strategy=strategy_id,
        rebalance=p.get("rebalance", "monthly"), hold_buffer=float(p.get("hold_buffer", 1.0)),
        weighting=p.get("weighting", "equal"), vol_target=float(p.get("vol_target", 0.0)),
        score_override=score, panel=engine_panel, eligibility=panel.eligibility,
        cost_model="a_share", limit_model="board", persist=persist,
        universe_tag=universe, return_weights=True, return_trades=True,
        capacity_pct=float(p.get("capacity_pct", 0.0) or 0.0),
    )

    nav, bench = res["nav"], res["benchmark"]
    oos_start = oos_start or DEFAULT_OOS_START
    oos = {}
    if oos_start and oos_start > res["start"]:
        oos_nav = nav[nav.index >= oos_start]
        oos_bench = bench[bench.index >= oos_start]
        if len(oos_nav) > 2:
            oos = compute_metrics(oos_nav, oos_bench)

    yearly = _yearly(nav)
    regime = _regime_metrics(repo, nav, bench)
    wf = walk_forward(nav, bench)
    wf_summary = summarize_walkforward(wf)
    scorecard = score_strategy(res["metrics"], oos)

    last_date = nav.index[-1] if len(nav) else None
    positions: list[str] = []
    if last_date is not None and last_date in score.index:
        row = score.loc[last_date].where(panel.eligibility.loc[last_date]).dropna()
        positions = list(row.sort_values(ascending=False).index[:int(p["top_k"])])

    trades = res.get("trades", [])
    worst_days = res.get("worst_days", [])
    recent_trades = trades[-50:][::-1]

    metrics_json = {
        "full": res["metrics"], "oos": oos, "yearly": yearly, "regime": regime,
        "cache": cache_info, "scorecard": scorecard,
        "walkforward": {"segments": wf, "summary": wf_summary},
        "worst_days": worst_days,
        "trades": recent_trades,
        "capacity_pct": float(p.get("capacity_pct", 0.0) or 0.0),
    }
    if weight_history:
        metrics_json["weights"] = weight_history[max(weight_history)]

    if persist:
        repo.upsert(
            "fact_strategy_run",
            [{
                "run_id": res["run_id"], "strategy_id": strategy_id, "universe_id": universe,
                "start_date": res["start"], "end_date": res["end"],
                "oos_start": oos_start,
                "params_json": json.dumps(p, ensure_ascii=False, default=str),
                "metrics_json": json.dumps(metrics_json, ensure_ascii=False),
                "score": scorecard["score"],
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }],
            ["run_id"],
        )
        if weight_history:
            rows = []
            for d, weights in weight_history.items():
                for sid, w in weights.items():
                    rows.append({"trade_date": d, "strategy_id": sid,
                                 "weight": round(float(w), 4), "reason": "factor_momentum"})
            if rows:
                repo.upsert("fact_strategy_weight", rows, ["trade_date", "strategy_id"])

    logger.info(f"策略回测 {strategy_id}/{universe}: {res['metrics'].get('annual_return', 0):.2%}"
                f" 夏普 {res['metrics'].get('sharpe', 0):.2f} 评分 {scorecard['score']}")

    pool_sync: dict = {}
    if persist and universe and universe != "A0" and strategy_id != "defensive":
        pool_sync = _sync_pool_outputs(repo, universe, strategy_id, res)

    dd = (nav / nav.cummax() - 1).fillna(0.0)
    return {
        "run_id": res["run_id"], "strategy_id": strategy_id, "strategy_name": spec.name,
        "universe": universe, "start": res["start"], "end": res["end"],
        "params": p, "metrics": res["metrics"], "oos": oos, "yearly": yearly,
        "regime": regime, "scorecard": scorecard, "cache": cache_info,
        "walkforward": {"segments": wf, "summary": wf_summary},
        "positions": positions, "weights": metrics_json.get("weights"),
        "trades": recent_trades, "worst_days": worst_days,
        "capacity_pct": metrics_json["capacity_pct"], "pool_sync": pool_sync,
        "nav": [
            {"trade_date": d, "nav": round(float(nav[d]), 2),
             "benchmark": round(float(bench[d]), 2),
             "drawdown": round(float(dd[d]), 6)}
            for d in nav.index
        ],
    }


def _sync_pool_outputs(repo: Repository, pool_id: str, strategy_id: str,
                       res: dict) -> dict:
    """把研究策略回测结果写成池的目标组合快照，并刷新跟踪与模拟账户。"""
    from ..strategy.paper import refresh_paper_account
    from ..strategy.tracking import simulate_tracking

    name = portfolio_target_name(pool_id, strategy_id)
    weights = res.get("weights")
    reb_dates = res.get("rebalance_dates") or []
    if weights is None or weights.empty or not reb_dates:
        return {"status": "skipped", "reason": "无调仓快照"}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    runs: list[dict] = []
    rows: list[dict] = []
    for d in reb_dates:
        if d not in weights.index:
            continue
        row = weights.loc[d]
        picked = {s: float(w) for s, w in row.items() if float(w) > 1e-9}
        if not picked:
            continue
        runs.append({"trade_date": d, "strategy": name, "is_rebalance": 1,
                     "basket_vol": None, "vol_scale": 1.0,
                     "exposure": round(sum(picked.values()), 6), "created_at": now})
        for sym, w in picked.items():
            rows.append({"trade_date": d, "strategy": name, "symbol": sym,
                         "weight": round(w, 6), "raw_weight": round(w, 6),
                         "is_rebalance": 1, "created_at": now})
    if not runs:
        return {"status": "skipped", "reason": "调仓快照为空"}

    repo.upsert("fact_target_run", runs, ["trade_date", "strategy"])
    repo.upsert("fact_target_portfolio", rows, ["trade_date", "strategy", "symbol"])

    tracking = simulate_tracking(repo, strategy=name, persist=True, universe_id=pool_id)
    try:
        paper = refresh_paper_account(repo, pool_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{pool_id} 模拟账户刷新失败：{e}")
        paper = {"status": "failed", "message": str(e)}
    return {"target_strategy": name, "snapshots": len(runs),
            "tracking": tracking.get("status"), "paper": paper.get("status")}
