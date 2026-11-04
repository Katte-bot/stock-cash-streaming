"""市场状态分组表现：上行 / 震荡 / 下行。

状态划分（只用当日及之前信息）：
- 基准等权净值过去 20 日涨幅 > +3%  → 上行
- 过去 20 日涨幅 < -3%             → 下行
- 其余                              → 震荡

另提供 `sync_regime_state`：把趋势 / 波动分位 / 市场宽度 / 成交额变化写入
`fact_regime_state`（研究页「因子择时 / 市场状态」使用）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest.engine import DEFAULT_COST_RATE
from ..common.logging import logger
from ..research.universe import C0
from ..storage.repository import Repository


def run_regime(repo: Repository, universe_id: str = C0,
               cost_model: str = "a_share", limit_model: str = "board") -> pd.DataFrame:
    from .experiment import B3_WEIGHTS, _evaluate, _panel, score_from_weights

    symbols, panel = _panel(repo, universe_id)
    score = score_from_weights(panel["piv"], panel["close"], B3_WEIGHTS,
                               panel["sector_map"], neutral=False)
    res = _evaluate(repo, score, panel, symbols, None, None,
                    top_k=12, cost_rate=DEFAULT_COST_RATE, rebalance="monthly",
                    hold_buffer=1.0, weighting="vol_inv", vol_target=0.25,
                    cost_model=cost_model, limit_model=limit_model)
    nav, bench = res["nav"], res["benchmark"]
    ret_s = nav.pct_change().fillna(0.0)
    ret_b = bench.pct_change().fillna(0.0)
    trend = bench.pct_change(20)
    regime = pd.Series("震荡", index=bench.index)
    regime[trend > 0.03] = "上行"
    regime[trend < -0.03] = "下行"
    regime[trend.isna()] = "未定"

    rows: list[dict] = []
    for name in ("上行", "震荡", "下行"):
        mask = regime == name
        n = int(mask.sum())
        if n < 20:
            continue
        s, b = ret_s[mask], ret_b[mask]
        rows.append({
            "regime": name,
            "days": n,
            "share": float(mask.mean()),
            "strategy_ann": float(s.mean() * 252),
            "strategy_vol": float(s.std() * np.sqrt(252)),
            "benchmark_ann": float(b.mean() * 252),
            "excess_ann": float((s.mean() - b.mean()) * 252),
            "win_rate": float((s > 0).mean()),
        })
    return pd.DataFrame(rows)


def sync_regime_state(repo: Repository, index_code: str = "sh000300",
                      days: int = 250) -> dict:
    """计算并落库市场状态（趋势 / 波动分位 / 宽度 / 成交额变化）。

    宽度 = 全市场最新交易日站上 MA20 的股票占比；成交额变化 = 全市场 20 日均额
    相对前 20 日均额的变化。所有指标只用当日及以前数据。
    """
    from ..ingest.index_quote import index_series

    idx = index_series(repo, index_code)
    if idx.empty:
        return {"status": "empty", "rows": 0}
    ma200 = idx.rolling(200, min_periods=60).mean()
    chg20 = idx.pct_change(20)
    vol20 = idx.pct_change().rolling(20, min_periods=10).std() * np.sqrt(252)
    vol_rank = vol20.rolling(250, min_periods=60).rank(pct=True)

    # 市场宽度与成交额：全市场近 40 个交易日的收盘/成交额
    start = str(idx.index[max(0, len(idx) - days - 40)])
    rows = repo.query(
        "SELECT trade_date, symbol, close, amount FROM fact_quote WHERE trade_date >= ?",
        (start,))
    breadth: dict[str, float] = {}
    amount_chg: dict[str, float] = {}
    if rows:
        q = pd.DataFrame([dict(r) for r in rows])
        close = q.pivot(index="trade_date", columns="symbol", values="close").sort_index()
        amount = q.pivot(index="trade_date", columns="symbol", values="amount").sort_index()
        ma20 = close.rolling(20, min_periods=10).mean()
        breadth = (close > ma20).sum(axis=1).div(ma20.notna().sum(axis=1)).to_dict()
        total = amount.sum(axis=1)
        amt20 = total.rolling(20, min_periods=10).mean()
        amount_chg = (amt20 / amt20.shift(20) - 1).to_dict()

    out: list[dict] = []
    for d in list(idx.index)[-days:]:
        up = bool(idx[d] > ma200.get(d, np.nan)) if not pd.isna(ma200.get(d, np.nan)) else False
        c20 = chg20.get(d, np.nan)
        if up and (pd.isna(c20) or c20 > 0):
            trend = "up"
        elif (not up) and (not pd.isna(c20) and c20 < 0):
            trend = "down"
        else:
            trend = "range"
        r = vol_rank.get(d, np.nan)
        vol_state = None if pd.isna(r) else ("low" if r < 0.33 else ("high" if r > 0.66 else "mid"))
        out.append({
            "trade_date": str(d), "trend": trend, "vol_state": vol_state,
            "breadth": None if d not in breadth or pd.isna(breadth[d]) else round(float(breadth[d]), 4),
            "amount_chg": (None if d not in amount_chg or pd.isna(amount_chg[d])
                           else round(float(amount_chg[d]), 4)),
            "style": None,
        })
    if out:
        repo.upsert("fact_regime_state", out, ["trade_date"])
    logger.info(f"市场状态落库：{len(out)} 个交易日（{index_code}）")
    return {"status": "ok", "rows": len(out), "index": index_code,
            "latest": out[-1] if out else None}
