"""目标组合：把防御型评分转换为可执行的目标权重与调仓清单。

与 `backtest/engine.py` 的口径严格一致：
- 评分：DEFENSIVE_WEIGHTS（8 因子，amihud 为正方向）
- 过滤：20 日成交额 > 1 亿、上市 > 60 日、剔除 ST/退
- 选股：top_k + 持仓缓冲 1 名
- 加权：1/vol_20
- 风控：篮子近 20 日年化波动 > 25% 时按 25%/实际 缩放（下限 30%）
- 调仓：每 20 个交易日（与回测的 i % 20 == 0 对齐）
- 执行：信号日次一交易日收盘

说明：本模块只输出客观组合与调仓清单，不构成投资建议。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from ..backtest.engine import (
    DEFENSIVE_VALUATION_WEIGHTS, DEFENSIVE_WEIGHTS, FUNDAMENTAL_FACTOR_NAMES,
    defensive_enhanced_score, defensive_score,
)
from ..common.logging import logger
from ..features.engine import FACTOR_VERSION
from ..research.universe import C0, eligibility_panel, universe_members
from ..storage.repository import Repository

STRATEGY_NAME = "defensive_top12_monthly"
STRATEGY_NAMES = {
    "defensive": STRATEGY_NAME,
    "defensive_valuation": "defensive_valuation_top12_monthly",
}
DEFAULT_TOP_K = 12
DEFAULT_MIN_AMOUNT = 1e8
DEFAULT_VOL_TARGET = 0.25
REBALANCE_PERIOD = 20
LOOKBACK_DAYS = 60


def _universe(repo: Repository, universe_id: str = C0) -> pd.DataFrame:
    """股票池成员 + 名称（C0 为板块成分，C1 为用户 CSV 池）。"""
    members = universe_members(repo, universe_id)
    if not members:
        return pd.DataFrame(columns=["name", "sector_code"])
    ph = ",".join("?" for _ in members)
    names = {r["symbol"]: r["name"] for r in repo.query(
        f"SELECT symbol, name FROM dim_stock WHERE symbol IN ({ph})", tuple(members))}
    return pd.DataFrame([{
        "symbol": s, "name": names.get(s, ""), "sector_code": sec,
    } for s, sec in members.items()]).set_index("symbol")


def _factor_matrix(repo: Repository, trade_date: str, version: str,
                   with_fundamentals: bool = False) -> dict[str, pd.DataFrame]:
    """取指定交易日的因子截面；技术/资金流用 v2，基本面用 v3。"""
    names = sorted(set(DEFENSIVE_WEIGHTS) | {"ma60_ratio"})
    placeholders = ",".join("?" for _ in names)
    rows = repo.query(
        f"SELECT symbol, factor, value FROM fact_factor "
        f"WHERE trade_date=? AND version=? AND factor IN ({placeholders})",
        tuple([trade_date, version] + names),
    )
    out: dict[str, pd.DataFrame] = {}
    if rows:
        df = pd.DataFrame([dict(r) for r in rows])
        for f in names:
            sub = df[df["factor"] == f]
            if not sub.empty:
                out[f] = pd.DataFrame({r.symbol: [r.value] for r in sub.itertuples()},
                                      index=[trade_date])
    if with_fundamentals:
        fnames = sorted(FUNDAMENTAL_FACTOR_NAMES)
        fph = ",".join("?" for _ in fnames)
        frows = repo.query(
            f"SELECT symbol, factor, value FROM fact_factor "
            f"WHERE trade_date=? AND version='v3' AND factor IN ({fph})",
            tuple([trade_date] + fnames),
        )
        if frows:
            fdf = pd.DataFrame([dict(r) for r in frows])
            for f in fnames:
                sub = fdf[fdf["factor"] == f]
                if not sub.empty:
                    out[f] = pd.DataFrame({r.symbol: [r.value] for r in sub.itertuples()},
                                          index=[trade_date])
    return out


def _rebalance_position(repo: Repository, trade_date: str) -> tuple[bool, int]:
    """返回 (是否调仓日, 距下次调仓的交易日数)，与回测 i % 20 对齐。"""
    dates = [r["d"] for r in repo.query(
        "SELECT DISTINCT trade_date AS d FROM fact_quote WHERE trade_date<=? ORDER BY trade_date",
        (trade_date,))]
    if not dates:
        return False, REBALANCE_PERIOD
    i = dates.index(trade_date) if trade_date in dates else len(dates) - 1
    mod = i % REBALANCE_PERIOD
    return mod == 0, REBALANCE_PERIOD - mod


def _previous_rebalance(repo: Repository, trade_date: str,
                        strategy: str = STRATEGY_NAME) -> tuple[str | None, dict[str, float]]:
    r = repo.query(
        """SELECT trade_date FROM fact_target_run
           WHERE strategy=? AND is_rebalance=1 AND trade_date<? 
           ORDER BY trade_date DESC LIMIT 1""",
        (strategy, trade_date),
    )
    if not r:
        return None, {}
    prev_date = r[0]["trade_date"]
    rows = repo.query(
        "SELECT symbol, weight FROM fact_target_portfolio WHERE strategy=? AND trade_date=?",
        (strategy, prev_date),
    )
    return prev_date, {x["symbol"]: float(x["weight"]) for x in rows}


def build_target_portfolio(
    repo: Repository,
    trade_date: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    min_amount: float = DEFAULT_MIN_AMOUNT,
    vol_target: float = DEFAULT_VOL_TARGET,
    version: str = FACTOR_VERSION,
    persist: bool = False,
    universe_id: str = C0,
    exclude_untradable: bool = True,
    strategy: str = "defensive",
) -> dict:
    """计算目标组合、权重与相对上次调仓的变化。

    strategy：`defensive`（生产口径）或 `defensive_valuation`（候选，含 8% 估值）。
    候选策略使用独立 strategy 名落库，不覆盖生产组合。
    """
    kind = strategy if strategy in STRATEGY_NAMES else "defensive"
    base_name = STRATEGY_NAMES[kind]
    strategy_name = base_name if universe_id == C0 else f"{base_name}_{universe_id}"
    if trade_date is None:
        r = repo.query(
            "SELECT MAX(trade_date) AS d FROM fact_factor WHERE version=? AND factor='vol_20'",
            (version,),
        )
        trade_date = r[0]["d"] if r else None
    if not trade_date:
        return {"error": "无因子数据"}

    uni = _universe(repo, universe_id)
    if uni.empty:
        return {"error": f"股票池 {universe_id} 为空", "trade_date": trade_date}
    piv = _factor_matrix(repo, trade_date, version,
                         with_fundamentals=(kind == "defensive_valuation"))
    if not piv:
        return {"error": f"{trade_date} 无因子数据", "trade_date": trade_date}

    # 用全universe构造 1 行 close 矩阵，保证截面排名与回测一致
    close = pd.DataFrame(0.0, index=[trade_date], columns=list(uni.index))
    st_symbols = {s for s, row in uni.iterrows()
                  if "ST" in (row["name"] or "") or "退" in (row["name"] or "")}
    if kind == "defensive_valuation":
        score = defensive_enhanced_score(piv, close, min_amount=min_amount,
                                         st_symbols=st_symbols,
                                         weights=DEFENSIVE_VALUATION_WEIGHTS).iloc[-1]
    else:
        score = defensive_score(piv, close, min_amount=min_amount,
                                st_symbols=st_symbols).iloc[-1]
    if exclude_untradable:
        mask = eligibility_panel(repo, list(uni.index))
        if not mask.empty:
            today = mask.loc[mask.index <= trade_date].iloc[-1] if (mask.index <= trade_date).any() else None
            if today is not None:
                score = score.where(today.reindex(score.index).fillna(False))

    is_reb, days_to_next = _rebalance_position(repo, trade_date)
    prev_date, prev_w = _previous_rebalance(repo, trade_date, strategy_name)
    held = list(prev_w.keys())

    # 选股：top_k + 持仓缓冲 1 名（与 _target_weights 一致）
    s = score.dropna()
    rank = s.rank(ascending=False, method="first")
    for h in held:
        if h in rank.index:
            rank[h] -= 1
    top = list(rank.nsmallest(min(top_k, len(rank))).index)

    # 1/vol 加权（vol_20 入库单位为百分数，这里换算为小数）
    vol = piv.get("vol_20")
    vol_s = (vol.iloc[-1].reindex(top) / 100.0 if vol is not None
             else pd.Series(np.nan, index=top))
    inv = (1.0 / vol_s.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    if inv.sum() > 0:
        raw_w = (inv / inv.sum()).fillna(0)
    else:
        raw_w = pd.Series(1.0 / len(top), index=top)

    # 篮子近 20 日波动 → 仓位缩放
    hist = pd.DataFrame([dict(r) for r in repo.query(
        f"SELECT symbol, trade_date, close FROM fact_quote "
        f"WHERE trade_date<=? AND symbol IN ({','.join('?' for _ in top)}) "
        f"ORDER BY trade_date",
        tuple([trade_date] + top))])
    basket_vol, scale = np.nan, 1.0
    if not hist.empty and len(top) > 0:
        hp = hist.pivot(index="trade_date", columns="symbol", values="close").sort_index()
        hp = hp.tail(LOOKBACK_DAYS + 1)
        rets = hp.pct_change().dropna(how="all").tail(20)
        if len(rets) >= 10:
            basket = (rets.reindex(columns=top).fillna(0) * raw_w).sum(axis=1)
            basket_vol = float(basket.std() * np.sqrt(252))
            if basket_vol > vol_target:
                scale = max(0.3, vol_target / basket_vol)
    weight = (raw_w * scale).round(6)

    positions = []
    for sym, w in weight.items():
        row = uni.loc[sym] if sym in uni.index else None
        note = ""
        if sym.startswith(("900", "200")):
            note = "B股，需确认账户可交易"
        positions.append({
            "symbol": sym,
            "name": row["name"] if row is not None else "",
            "sector": row["sector_code"] if row is not None else "",
            "score": round(float(score.get(sym, np.nan)) * 100, 2),
            "vol_20": None if pd.isna(vol_s.get(sym)) else round(float(vol_s.get(sym)), 4),
            "raw_weight": round(float(raw_w.get(sym, 0)), 6),
            "weight": float(w),
            "note": note,
        })
    positions.sort(key=lambda x: -x["weight"])

    # 相对上次调仓的变化
    cur = {p["symbol"]: p["weight"] for p in positions}

    def _named(sym: str) -> dict:
        return {"symbol": sym,
                "name": uni.loc[sym, "name"] if sym in uni.index else ""}

    added = [_named(s) for s in cur if s not in prev_w]
    removed = [_named(s) for s in prev_w if s not in cur]
    delta = []
    for s in cur:
        if s in prev_w:
            d = cur[s] - prev_w[s]
            if abs(d) >= 0.005:
                delta.append({**_named(s), "from": prev_w[s], "to": cur[s], "delta": round(d, 4)})

    action = "rebalance" if is_reb else "hold"
    if prev_date is None:
        action = "initial"

    result = {
        "trade_date": trade_date,
        "strategy": strategy_name,
        "strategy_kind": kind,
        "execute_at": "次一交易日收盘",
        "action": action,
        "is_rebalance_day": is_reb,
        "days_to_next_rebalance": days_to_next,
        "previous_rebalance": prev_date,
        "basket_vol_20d": None if pd.isna(basket_vol) else round(basket_vol, 4),
        "vol_target": vol_target,
        "vol_scale": round(scale, 4),
        "exposure": round(float(weight.sum()), 4),
        "positions": positions,
        "changes": {
            "type": "initial" if prev_date is None else "rebalance",
            "added": added,
            "removed": removed,
            "weight_changes": delta,
        },
        "names": {p["symbol"]: p["name"] for p in positions},
    }

    if persist:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 首次运行作为对比基线落库（标记为 is_rebalance=1），后续调仓以它为基准
        flag = int(is_reb or prev_date is None)
        repo.upsert("fact_target_run", [{
            "trade_date": trade_date, "strategy": strategy_name,
            "is_rebalance": flag, "basket_vol": None if pd.isna(basket_vol) else float(basket_vol),
            "vol_scale": float(scale), "exposure": float(weight.sum()), "created_at": now,
        }], ["trade_date", "strategy"])
        repo.upsert("fact_target_portfolio", [{
            "trade_date": trade_date, "strategy": strategy_name, "symbol": p["symbol"],
            "name": p["name"], "sector_code": p["sector"], "score": p["score"],
            "vol_20": p["vol_20"], "raw_weight": p["raw_weight"], "weight": p["weight"],
            "is_rebalance": flag, "note": p["note"], "created_at": now,
        } for p in positions], ["trade_date", "strategy", "symbol"])
        logger.info(f"目标组合已落库 {trade_date}：{len(positions)} 只，总仓位 {weight.sum():.2%}")

    return result

