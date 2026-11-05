"""策略研究平台：因子注册表、v4/v5 因子、横截面打分、策略库、因子动量、IC 与自然语言配置。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_cash_streaming.features import registry as freg
from stock_cash_streaming.features.fundamental_v5 import _align, _report_frame
from stock_cash_streaming.features.panel import Panel
from stock_cash_streaming.features.technical_v4 import compute_v4
from stock_cash_streaming.research import factor_momentum as fm
from stock_cash_streaming.research import nl_config
from stock_cash_streaming.research.ic import compute_ic, summarize
from stock_cash_streaming.research.nl_config import SpecError
from stock_cash_streaming.storage.repository import open_repository
from stock_cash_streaming.strategy import library, registry, scoring

SYMBOLS = [f"6000{i:02d}" for i in range(10)]


@pytest.fixture()
def repo(tmp_path):
    r = open_repository(str(tmp_path / "test.sqlite3"))
    try:
        yield r
    finally:
        r.close()


def _panel(n_days: int = 320, n_sym: int = 6, seed: int = 7) -> Panel:
    dates = pd.bdate_range("2023-01-02", periods=n_days).strftime("%Y-%m-%d")
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0003, 0.02, (n_days, n_sym))
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=dates,
                         columns=SYMBOLS[:n_sym])
    ret = close.pct_change()
    flat = lambda v: pd.DataFrame(v, index=dates, columns=close.columns)  # noqa: E731
    return Panel(
        close=close.astype("float32"), ret=ret.astype("float32"),
        pct=(ret * 100).astype("float32"), amount=flat(1e8), turnover=flat(1.5),
        volume=flat(1e6), eligibility=pd.DataFrame(True, index=dates, columns=close.columns),
        open=close.astype("float32"), high=(close * 1.01).astype("float32"),
        low=(close * 0.99).astype("float32"),
        sector_map={s: ("S1" if i % 2 == 0 else "S2") for i, s in enumerate(close.columns)},
    )


# ---------- 注册表 ----------

def test_factor_registry_versions():
    v4 = freg.by_version("v4")
    v5 = freg.by_version("v5")
    assert len(v4) == 20 and len(v5) == 17
    names = freg.all_names()
    assert len(names) == len(set(names)) == 68          # 命名唯一
    assert freg.get("resid_mom_252").higher_is_better is True
    assert freg.get("vol_120").higher_is_better is False
    assert {"vol_120", "capex_to_assets", "sue"} <= set(names)


def test_strategy_registry_registered():
    registry.ensure_registered()
    for sid in ("value_quality", "residual_momentum", "conservative_investment",
                "pead", "factor_momentum", "low_vol_multidim", "defensive", "nl_factor"):
        assert registry.get(sid) is not None, sid
    assert registry.get("defensive").builder is library.build_defensive


# ---------- v4 因子 ----------

def test_v4_no_lookahead():
    panel = _panel(n_days=320)
    full = compute_v4(panel)
    t = panel.dates[-60]
    trunc = compute_v4(panel.slice(end=t))
    cols = ["vol_60", "vol_120", "downside_vol_120", "ivol_60", "ret_60", "ret_120",
            "resid_ret_5", "resid_ret_10", "resid_ret_20", "ts_trend_60",
            "dist_high_60", "lower_shadow_20", "volume_shrink_5_20", "amihud_std_20"]
    for col in cols:
        a = full[col].loc[t]
        b = trunc[col].loc[t]
        assert np.allclose(a.to_numpy(dtype=float), b.to_numpy(dtype=float),
                           equal_nan=True), f"未来函数泄漏: {col}"


def test_v4_residual_momentum_skips_last_month():
    """残差动量应剔除最近 21 个交易日，避免短期反转污染。"""
    panel = _panel(n_days=320)
    bumped = panel.slice()
    last = bumped.dates[-1]
    bumped.ret.loc[last] = bumped.ret.loc[last] * 3
    f = compute_v4(panel)
    f2 = compute_v4(bumped)
    assert np.allclose(f["resid_mom_126"].loc[last].to_numpy(dtype=float),
                       f2["resid_mom_126"].loc[last].to_numpy(dtype=float), equal_nan=True)
    assert not np.allclose(f["resid_ret_5"].loc[last].to_numpy(dtype=float),
                           f2["resid_ret_5"].loc[last].to_numpy(dtype=float), equal_nan=True)


# ---------- v5 因子 ----------

def _fin_rows(symbol: str = "600000", quarters: int = 12) -> list[dict]:
    rows = []
    periods = pd.period_range("2022Q1", periods=quarters, freq="Q")
    for i, p in enumerate(periods):
        stat = f"{p.year}-{p.month:02d}-{p.end_time.day:02d}"
        pub = (p.end_time + pd.Timedelta(days=40)).strftime("%Y-%m-%d")
        q = i + 1
        assets = 1000.0 * (1.1 ** (i // 4))         # 每 4 个季度（一年）增长 10%
        base = {"symbol": symbol, "stat_date": stat, "pub_date": pub,
                "src": "test", "fetched_at": "2026-01-01 00:00:00"}
        for metric, value in {
            "revenue": 100.0 * q, "net_profit": 10.0 * q, "ocf": 12.0 * q,
            "equity": assets * 0.6, "bps": 3.0, "total_assets": assets,
            "capex": 8.0 * q,
        }.items():
            rows.append({**base, "metric": metric, "value": value})
    return rows


def test_v5_report_frame_metrics(repo):
    rows = _fin_rows()
    repo.upsert("fact_financial", rows, ["symbol", "stat_date", "metric"])
    wide = _report_frame(repo, ["600000"])
    assert not wide.empty
    last = wide.iloc[-1]
    # 最新 4 季总资产较前一年同期增长 10%
    assert last["asset_growth"] == pytest.approx(0.1, rel=0.02)
    assert 0 < last["capex_to_assets"] < 1
    assert last["roa_ttm"] > 0
    assert np.isfinite(last["accruals"])


def test_report_panel_derives_income_metrics(repo):
    """只入利润表/资产负债表科目时，净资产、股本、经营现金流应能派生。"""
    rows = []
    for i in range(8):
        year, q = 2024 + i // 4, i % 4 + 1
        mmdd = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[q]
        stat = f"{year}-{mmdd}"
        pub = (pd.Timestamp(stat) + pd.Timedelta(days=40)).strftime("%Y-%m-%d")
        revenue = 1000.0 * q                  # 年内累计
        net_profit = revenue * 0.1
        assets = 10000.0 * (1 + 0.02 * i)
        base = {"symbol": "600001", "stat_date": stat, "pub_date": pub,
                "src": "test", "fetched_at": "2026-01-01 00:00:00"}
        for metric, value in {
            "revenue": revenue, "net_profit": net_profit, "eps": net_profit / 1000.0,
            "ocf_ps": net_profit / 1000.0 * 1.2, "total_assets": assets,
            "total_liabilities": assets * 0.4, "capex": revenue * 0.05,
        }.items():
            rows.append({**base, "metric": metric, "value": value})
    repo.upsert("fact_financial", rows, ["symbol", "stat_date", "metric"])

    wide = _report_frame(repo, ["600001"])
    last = wide.iloc[-1]
    assert last["equity"] == pytest.approx(last["total_assets"] * 0.6, rel=1e-6)
    assert last["shares"] == pytest.approx(1000.0, rel=1e-3)
    assert last["ocf_ttm"] == pytest.approx(last["net_profit_ttm"] * 1.2, rel=1e-3)
    assert last["accruals"] < 0


def test_load_quotes_filters_symbols_and_bad_prices(repo):
    """按池加载行情必须在 SQL 层过滤；单日 > 50% 的价格断点标的应被剔除。"""
    from stock_cash_streaming.backtest.engine import _load_quotes

    dates = pd.bdate_range("2024-01-02", periods=5).strftime("%Y-%m-%d")
    rows = []
    for sym, jump in (("600001", False), ("600002", True)):
        px = 10.0
        for i, d in enumerate(dates):
            if jump and i == 2:
                px = px * 100          # 复权基准混用的断点
            rows.append({"symbol": sym, "trade_date": d, "open": px, "high": px,
                         "low": px, "close": px, "volume": 1e6, "amount": 1e7,
                         "pct_chg": 0.0, "turnover": 1.0, "adj_factor": None,
                         "src": "test", "fetched_at": "2026-01-01 00:00:00"})
    repo.upsert("fact_quote", rows, ["symbol", "trade_date"])

    close, ret, pct = _load_quotes(repo, ["600001"])
    assert list(close.columns) == ["600001"]
    assert len(close.index) == 5

    close_all, _, _ = _load_quotes(repo)
    assert "600002" not in close_all.columns      # 价格断点标的被剔除
    assert "600001" in close_all.columns


def test_v5_align_uses_publication_date():
    dates = pd.Index(["2024-04-25", "2024-04-30", "2024-05-06", "2024-05-10"])
    rep = pd.DataFrame({
        "pub_date": ["2024-04-26", "2024-05-06"],
        "roa_ttm": [5.0, 6.0],
    })
    out = _align(rep, dates, ["roa_ttm"])
    assert np.isnan(out.loc["2024-04-25", "roa_ttm"])            # 公告前不可见
    assert out.loc["2024-04-30", "roa_ttm"] == 5.0
    assert out.loc["2024-05-06", "roa_ttm"] == 6.0                # 公告当日可见
    assert out.loc["2024-05-10", "roa_ttm"] == 6.0


# ---------- 横截面打分 ----------

def test_scoring_winsorize_zscore_neutral():
    df = pd.DataFrame({"a": [1.0, 2.0, 3.0, 1000.0], "b": [1.0, 1.0, 1.0, 1.0]}).T
    w = scoring.winsorize(df)
    assert w.iloc[0, 3] < 1000.0                                 # 缩尾
    z = scoring.zscore(w)
    assert z.iloc[0].mean() == pytest.approx(0.0, abs=1e-6)
    assert z.iloc[0].std() == pytest.approx(1.0, abs=1e-6)       # 样本标准差
    panel = _panel()
    zz = pd.DataFrame([[1.0, 2.0, 3.0, 4.0]], columns=list("abcd"))
    neutral = scoring.industry_neutral(zz, {"a": "S1", "b": "S1", "c": "S2", "d": "S2"})
    assert neutral.loc[0, ["a", "b"]].mean() == pytest.approx(0.0, abs=1e-6)
    assert neutral.loc[0, ["c", "d"]].mean() == pytest.approx(0.0, abs=1e-6)
    assert panel.close.shape[0] == 320


def test_scoring_composite_direction():
    dates = ["2024-01-02"]
    up = pd.DataFrame([[3.0, 2.0, 1.0]], index=dates, columns=list("abc"))
    score = scoring.composite({"vol_120": up}, {"vol_120": 1.0}, neutral=False)
    assert score.iloc[0, 0] < score.iloc[0, 2]                   # 低波动得高分
    assert scoring.top_symbols(score.iloc[0], 2) == ["c", "b"]


# ---------- 策略库 ----------

def test_build_value_quality_drops_negative_ep():
    dates = ["2024-01-02"]
    factors = {n: pd.DataFrame([[1.0, 1.0, 1.0]], index=dates, columns=list("abc"))
               for n in library.VALUE_QUALITY_WEIGHTS}
    factors["ep_ttm"] = pd.DataFrame([[-0.1, 0.2, 0.3]], index=dates, columns=list("abc"))
    panel = Panel(close=pd.DataFrame(columns=list("abc")), ret=pd.DataFrame(),
                  pct=pd.DataFrame(), amount=pd.DataFrame(), turnover=pd.DataFrame(),
                  volume=pd.DataFrame(), eligibility=pd.DataFrame())
    score = library.build_value_quality(panel, factors, {})
    assert np.isnan(score.loc[dates[0], "a"])
    assert np.isfinite(score.loc[dates[0], "b"])


def test_build_custom_requires_weights():
    panel = _panel()
    assert library.build_custom(panel, {}, {}).empty
    factors = {"vol_120": pd.DataFrame([[1.0, 2.0]], index=["2024-01-02"], columns=list("ab"))}
    out = library.build_custom(panel, factors, {"factor_weights": {"vol_120": 1.0},
                                                "min_factors": 1})
    assert not out.empty


# ---------- 因子动量 ----------

def test_factor_momentum_weights_capped():
    dates = list(pd.bdate_range("2020-01-01", periods=400).strftime("%Y-%m-%d"))
    rng = np.random.default_rng(3)
    comps = {f"s{i}": pd.Series(np.cumsum(rng.normal(0.001 * i, 0.01, len(dates))),
                                index=dates)
             for i in range(5)}
    weights = fm.build_weights(comps, dates[250::20])
    assert weights
    for w in weights.values():
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)
        assert max(w.values()) <= fm.MAX_WEIGHT + 1e-9


def test_factor_momentum_meta_score_shape():
    dates = list(pd.bdate_range("2024-01-02", periods=6).strftime("%Y-%m-%d"))
    comps = {"a": pd.DataFrame(1.0, index=dates, columns=list("xy")),
             "b": pd.DataFrame(2.0, index=dates, columns=list("xy"))}
    weights = {dates[0]: {"a": 0.5, "b": 0.5}}
    score = fm.build_meta_score(comps, weights, dates)
    assert list(score.columns) == ["x", "y"]
    assert score.dtypes.iloc[0] == np.dtype("float64")


# ---------- IC ----------

def test_ic_detects_predictive_factor():
    panel = _panel(n_days=120, n_sym=10, seed=11)
    fwd = panel.ret.shift(-1)                                     # 次日收益
    ic_df = compute_ic(panel, {"perfect": fwd}, horizon=1, min_n=5)
    summary = summarize(ic_df)
    assert summary["perfect"]["mean_ic"] > 0.9                     # 完全相关
    assert summary["perfect"]["positive_ratio"] == 1.0


# ---------- 自然语言配置 ----------

def test_nl_config_normalizes_weights():
    cfg = nl_config.validate_config({
        "factors": [{"name": "low_volatility", "weight": 0.4, "metrics": ["VOL_120"]},
                    {"name": "value", "weight": 0.3, "metrics": ["EP", "BP"]},
                    {"name": "quality", "weight": 0.3, "metrics": ["ROE"]}],
        "portfolio": {"stock_count": 8, "rebalance": "weekly"},
    })
    assert sum(f["weight"] for f in cfg["factors"]) == pytest.approx(1.0)
    weights = nl_config.config_to_weights(cfg)
    assert weights["vol_120"] == pytest.approx(0.4)
    assert weights["ep_ttm"] == pytest.approx(0.15)
    assert cfg["portfolio"]["stock_count"] == 8


@pytest.mark.parametrize("payload,msg", [
    ({"factors": [{"name": "unknown", "weight": 0.5}]}, "不支持的因子组"),
    ({"factors": [{"name": "value", "weight": 0.5, "metrics": ["NOPE"]}]}, "不支持指标"),
    ({"factors": [{"name": "low_volatility", "weight": 0.8},
                  {"name": "value", "weight": 0.2}]}, "单组上限"),
    ({"factors": [{"name": "value", "weight": 0.4},
                  {"name": "quality", "weight": 0.3},
                  {"name": "low_volatility", "weight": 0.3}],
      "portfolio": {"rebalance": "hourly"}}, "rebalance"),
    ({"factors": [{"name": "value", "weight": 0.4}], "universe": {"id": "X9"}}, "universe"),
])
def test_nl_config_rejects_invalid(payload, msg):
    with pytest.raises(SpecError) as e:
        nl_config.validate_config(payload)
    assert msg in str(e.value)


# ---------- 回测约束与稳健性 ----------

def test_v4_all_factors_no_lookahead():
    panel = _panel(n_days=420)
    full = compute_v4(panel)
    t = panel.dates[-40]
    trunc = compute_v4(panel.slice(end=t))
    assert set(full) == set(trunc)
    for name in full:
        a = full[name].loc[t].to_numpy(dtype=float)
        b = trunc[name].loc[t].to_numpy(dtype=float)
        assert np.allclose(a, b, equal_nan=True), f"未来函数泄漏: {name}"


def test_capacity_constraint_caps_weight(repo):
    from stock_cash_streaming.backtest.engine import run_backtest

    panel = _panel(n_days=90, n_sym=2)
    symbols = list(panel.close.columns)
    score = pd.DataFrame(np.nan, index=panel.close.index, columns=symbols)
    score.iloc[:, 0] = 10.0                      # 只买第一只
    score.iloc[:, 1] = 1.0
    amount = panel.amount.copy()
    amount.iloc[:, 0] = 1_000_000.0              # 20 日均额 100 万 → 上限 1% = 1 万
    amount.iloc[:, 1] = 1e9
    res = run_backtest(
        repo, top_k=1, rebalance="monthly", persist=False,
        score_override=score,
        panel={**{"close": panel.close, "ret": panel.ret, "pct": panel.pct,
                  "piv": {"vol_20": pd.DataFrame(20.0, index=panel.close.index,
                                                 columns=symbols)},
                  "sector_map": {}, "st_symbols": set(), "eligibility": panel.eligibility,
                  "amount": amount}},
        initial_capital=1_000_000, cost_model="a_share", limit_model="board",
        capacity_pct=0.01, return_weights=True,
    )
    w = res["weights"]
    assert float(w.max().max()) <= 0.0105        # 单票被容量上限约束
    assert res["metrics"]["avg_exposure"] < 0.05


def test_walkforward_segments():
    from stock_cash_streaming.research.walkforward import summarize, walk_forward

    dates = pd.bdate_range("2019-01-01", "2025-12-31").strftime("%Y-%m-%d")
    nav = pd.Series(np.linspace(1.0, 2.0, len(dates)), index=dates)
    bench = pd.Series(np.linspace(1.0, 1.3, len(dates)), index=dates)
    segs = walk_forward(nav, bench)
    assert len(segs) == 4                        # 2022/2023/2024/2025
    assert all({"test_start", "test_end", "test_annual", "passed"} <= set(s) for s in segs)
    summary = summarize(segs)
    assert summary["segments"] == 4 and 0 <= summary["pass_rate"] <= 1


def test_strategy_registry_rejects_duplicate():
    dup = registry.StrategySpec(id="value_quality", name="x", category="test",
                                description="")
    with pytest.raises(ValueError):
        registry.register(dup)


# ---------- 池输出与数据快照 ----------

def test_sync_pool_outputs_writes_snapshots(repo, monkeypatch):
    from stock_cash_streaming.research import runner as runner_mod
    from stock_cash_streaming.strategy import paper as paper_mod
    from stock_cash_streaming.strategy import tracking as tracking_mod

    calls: dict = {}
    monkeypatch.setattr(tracking_mod, "simulate_tracking",
                        lambda *a, **kw: calls.setdefault("tracking", "running") and {})
    monkeypatch.setattr(paper_mod, "refresh_paper_account",
                        lambda *a, **kw: calls.setdefault("paper", "ok") and {})
    dates = ["2024-01-02", "2024-02-01"]
    weights = pd.DataFrame([[0.6, 0.4], [0.5, 0.5]], index=dates, columns=["600000", "600001"])
    res = {"weights": weights, "rebalance_dates": dates}
    out = runner_mod._sync_pool_outputs(repo, "C1", "value_quality", res)
    assert out["snapshots"] == 2 and out["target_strategy"] == "value_quality_C1"
    runs = repo.query("SELECT * FROM fact_target_run WHERE strategy='value_quality_C1'")
    rows = repo.query("SELECT * FROM fact_target_portfolio WHERE strategy='value_quality_C1'")
    assert len(runs) == 2 and len(rows) == 4
    assert abs(sum(r["weight"] for r in rows if r["trade_date"] == dates[0]) - 1.0) < 1e-6


def test_snapshot_members_monthly_idempotent(repo):
    from stock_cash_streaming.research.universe import snapshot_members

    repo.upsert("dim_universe", [{"universe_id": "C1", "name": "C1"}], ["universe_id"])
    repo.upsert("dim_universe_member",
                [{"universe_id": "C1", "symbol": s, "sector_code": "S1"}
                 for s in ("600000", "600001", "600002")],
                ["universe_id", "symbol"])
    repo.upsert("fact_quote", [
        {"symbol": s, "trade_date": d, "close": 10.0, "pct_chg": 0.0}
        for d in ("2024-05-06", "2024-05-07") for s in ("600000", "600001", "600002")
    ], ["symbol", "trade_date"])
    first = snapshot_members(repo, ["C1"])
    assert first["trade_date"] == "2024-05-06" and first["pools"]["C1"] == 3
    second = snapshot_members(repo, ["C1"])
    assert second["rows"] == 0
    assert len(repo.query("SELECT * FROM dim_universe_member_history")) == 3


def test_sync_earnings_events(repo):
    from stock_cash_streaming.features.fundamental_v5 import sync_earnings_events

    repo.upsert("fact_financial", _fin_rows(), ["symbol", "stat_date", "metric"])
    dates = pd.bdate_range("2022-01-03", "2025-12-31").strftime("%Y-%m-%d")
    repo.upsert("fact_quote", [
        {"symbol": "600000", "trade_date": d, "close": 10.0,
         "pct_chg": 1.0 if i % 3 else -1.0}
        for i, d in enumerate(dates)
    ], ["symbol", "trade_date"])
    out = sync_earnings_events(repo, ["600000"])
    assert out["rows"] >= 8
    rows = repo.query("SELECT * FROM fact_earnings_event WHERE symbol='600000'")
    assert any(r["announce_ret_1d"] is not None for r in rows)
    assert all(r["pub_date"] for r in rows)
