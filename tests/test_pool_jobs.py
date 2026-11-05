"""池级任务：幂等跳过、增量触发、成员变更判定、每日批量隔离（mock 重活）。"""

import pytest

from stock_cash_streaming.pipeline import daily, pool_jobs
from stock_cash_streaming.research.pool_admin import create_pool
from stock_cash_streaming.storage.repository import open_repository
from stock_cash_streaming.strategy import portfolio

LATEST = "2026-10-26"


@pytest.fixture()
def repo(tmp_path):
    r = open_repository(str(tmp_path / "test.sqlite3"))
    try:
        yield r
    finally:
        r.close()


def _seed(repo, *, backtest_end=LATEST, created_at="2026-10-26 10:00:00",
          members_updated_at="2026-10-01 09:00:00", daily_enabled=1,
          last_dates=(LATEST, LATEST), with_factors=True,
          with_portfolio=True, with_tracking=True):
    create_pool(repo, "测试池", [{"symbol": "600406", "name": "国电南瑞"},
                                 {"symbol": "600519", "name": "贵州茅台"}])
    now = "2026-10-26 08:00:00"
    repo.conn.execute(
        "UPDATE dim_universe SET members_updated_at=?, daily_enabled=? WHERE universe_id='U1'",
        (members_updated_at, daily_enabled))
    repo.conn.commit()

    row = {"symbol": "600406", "name": "国电南瑞", "market": "sh", "updated_at": now}
    row2 = {"symbol": "600519", "name": "贵州茅台", "market": "sh", "updated_at": now}
    repo.upsert("dim_stock", [row, row2], ["symbol"])
    repo.conn.execute(
        "UPDATE dim_stock SET ipo_date='2010-01-01' WHERE symbol IN ('600406','600519')")
    repo.conn.commit()

    quotes = [{"symbol": sym, "trade_date": last_dates[i], "close": 10.0, "amount": 1e8,
               "turnover": 1.0, "pct_chg": 0.0, "src": "test", "fetched_at": now}
              for i, sym in enumerate(("600406", "600519"))]
    repo.upsert("fact_quote", quotes, ["symbol", "trade_date"])
    repo.upsert("fact_financial", [
        {"symbol": sym, "stat_date": "2026-06-30", "pub_date": "2026-08-30",
         "metric": "net_profit", "value": 1.0, "src": "test", "fetched_at": now}
        for sym in ("600406", "600519")], ["symbol", "stat_date", "metric"])

    if with_factors:
        repo.upsert("fact_factor", [
            {"factor": "vol_20", "symbol": sym, "trade_date": LATEST,
             "version": "v2", "value": 20.0} for sym in ("600406", "600519")],
            ["factor", "symbol", "trade_date", "version"])

    repo.upsert("backtest_run", [{
        "run_id": "bt_test", "strategy": "defensive_top12_monthly_U1", "params": "{}",
        "start_date": "2018-01-01", "end_date": backtest_end, "data_version": "v2",
        "created_at": created_at,
    }], ["run_id"])

    if with_portfolio:
        repo.upsert("fact_target_run", [{
            "trade_date": LATEST, "strategy": "defensive_top12_monthly_U1",
            "is_rebalance": 1, "basket_vol": 0.2, "vol_scale": 1.0,
            "exposure": 0.9, "created_at": created_at,
        }], ["trade_date", "strategy"])
    if with_tracking:
        repo.upsert("fact_tracking_nav", [{
            "trade_date": LATEST, "strategy": "defensive_top12_monthly_U1", "nav": 1.0,
            "benchmark": 1.0, "exposure": 0.9, "drawdown": 0.0, "created_at": created_at,
        }], ["trade_date", "strategy"])


def _noop_mocks(monkeypatch, called):
    def _sync(*a, **k):
        called["sync"] += 1
        return {"price_ok": 0, "price_fail": 0, "flow_ok": 0, "flow_fail": 0}

    def _bt(*a, **k):
        called["bt"] += 1
        return {"run_id": "bt_mock", "start": "2018-01-01", "end": LATEST,
                "metrics": {"annual_return": 0.1, "sharpe": 1.0}}

    def _pf(*a, **k):
        called["pf"] += 1
        called["pf_kw"] = k
        return {"positions": [], "exposure": 0.0}

    def _tr(*a, **k):
        called["tr"] += 1
        called["tr_kw"] = k
        return {"status": "running", "days": 3}

    monkeypatch.setattr(pool_jobs, "sync_symbols", _sync)
    monkeypatch.setattr(pool_jobs, "run_backtest", _bt)
    monkeypatch.setattr(pool_jobs, "build_target_portfolio", _pf)
    monkeypatch.setattr(pool_jobs, "simulate_tracking", _tr)
    monkeypatch.setattr(pool_jobs, "compute_all", lambda *a, **k: {})
    monkeypatch.setattr(pool_jobs, "compute_fundamental_factors", lambda *a, **k: {})


def test_all_latest_skipped(repo, monkeypatch):
    _seed(repo)
    called = {"sync": 0, "bt": 0, "pf": 0, "tr": 0}
    _noop_mocks(monkeypatch, called)

    res = pool_jobs.run_pool_job(repo, "U1", lock_held=True)
    assert res["status"] == "skipped"
    assert called == {"sync": 0, "bt": 0, "pf": 0, "tr": 0}

    steps = {a["step"]: a["status"] for a in res["actions"]}
    assert steps["data"] == "skipped"
    assert steps["backtest"] == "skipped"
    assert steps["schedule"] == "skipped"

    job = pool_jobs.get_pool_job(repo, "U1")
    assert job["status"] == "skipped"
    assert job["actions"]


def test_stale_data_and_backtest_trigger(repo, monkeypatch):
    _seed(repo, backtest_end="2026-10-20", last_dates=(LATEST, "2026-10-24"),
          with_factors=False, with_portfolio=False, with_tracking=False)
    called = {"sync": 0, "bt": 0, "pf": 0, "tr": 0}
    _noop_mocks(monkeypatch, called)

    res = pool_jobs.run_pool_job(repo, "U1", lock_held=True)
    assert res["status"] == "ok"
    assert called["sync"] == 1
    assert called["bt"] == 1
    assert called["pf"] == 1
    assert called["tr"] == 1
    # 非 C0 池的组合/跟踪必须以该池为基准
    assert called["tr_kw"]["universe_id"] == "U1"
    assert called["pf_kw"]["universe_id"] == "U1"


def test_members_change_marks_backtest_stale(repo):
    _seed(repo, created_at="2026-10-01 10:00:00",
          members_updated_at="2026-10-20 09:00:00")
    state, _run = pool_jobs.backtest_state(repo, "U1", LATEST, "2026-10-20 09:00:00")
    assert state == "stale"


def test_enabled_pool_jobs_isolated(repo, monkeypatch):
    _seed(repo)
    create_pool(repo, "第二池", [{"symbol": "600036", "name": "招商银行"}])
    repo.conn.execute(
        "UPDATE dim_universe SET daily_enabled=1 WHERE universe_id IN ('U1','U2')")
    repo.conn.commit()

    def _fake(repo_, pool_id, **kwargs):
        if pool_id == "U2":
            raise RuntimeError("boom")
        return {"status": "ok",
                "actions": [{"step": "data", "status": "ok", "message": ""}]}

    monkeypatch.setattr(pool_jobs, "run_pool_job", _fake)
    out = pool_jobs.run_enabled_pool_jobs(repo)
    # C0 也纳入每日池任务（只补数据/因子/回测），因此一并返回
    assert set(out) == {"C0", "U1", "U2"}
    assert out["C0"]["status"] == "ok"
    assert out["U1"]["status"] == "ok"
    assert out["U2"]["status"] == "failed"


# ---------- 每日流水线：行情发布完整性护栏 ----------

def test_quote_coverage_detects_partial_day(repo):
    """数据源分批发布日线时，当日覆盖标的数应明显少于上一交易日。"""
    now = "2026-11-05 16:30:00"
    rows = [{"symbol": f"60000{i}", "trade_date": "2026-11-04", "close": 10.0,
             "amount": 1e8, "src": "test", "fetched_at": now} for i in range(5)]
    rows.append({"symbol": "600000", "trade_date": "2026-11-05", "close": 10.0,
                 "amount": 1e8, "src": "test", "fetched_at": now})
    repo.upsert("fact_quote", rows, ["symbol", "trade_date"])
    # 日更股票池只统计池成员，全 A 研究行情不参与判断
    repo.upsert("dim_universe_member",
                [{"universe_id": "C1", "symbol": f"60000{i}", "sector_code": "S1"}
                 for i in range(5)], ["universe_id", "symbol"])
    assert daily._quote_coverage(repo) == (1, 5)


def test_with_timeout_skips_hung_call():
    """数据源长连接不返回时，辅助步骤应超时跳过而不是卡住流水线。"""
    import time as _t

    from stock_cash_streaming.ingest.incremental import _with_timeout

    assert _with_timeout(lambda: _t.sleep(1.5) or "x", 0.1, "default") == "default"
    assert _with_timeout(lambda: 42, 1, None) == 42


def test_target_portfolio_returns_error_when_no_scores(repo):
    """评分整列为空（截面数据不完整）时返回错误而不是除零中断。"""
    create_pool(repo, "测试池", [{"symbol": "600406", "name": "国电南瑞"}])
    now = "2026-11-05 08:00:00"
    repo.upsert("dim_stock", [{"symbol": "600406", "name": "国电南瑞",
                               "market": "sh", "updated_at": now}], ["symbol"])
    repo.upsert("fact_factor", [
        {"factor": f, "symbol": "600406", "trade_date": LATEST, "version": "v2", "value": v}
        for f, v in (("vol_20", 20.0), ("amount_20d", 0.0), ("ret_20", 1.0))
    ], ["factor", "symbol", "trade_date", "version"])
    res = portfolio.build_target_portfolio(repo, trade_date=LATEST, universe_id="U1")
    assert res["positions"] == []
    assert "error" in res
