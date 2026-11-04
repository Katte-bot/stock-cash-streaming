"""策略研究 API：策略库、详情、回测、对比、因子 IC、市场状态、因子动量。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..features.registry import as_dicts as factor_dicts
from ..research.runner import run_strategy
from ..storage.repository import open_repository
from ..strategy.registry import ensure_registered, get as get_strategy, list_specs, sync_to_db

router = APIRouter(prefix="/api/research", tags=["research"])


class BacktestIn(BaseModel):
    universe: str = "A0"
    start: str | None = None
    end: str | None = None
    oos_start: str | None = None
    params: dict | None = None
    persist: bool = True
    force_refresh: bool = False


class CompareIn(BaseModel):
    strategy_ids: list[str]
    universe: str = "A0"
    start: str | None = None
    end: str | None = None
    oos_start: str | None = None


def _with_repo(fn):
    repo = open_repository()
    try:
        return fn(repo)
    finally:
        repo.close()


def _latest_run(repo, strategy_id: str, universe: str) -> dict | None:
    rows = repo.query(
        "SELECT * FROM fact_strategy_run WHERE strategy_id=? AND universe_id=? "
        "ORDER BY created_at DESC LIMIT 1", (strategy_id, universe))
    if not rows:
        return None
    row = dict(rows[0])
    try:
        row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
    except ValueError:
        row["metrics"] = {}
    return row


@router.get("/strategies")
def strategies(universe: str = "A0") -> dict:
    def _run(repo):
        ensure_registered()
        sync_to_db(repo)
        status = {r["strategy_id"]: r["status"] for r in repo.query(
            "SELECT strategy_id, status FROM dim_strategy")}
        items = []
        for spec in list_specs():
            latest = _latest_run(repo, spec.id, universe)
            metrics = (latest or {}).get("metrics", {})
            items.append({
                "strategy_id": spec.id, "name": spec.name, "category": spec.category,
                "description": spec.description, "factors": spec.factors,
                "defaults": spec.defaults, "status": status.get(spec.id, "research"),
                "last_run": None if not latest else {
                    "run_id": latest["run_id"], "created_at": latest["created_at"],
                    "score": latest["score"], "start": latest["start_date"],
                    "end": latest["end_date"], "full": metrics.get("full", {}),
                    "oos": metrics.get("oos", {}), "scorecard": metrics.get("scorecard", {}),
                    "yearly": metrics.get("yearly", {}),
                },
            })
        return {"universe": universe, "items": items}

    return _with_repo(_run)


@router.get("/strategies/{strategy_id}")
def strategy_detail(strategy_id: str, universe: str = "A0") -> dict:
    def _run(repo):
        ensure_registered()
        spec = get_strategy(strategy_id)
        if spec is None:
            raise HTTPException(status_code=404, detail="策略不存在")
        latest = _latest_run(repo, strategy_id, universe)
        nav = []
        if latest:
            rows = repo.query(
                "SELECT trade_date, nav, benchmark, drawdown FROM backtest_result "
                "WHERE run_id=? ORDER BY trade_date", (latest["run_id"],))
            nav = [{"trade_date": r["trade_date"], "nav": r["nav"],
                    "benchmark": r["benchmark"], "drawdown": r["drawdown"]}
                   for r in rows[::3]]
        return {
            "strategy_id": spec.id, "name": spec.name, "category": spec.category,
            "description": spec.description, "factors": spec.factors,
            "defaults": spec.defaults, "universe": universe,
            "last_run": latest, "nav": nav,
        }

    return _with_repo(_run)


@router.post("/strategies/{strategy_id}/backtest")
def strategy_backtest(strategy_id: str, payload: BacktestIn) -> dict:
    if get_strategy(strategy_id) is None:
        ensure_registered()
    if get_strategy(strategy_id) is None:
        raise HTTPException(status_code=404, detail="策略不存在")
    try:
        return _with_repo(lambda repo: run_strategy(
            repo, strategy_id, universe=payload.universe, start=payload.start,
            end=payload.end, oos_start=payload.oos_start, params=payload.params,
            persist=payload.persist, force_refresh=payload.force_refresh,
        ))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"回测失败：{e}") from e


@router.post("/compare")
def compare(payload: CompareIn) -> dict:
    ids = payload.strategy_ids[:5]
    if len(ids) < 2:
        raise HTTPException(status_code=400, detail="至少选择 2 个策略")

    def _run(repo):
        results, curves = [], {}
        for sid in ids:
            try:
                res = run_strategy(repo, sid, universe=payload.universe, start=payload.start,
                                   end=payload.end, oos_start=payload.oos_start,
                                   persist=False)
            except ValueError as e:
                results.append({"strategy_id": sid, "error": str(e)})
                continue
            results.append({
                "strategy_id": sid, "name": res["strategy_name"],
                "metrics": res["metrics"], "oos": res["oos"],
                "scorecard": res["scorecard"], "start": res["start"], "end": res["end"],
            })
            curves[sid] = {x["trade_date"]: x["nav"] for x in res["nav"]}
        # 对齐日期并归一化
        import pandas as pd

        series = {sid: pd.Series(c, dtype="float64").sort_index()
                  for sid, c in curves.items() if c}
        if not series:
            return {"universe": payload.universe, "results": results, "nav": [], "corr": None}
        norm = pd.DataFrame({sid: s / float(s.iloc[0]) for sid, s in series.items()}).sort_index()
        norm = norm.ffill()
        nav = [{"trade_date": str(d),
                **{sid: (None if pd.isna(v) else round(float(v), 4))
                   for sid, v in row.items()}}
               for d, row in norm.iterrows()]
        corr = None
        if len(series) >= 2:
            rets = pd.DataFrame({sid: s.pct_change() for sid, s in series.items()})
            corr = rets.corr().round(3).to_dict()
        return {"universe": payload.universe, "results": results, "nav": nav, "corr": corr}

    return _with_repo(_run)


@router.get("/factors")
def factors(universe: str = "A0") -> dict:
    def _run(repo):
        rows = repo.query(
            "SELECT factor, horizon, AVG(ic) AS ic, AVG(rank_ic) AS rank_ic, COUNT(*) AS n "
            "FROM fact_factor_ic WHERE universe_id=? GROUP BY factor, horizon",
            (universe,))
        ic_map: dict[str, dict] = {}
        for r in rows:
            if r["ic"] is None:
                continue
            ic_map.setdefault(r["factor"], {})[f"h{r['horizon']}"] = round(float(r["ic"]), 4)
        return {"universe": universe,
                "items": [{**d, "ic": ic_map.get(d["name"], {})} for d in factor_dicts()]}

    return _with_repo(_run)


@router.get("/factors/{name}/ic")
def factor_ic(name: str, universe: str = "A0", horizon: int = 5,
              start: str | None = None, end: str | None = None) -> dict:
    from ..features.cache import load_cached_factors
    from ..research.ic import compute_ic, quantile_returns, summarize

    def _run(repo):
        panel, factors, cache_info = load_cached_factors(repo, universe, start, end)
        if name not in factors:
            raise HTTPException(status_code=404, detail="因子不存在或无数据")
        ic_df = compute_ic(panel, {name: factors[name]}, horizon=horizon)
        q = quantile_returns(panel, factors[name], horizon=max(horizon, 20))
        q.index = [str(x) for x in q.index] if len(q) else q.index
        return {
            "factor": name, "universe": universe, "horizon": horizon,
            "summary": summarize(ic_df).get(name, {}),
            "series": [{"trade_date": d, "ic": round(float(v), 4)}
                       for d, v in ic_df[name].dropna().items()],
            "quantiles": q.reset_index().rename(columns={"index": "trade_date"})
            .to_dict("records") if len(q) else [],
            "cache": cache_info,
        }

    return _with_repo(_run)


@router.get("/regime")
def regime() -> dict:
    def _run(repo):
        rows = repo.query(
            "SELECT * FROM fact_regime_state ORDER BY trade_date DESC LIMIT 250")
        if rows:
            return {"source": "table", "items": [dict(r) for r in reversed(rows)]}
        from ..ingest.index_quote import index_series

        idx = index_series(repo, "sh000300")
        if idx.empty:
            return {"source": "empty", "items": []}
        ma200 = idx.rolling(200, min_periods=60).mean()
        trend = []
        values = idx.to_numpy()
        for pos, d in enumerate(idx.index[-250:]):
            i = len(idx) - min(250, len(idx)) + pos
            up = values[i] > ma200.iloc[i]
            chg20 = values[i] / values[i - 20] - 1 if i >= 20 else 0.0
            trend.append({"trade_date": d,
                          "trend": "up" if up and chg20 > 0 else ("down" if not up and chg20 < 0 else "range"),
                          "breadth": None, "vol_state": None, "amount_chg": None, "style": None})
        return {"source": "computed", "items": trend}

    return _with_repo(_run)


@router.get("/meta/weights")
def meta_weights() -> dict:
    def _run(repo):
        latest = repo.query("SELECT MAX(trade_date) AS d FROM fact_strategy_weight")
        d = latest[0]["d"] if latest else None
        if not d:
            return {"trade_date": None, "items": []}
        rows = repo.query(
            "SELECT strategy_id, weight, reason FROM fact_strategy_weight "
            "WHERE trade_date=? ORDER BY weight DESC", (d,))
        hist = repo.query(
            "SELECT trade_date, strategy_id, weight FROM fact_strategy_weight "
            "ORDER BY trade_date")
        return {"trade_date": d, "items": [dict(r) for r in rows],
                "history": [dict(r) for r in hist]}

    return _with_repo(_run)
