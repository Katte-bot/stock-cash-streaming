"""模拟持仓（纸面账户）：费用、整手回放、事件生成、幂等与重建。"""

import numpy as np
import pandas as pd
import pytest

from stock_cash_streaming.strategy import paper
from stock_cash_streaming.strategy.paper import (
    _events_from_weights,
    simulate_ledger,
    trade_fee,
)
from stock_cash_streaming.storage.repository import open_repository

DATES = list(pd.bdate_range("2024-01-01", periods=60).strftime("%Y-%m-%d"))


def _prices() -> pd.DataFrame:
    return pd.DataFrame({
        "600001": np.linspace(10, 12, len(DATES)),
        "600002": np.linspace(20, 18, len(DATES)),
    }, index=DATES)


def _events() -> list[tuple[str, dict[str, float]]]:
    return [(DATES[0], {"600001": 0.5, "600002": 0.3}),
            (DATES[40], {"600001": 0.0, "600002": 0.6})]


def test_trade_fee():
    # 佣金最低 5 元 + 过户费万 0.1
    assert trade_fee("buy", "2026-01-05", 10_000) == pytest.approx(5.1)
    # 卖出另加印花税 0.05%（2023-08-28 之后）
    assert trade_fee("sell", "2026-01-05", 10_000) == pytest.approx(10.1)
    # 2023-08-28 之前为 0.1%
    assert trade_fee("sell", "2023-08-01", 10_000) == pytest.approx(15.1)
    # 大额按费率计算：佣金 250 + 过户费 10
    assert trade_fee("buy", "2026-01-05", 1_000_000) == pytest.approx(260.0)
    assert trade_fee("buy", "2026-01-05", 0) == 0.0


def test_simulate_ledger():
    ledger = simulate_ledger(_prices(), _events(), 100_000)

    assert ledger["status"] == "ok"
    assert len(ledger["nav"]) == len(DATES)
    # 整手成交
    assert all(t["shares"] % paper.LOT == 0 for t in ledger["trades"])
    # 每日总资产 = 现金 + 持仓市值
    for n in ledger["nav"]:
        # 净值按 2 位小数落库，允许 1 分钱误差
        assert abs(n["total_value"] - (n["cash"] + n["market_value"])) < 0.02
        assert n["cash"] >= 0
    # 首次买入、第二次调仓出现卖出
    sides = [t["side"] for t in ledger["trades"]]
    assert sides[0] == "buy" and "sell" in sides
    # 第二次调仓后 600001 清仓
    assert {p["symbol"] for p in ledger["positions"]} == {"600002"}
    assert ledger["rebalances"] == 2


def test_ledger_idempotent():
    a = simulate_ledger(_prices(), _events(), 100_000)
    b = simulate_ledger(_prices(), _events(), 100_000)
    assert a["trades"] == b["trades"]
    assert a["nav"] == b["nav"]


def test_events_from_weights():
    dates = DATES[:5]
    w = pd.DataFrame({"a": 0.0, "b": 0.0}, index=dates)
    w.iloc[1] = [0.5, 0.5]
    w.iloc[2] = [0.5, 0.5]
    w.iloc[3] = [0.0, 0.9]
    w.iloc[4] = [0.0, 0.9]
    events = _events_from_weights(w)
    # 权重变化日 d 的权重在 d-1 收盘执行
    assert [e["exec_date"] for e in events] == [dates[0], dates[2]]
    assert events[1]["weights"] == {"b": pytest.approx(0.9)}
    assert all(e["source"] == "backtest" for e in events)


@pytest.fixture()
def repo(tmp_path):
    r = open_repository(str(tmp_path / "test.sqlite3"))
    try:
        yield r
    finally:
        r.close()


def _seed_quotes(repo, symbols=("600001", "600002")):
    rows = []
    for s in symbols:
        for i, d in enumerate(DATES):
            rows.append({"symbol": s, "trade_date": d,
                         "close": 10.0 + i * 0.02, "src": "test",
                         "fetched_at": "2026-01-01 00:00:00"})
    repo.upsert("fact_quote", rows, ["symbol", "trade_date"])


def test_rebuild_and_reset(repo, monkeypatch):
    _seed_quotes(repo)
    weights = pd.DataFrame({"600001": 0.0, "600002": 0.0}, index=DATES)
    weights.iloc[1:] = [0.5, 0.4]

    monkeypatch.setattr(paper, "universe_members",
                        lambda repo_, uid: {"600001": "电网设备", "600002": "天然气"})
    monkeypatch.setattr(paper, "eligibility_panel", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(paper, "run_backtest", lambda *a, **k: {"weights": weights})
    # 隔离跨进程锁，避免测试占用 data/pool_job.lock
    monkeypatch.setattr("stock_cash_streaming.pipeline.pool_jobs.try_acquire_job_lock",
                        lambda: object())
    monkeypatch.setattr("stock_cash_streaming.pipeline.pool_jobs.release_job_lock",
                        lambda handle: None)

    res = paper.rebuild_paper_account(repo, "C0", 100_000, acquire_lock=False)
    assert res["status"] == "ok"
    acc = paper.get_account(repo, "C0")
    assert acc["initial_capital"] == 100_000
    assert repo.count("fact_paper_trade") > 0
    assert repo.count("fact_paper_position") == 2
    first_total = acc["total_value"]

    summary = paper.paper_summary(repo, "C0", trade_limit=5)
    assert summary["status"] == "ok"
    assert summary["account"]["trades_count"] == repo.count("fact_paper_trade")
    assert len(summary["trades"]) == min(5, summary["account"]["trades_count"])
    assert summary["metrics"]["days"] == len(DATES)

    res2 = paper.reset_paper_account(repo, "C0", 200_000)
    assert res2["status"] == "ok"
    acc2 = paper.get_account(repo, "C0")
    assert acc2["initial_capital"] == 200_000
    assert abs(acc2["total_value"] / first_total - 2) < 0.05

    # 未开放的池返回未初始化
    assert paper.paper_summary(repo, "C1")["status"] == "not_initialized"
