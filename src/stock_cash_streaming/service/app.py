"""FastAPI 服务：REST API + WebSocket + 单页前端。"""

import asyncio
import math
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..backtest.metrics import compute_metrics
from ..storage.repository import open_repository

BASE = Path(__file__).parent
app = FastAPI(title="stock-cash-streaming", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (BASE / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/overview")
def overview(universe: str = "C0") -> dict:
    """总览：池内个股、信号、资金流、最新回测、数据状态。"""
    from ..research.universe import universe_members

    repo = open_repository()
    try:
        members = universe_members(repo, universe)
        quote_date = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")[0]["d"]
        flow_date = repo.query("SELECT MAX(trade_date) AS d FROM fact_stock_flow")[0]["d"]
        sig_date = repo.query("SELECT MAX(trade_date) AS d FROM fact_signal")[0]["d"]

        if universe == "C0":
            member_sql = ("SELECT m.symbol, m.sector_code FROM dim_sector_member m "
                          "LEFT JOIN dim_stock s ON s.symbol=m.symbol "
                          "WHERE (m.out_date IS NULL OR m.out_date='') "
                          "AND IFNULL(s.name,'') NOT LIKE '%ST%' "
                          "AND IFNULL(s.name,'') NOT LIKE '%退%'")
            member_params: tuple = ()
            sector_meta = {r["sector_code"]: r["sector_type"] for r in repo.query(
                "SELECT sector_code, sector_type FROM dim_sector")}
            sector_flow = _rows(repo.query(
                "SELECT sector_code, main_net, src FROM fact_sector_flow WHERE trade_date=? "
                "ORDER BY sector_code", (flow_date,)))
        else:
            member_sql = "SELECT symbol, sector_code FROM dim_universe_member WHERE universe_id=?"
            member_params = (universe,)
            sector_meta = {}
            sector_flow = _rows(repo.query(
                f"""WITH m AS ({member_sql})
                    SELECT m.sector_code,
                           SUM(IFNULL(f.main_net, 0)) AS main_net,
                           'pool' AS src
                    FROM m
                    LEFT JOIN fact_stock_flow f
                      ON f.symbol=m.symbol AND f.trade_date=?
                    GROUP BY m.sector_code ORDER BY main_net DESC""",
                tuple([universe, flow_date])))

        stocks = _rows(repo.query(
            f"""WITH m AS ({member_sql})
                SELECT m.sector_code, m.symbol, IFNULL(s.name, '') AS name,
                       f.main_net, q.close, q.pct_chg
                FROM m
                LEFT JOIN dim_stock s ON s.symbol = m.symbol
                LEFT JOIN fact_stock_flow f ON f.symbol = m.symbol AND f.trade_date = ?
                LEFT JOIN fact_quote q ON q.symbol = m.symbol AND q.trade_date = ?
                ORDER BY m.sector_code, IFNULL(f.main_net, -1e18) DESC""",
            tuple(list(member_params) + [flow_date, quote_date])))

        from ..strategy.signal import compute_signals

        signals = compute_signals(repo, universe=universe)
        for x in signals:
            x["sector_code"] = x.pop("sector", "")
        signals.sort(key=lambda x: -x["score"])
        if signals:
            sig_date = signals[0]["trade_date"]

        backtest = None
        bt_name = f"defensive_top12_monthly_{universe}"
        bt = repo.query(
            "SELECT * FROM backtest_run WHERE strategy=? ORDER BY created_at DESC LIMIT 1",
            (bt_name,))
        if bt:
            run = dict(bt[0])
            series = _rows(repo.query(
                "SELECT trade_date, nav, benchmark FROM backtest_result WHERE run_id=? ORDER BY trade_date",
                (run["run_id"],)))
            metrics = {}
            if series:
                nav = pd.Series([x["nav"] for x in series], index=[x["trade_date"] for x in series])
                bench = pd.Series([x["benchmark"] for x in series], index=[x["trade_date"] for x in series])
                metrics = compute_metrics(nav, bench)
                step = max(1, len(series) // 300)
                series = series[::step]
            backtest = {
                "run_id": run["run_id"], "strategy": run["strategy"],
                "start": run["start_date"], "end": run["end_date"],
                "metrics": metrics, "series": series,
            }

        return {
            "universe": universe,
            "members": len(members),
            "as_of": quote_date,
            "signal_date": sig_date,
            "sectors": [{"code": k, "type": v} for k, v in sector_meta.items()],
            "sector_flow": sector_flow,
            "stocks": stocks,
            "signals": signals,
            "backtest": backtest,
            "status": {
                "quotes": repo.count("fact_quote"),
                "factors": repo.count("fact_factor"),
                "signals": repo.count("fact_signal"),
                "stocks": len(members),
            },
        }
    finally:
        repo.close()


@app.get("/api/stock/{symbol}")
def stock_detail(symbol: str) -> dict:
    repo = open_repository()
    try:
        flow = _rows(repo.query(
            "SELECT trade_date, main_net, close, pct_chg FROM fact_stock_flow "
            "WHERE symbol=? ORDER BY trade_date", (symbol,)))
        factors = _rows(repo.query(
            "SELECT trade_date, factor, value FROM fact_factor "
            "WHERE symbol=? AND factor IN ('ret_5','boll_pos','rsi14','macd_hist') ORDER BY trade_date",
            (symbol,)))
        return {"symbol": symbol, "flow": flow, "factors": factors}
    finally:
        repo.close()


@app.get("/api/status")
def status() -> dict:
    repo = open_repository()
    try:
        return {
            "watermarks": _rows(repo.query("SELECT * FROM ingest_watermark")),
            "tasks": _rows(repo.query("SELECT * FROM task_log ORDER BY started_at DESC LIMIT 20")),
        }
    finally:
        repo.close()


HEATMAP_FACTORS = ["vol_20", "turnover_5", "turnover_std_20", "amount_20d",
                   "max_ret_20", "ret_20", "boll_pos", "amihud_20"]


@app.get("/api/factors/latest")
def factors_latest(universe: str = "C0") -> dict:
    """最新交易日的因子截面（用于热力图），可按股票池过滤。"""
    from ..features.engine import FACTOR_VERSION
    from ..research.universe import universe_symbols

    repo = open_repository()
    try:
        d = repo.query(
            "SELECT MAX(trade_date) AS d FROM fact_factor WHERE version=?", (FACTOR_VERSION,)
        )[0]["d"]
        symbols = universe_symbols(repo, universe)
        if not symbols:
            return {"date": d, "factors": HEATMAP_FACTORS, "data": [], "names": {}}
        sp = ",".join("?" for _ in symbols)
        placeholders = ",".join("?" for _ in HEATMAP_FACTORS)
        rows = _rows(repo.query(
            f"SELECT symbol, factor, value FROM fact_factor "
            f"WHERE trade_date=? AND version=? AND factor IN ({placeholders}) "
            f"AND symbol IN ({sp})",
            tuple([d, FACTOR_VERSION] + HEATMAP_FACTORS + symbols)))
        names = {r["symbol"]: r["name"] for r in repo.query(
            f"SELECT symbol, name FROM dim_stock WHERE symbol IN ({sp})", tuple(symbols))}
        return {"date": d, "factors": HEATMAP_FACTORS, "data": rows, "names": names}
    finally:
        repo.close()


@app.get("/api/universes")
def universe_performance() -> dict:
    """各股票池的分年度表现（来自 docs/experiments_universes.csv）。"""
    path = BASE.parents[2] / "docs" / "experiments_universes.csv"
    if not path.exists():
        return {"years": [], "universes": []}

    def num(v) -> float | None:
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(v) else v

    df = pd.read_csv(path)
    years = [c[1:] for c in df.columns if len(c) == 5 and c[0] == "y" and c[1:].isdigit()]
    out = []
    for uid, g in df.groupby("universe", sort=True):
        members = num(g["members"].iloc[0]) if "members" in g.columns else None
        schemes = []
        for _, r in g.iterrows():
            schemes.append({
                "name": str(r["experiment"]).split(" ", 1)[-1],
                "annual": num(r.get("annual")),
                "vol": num(r.get("vol")),
                "sharpe": num(r.get("sharpe")),
                "max_dd": num(r.get("max_dd")),
                "excess": num(r.get("excess")),
                "cost": num(r.get("cost")),
                "yearly": {y: num(r.get(f"y{y}")) for y in years},
            })
        out.append({"id": str(uid), "members": int(members) if members else None,
                    "schemes": schemes})
    return {"years": years, "universes": out}


@app.get("/api/portfolio")
def portfolio(trade_date: str | None = None, strategy: str = "defensive",
              universe: str = "C0") -> dict:
    """目标组合与调仓清单（防御型策略，实盘跟踪口径）。"""
    from ..strategy.portfolio import build_target_portfolio

    repo = open_repository()
    try:
        return build_target_portfolio(repo, trade_date=trade_date, persist=False,
                                      strategy=strategy, universe_id=universe)
    finally:
        repo.close()


@app.get("/api/tracking")
def tracking(start_date: str | None = None, strategy: str = "defensive",
             universe: str = "C0") -> dict:
    """模拟跟踪净值（按调仓快照 + 真实收盘价）。"""
    from ..strategy.portfolio import STRATEGY_NAMES
    from ..strategy.tracking import simulate_tracking

    base = STRATEGY_NAMES.get(strategy, strategy)
    name = base if universe == "C0" else f"{base}_{universe}"
    repo = open_repository()
    try:
        return simulate_tracking(repo, strategy=name, start_date=start_date, persist=False)
    finally:
        repo.close()


@app.get("/api/intraday")
def intraday() -> dict:
    """盘中实时快照（腾讯全市场 → 目标板块成分）。"""
    from ..datasource.registry import get_source
    from ..predict.intraday import intraday_snapshot

    return intraday_snapshot(get_source())


@app.post("/api/report")
def report() -> dict:
    """生成 AI 日报（DeepSeek；失败降级规则模板）。"""
    from ..report.daily import generate_daily_report

    return generate_daily_report()


@app.websocket("/ws/realtime")
async def ws_realtime(ws: WebSocket) -> None:
    """每 60 秒推送一次盘中快照。"""
    from ..datasource.registry import get_source
    from ..predict.intraday import intraday_snapshot

    await ws.accept()
    ds = get_source()
    try:
        while True:
            data = await asyncio.to_thread(intraday_snapshot, ds)
            await ws.send_json(data)
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        return
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send_json({"error": str(e)})
        except Exception:  # noqa: BLE001
            pass
