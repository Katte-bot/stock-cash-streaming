"""自然语言策略 DSL：校验、指标、周线重采样、执行时点与组合回测。"""

import sys
import types

import pandas as pd
import pytest

from stock_cash_streaming.research import nl_rules
from stock_cash_streaming.research.nl_rules import (
    SpecError,
    StrategyParseError,
    build_signal_matrix,
    delete_strategy,
    describe_spec,
    get_strategy,
    list_strategies,
    parse_nl_strategy,
    resample_bars,
    run_nl_backtest,
    save_strategy,
    simulate_rule_portfolio,
    to_execution_signals,
    validate_spec,
)
from stock_cash_streaming.storage.repository import open_repository

DATES = list(pd.bdate_range("2024-01-01", periods=20).strftime("%Y-%m-%d"))


@pytest.fixture()
def repo(tmp_path):
    r = open_repository(str(tmp_path / "test.sqlite3"))
    try:
        yield r
    finally:
        r.close()


def _spec(**kw) -> dict:
    base = {"name": "连阴测试", "timeframe": "D",
            "entry": {"all": [{"indicator": "consecutive_down", "params": {"n": 3}}]},
            "hold_days": 2}
    base.update(kw)
    return base


def _panels(closes: dict[str, list[float]], pct: dict[str, float] | None = None):
    close = pd.DataFrame(closes, index=DATES[:len(next(iter(closes.values())))])
    pct_df = pd.DataFrame(pct or {c: 0.0 for c in close.columns}, index=close.index)
    return close, pct_df


def test_validate_spec_defaults_and_normalize():
    spec = validate_spec({"timeframe": "w", "entry": [
        {"indicator": "macd_cross", "params": {"direction": "金叉"}},
        {"indicator": "rsi", "params": {"op": "≤", "value": 30}},
    ]})
    assert spec["timeframe"] == "W"
    assert spec["hold_days"] == 5                       # 未给则默认 5
    assert spec["entry"]["all"][0]["params"]["fast"] == 12
    assert spec["entry"]["all"][0]["params"]["direction"] == "golden"
    assert spec["entry"]["all"][1]["params"]["op"] == "le"
    # entry 允许直接传列表
    assert validate_spec({"timeframe": "D", "entry": [
        {"indicator": "consecutive_up", "params": {"n": 2}}]})["entry"]["all"][0]["params"]["n"] == 2


@pytest.mark.parametrize("raw", [
    {"timeframe": "M", "entry": [{"indicator": "rsi", "params": {}}]},
    {"timeframe": "D", "entry": []},
    {"timeframe": "D", "entry": [{"indicator": "future_magic", "params": {}}]},
    {"timeframe": "D", "entry": [{"indicator": "macd_cross", "params": {"fast": 30, "slow": 10}}]},
    {"timeframe": "D", "hold_days": 0, "entry": [{"indicator": "rsi", "params": {}}]},
    {"timeframe": "D", "hold_days": 61, "entry": [{"indicator": "rsi", "params": {}}]},
    {"timeframe": "D", "entry": [{"indicator": "rsi", "params": {"value": 120}}]},
    {"timeframe": "D", "entry": [{"indicator": "rsi", "params": {}}] * 6},
])
def test_validate_spec_rejects(raw):
    with pytest.raises(SpecError):
        validate_spec(raw)


def test_describe_spec():
    lines = describe_spec(validate_spec(_spec(timeframe="W", hold_days=5)))
    text = " ".join(lines)
    assert "周线" in text and "5 个交易日" in text and "连续 3 根阴线" in text


def test_resample_bars_labels_last_trading_day():
    close, _ = _panels({"600001": [100 + i for i in range(20)]})
    volume = pd.DataFrame(1_000, index=close.index, columns=close.columns)
    w_close, w_vol, bars = resample_bars(close, volume, "W")
    assert len(bars) == len(w_close) == 4
    # 每周 bar 的标签必须是该周最后一个交易日（便于次一交易日成交）
    assert bars[-1] == DATES[19]
    assert w_close.iloc[-1, 0] == close.iloc[-1, 0]
    assert w_vol.iloc[0, 0] == 5_000


def test_consecutive_down_signal_and_next_day_execution():
    close, _ = _panels({"600001": [100 - i for i in range(10)]})
    volume = pd.DataFrame(1_000, index=close.index, columns=close.columns)
    sig = build_signal_matrix(close, volume, validate_spec(_spec()))
    assert list(sig["600001"]) == [False, False, False, True, True, True, True, True, True, True]

    exec_sig = to_execution_signals(sig, list(close.index))
    assert bool(exec_sig.loc[DATES[4], "600001"]) is True      # 第 4 天信号 → 第 5 天执行
    assert bool(exec_sig.loc[DATES[3], "600001"]) is False


def test_simulate_rule_portfolio_buy_and_sell():
    # 单调上涨，买入后持有 2 日必然盈利
    close, pct = _panels({"600001": [10 + 0.5 * i for i in range(12)]})
    signals = pd.DataFrame(False, index=close.index, columns=close.columns)
    signals.loc[DATES[1], "600001"] = True

    res = simulate_rule_portfolio(close, pct, signals, capital=100_000,
                                  top_n=1, hold_days=2)
    assert res["status"] == "ok"
    assert len(res["nav"]) == len(close)
    assert len(res["trades"]) == 1
    t = res["trades"][0]
    assert t["entry_date"] == DATES[1] and t["exit_date"] == DATES[3]
    assert t["hold_days"] == 2 and t["shares"] % 100 == 0
    assert t["pnl"] > 0
    assert res["trade_stats"]["win_rate"] == pytest.approx(1.0)
    for row in res["nav"]:
        assert abs(row["nav"] - (row["cash"] + row["market_value"])) < 0.02


def test_simulate_rule_portfolio_skips_limit_up():
    close, _ = _panels({"600001": [10 + 0.5 * i for i in range(12)]})
    pct = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    pct.loc[DATES[1], "600001"] = 10.0                    # 主板涨停（阈值 9.8%）
    signals = pd.DataFrame(False, index=close.index, columns=close.columns)
    signals.loc[DATES[1], "600001"] = True

    res = simulate_rule_portfolio(close, pct, signals, capital=100_000,
                                  top_n=1, hold_days=2)
    assert res["trades"] == []


def test_strategy_crud(repo):
    spec = validate_spec(_spec())
    result = {"metrics": {"total_return": 0.2, "annual_return": 0.1, "sharpe": 1.2,
                          "max_drawdown": -0.15},
              "trade_stats": {"trades": 8, "win_rate": 0.625},
              "params": {"top_n": 5, "start": DATES[0], "end": DATES[-1]}}

    saved = save_strategy(repo, spec, "连续三根阴线后买入", "C0", result, "测试策略")
    assert saved["strategy_id"] == "NL1"
    assert saved["name"] == "测试策略"
    assert saved["spec"]["timeframe"] == "D"
    assert saved["last_summary"]["win_rate"] == pytest.approx(0.625)
    assert saved["describe"]

    items = list_strategies(repo)
    assert [x["strategy_id"] for x in items] == ["NL1"]
    assert get_strategy(repo, "NL1")["nl_text"].startswith("连续三根")

    assert delete_strategy(repo, "NL1") is True
    assert get_strategy(repo, "NL1") is None
    assert list_strategies(repo) == []


def _fake_openai(payload: str):
    class Completions:
        def create(self, **kwargs):
            msg = types.SimpleNamespace(content=payload)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    class OpenAI:
        def __init__(self, **kwargs):
            self.chat = types.SimpleNamespace(completions=Completions())

    return types.SimpleNamespace(OpenAI=OpenAI)


def test_parse_nl_strategy(monkeypatch):
    payload = ('{"name":"周线9连阴","timeframe":"W",'
               '"entry":{"all":[{"indicator":"consecutive_down","params":{"n":9}}]},'
               '"hold_days":5}')
    monkeypatch.setitem(sys.modules, "openai", _fake_openai(payload))
    monkeypatch.setattr(nl_rules, "get_settings", lambda: types.SimpleNamespace(
        openai_api_key="test-key", openai_base_url="http://localhost", llm_model="test"))

    spec = parse_nl_strategy("周K线9连阴后买入并持仓1周")
    assert spec["timeframe"] == "W" and spec["hold_days"] == 5
    assert spec["entry"]["all"][0]["params"]["n"] == 9


def test_parse_nl_strategy_errors(monkeypatch):
    # 未配置 LLM
    monkeypatch.setattr(nl_rules, "get_settings", lambda: types.SimpleNamespace(
        openai_api_key="", openai_base_url="", llm_model=""))
    with pytest.raises(StrategyParseError):
        parse_nl_strategy("随便什么")

    # 模型返回非法指标
    bad = '{"timeframe":"D","entry":{"all":[{"indicator":"magic","params":{}}]}}'
    monkeypatch.setitem(sys.modules, "openai", _fake_openai(bad))
    monkeypatch.setattr(nl_rules, "get_settings", lambda: types.SimpleNamespace(
        openai_api_key="k", openai_base_url="http://localhost", llm_model="m"))
    with pytest.raises(StrategyParseError):
        parse_nl_strategy("魔法指标")


def test_run_nl_backtest(repo, monkeypatch):
    days = list(pd.bdate_range("2023-01-02", periods=150).strftime("%Y-%m-%d"))
    prices, p = [], 20.0
    for i in range(len(days)):
        p *= 0.96 if (i % 6) < 3 else 1.05   # 每 6 日一轮「三连阴 + 三连阳」
        prices.append(round(p, 2))
    close = pd.Series(prices, index=days)
    pct = close.pct_change().fillna(0.0) * 100

    repo.upsert("fact_quote", [
        {"symbol": "600001", "trade_date": d, "open": float(close[d]), "high": float(close[d]),
         "low": float(close[d]), "close": float(close[d]), "volume": 1_000_000.0,
         "amount": float(close[d]) * 1_000_000, "pct_chg": float(pct[d]),
         "turnover": 1.0, "adj_factor": 1.0, "src": "test", "fetched_at": "2024-01-01"}
        for d in days
    ], ["symbol", "trade_date"])
    monkeypatch.setattr(nl_rules, "universe_members", lambda repo, uid: ["600001"])

    res = run_nl_backtest(repo, _spec(), "C0", top_n=1)
    assert res["universe"] == "C0"
    assert len(res["nav"]) == len(days)
    assert res["metrics"]["annual_return"] is not None
    assert res["params"]["hold_days"] == 2
    assert res["trades"], "应产生交易"
    assert all(t["exit_date"] > t["entry_date"] for t in res["trades"])
    assert all(t["hold_days"] >= 1 for t in res["trades"])
    assert any(row["benchmark"] for row in res["nav"])
