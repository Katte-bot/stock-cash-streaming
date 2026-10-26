"""综合信号：板块内横向对比 → 评分 / 分级 / 理由。

评分口径与回测策略保持一致（防御型，v2 研究结论）：
- 低波动、低换手、低成交额、低彩票特征、弱动量、布林带低位 → 得分高
- 高非流动性（Amihud）→ 得分高
score = 100 × Σ w_i × rank_i，权重见 WEIGHTS（合计 1）。

说明：本模块只做客观打分，不构成投资建议。
"""

import json

import pandas as pd

from ..common.logging import logger
from ..features.engine import FACTOR_VERSION
from ..storage.repository import Repository

WEIGHTS = {
    "vol_20": 0.20,
    "turnover_5": 0.13,
    "turnover_std_20": 0.06,
    "amount_20d": 0.11,
    "max_ret_20": 0.17,
    "ret_20": 0.09,
    "boll_pos": 0.06,
    "amihud_20": 0.18,
}
# 方向：higher 越好 / lower 越好
DIRECTION = {
    "vol_20": "lower",
    "turnover_5": "lower",
    "turnover_std_20": "lower",
    "amount_20d": "lower",
    "max_ret_20": "lower",
    "ret_20": "lower",
    "boll_pos": "lower",
    "amihud_20": "higher",
}

REASON_RULES = [
    ("vol_20", "lower", 0.30, "波动率处于板块低位"),
    ("turnover_5", "lower", 0.30, "换手率处于板块低位"),
    ("amount_20d", "lower", 0.30, "成交额处于板块低位"),
    ("max_ret_20", "lower", 0.30, "无彩票型单日暴涨"),
    ("ret_20", "lower", 0.30, "20日涨幅偏低（未过热）"),
    ("boll_pos", "lower", 0.30, "股价位于布林带低位"),
    ("amihud_20", "higher", 0.70, "非流动性溢价较高"),
]


def _grade(score: float) -> str:
    if score >= 75:
        return "强烈关注"
    if score >= 60:
        return "关注"
    if score >= 40:
        return "中性"
    return "回避"


def _reasons(ranks: pd.Series) -> list[str]:
    rs = []
    for factor, direction, threshold, text in REASON_RULES:
        v = ranks.get(factor)
        if pd.isna(v):
            continue
        if (direction == "lower" and v <= threshold) or (direction == "higher" and v >= threshold):
            rs.append(text)
    return rs


def compute_signals(repo: Repository, trade_date: str | None = None,
                    version: str = FACTOR_VERSION, universe: str = "C0") -> list[dict]:
    """按股票池计算信号（不落库），池内分行业横向打分。"""
    from ..research.universe import universe_members

    if trade_date is None:
        r = repo.query(
            "SELECT MAX(trade_date) AS d FROM fact_factor WHERE factor='main_net_ratio_5d' AND version=?",
            (version,),
        )
        trade_date = r[0]["d"] if r else None
    if not trade_date:
        logger.warning("无资金流因子，无法生成信号")
        return []

    members = universe_members(repo, universe)
    if not members:
        return []
    syms = list(members)
    sp = ",".join("?" for _ in syms)
    rows = repo.query(
        f"SELECT symbol, factor, value FROM fact_factor "
        f"WHERE trade_date=? AND version=? AND symbol IN ({sp})",
        tuple([trade_date, version] + syms))
    if not rows:
        logger.warning(f"{trade_date} 无因子数据")
        return []
    piv = pd.DataFrame([dict(r) for r in rows]).pivot_table(
        index="symbol", columns="factor", values="value"
    )

    names = {r["symbol"]: r["name"] for r in repo.query(
        f"SELECT symbol, IFNULL(name, '') AS name FROM dim_stock WHERE symbol IN ({sp})", tuple(syms))}
    mdf = pd.DataFrame([{"symbol": s, "sector_code": sec, "name": names.get(s, "")}
                        for s, sec in members.items()]).set_index("symbol")
    df = piv.join(mdf, how="inner")
    if df.empty:
        return []

    signals: list[dict] = []
    for sector, g in df.groupby("sector_code"):
        g = g.copy()
        score = pd.Series(0.0, index=g.index)
        ranks = pd.DataFrame(index=g.index)
        for factor, w in WEIGHTS.items():
            if factor not in g.columns:
                continue
            rank = g[factor].rank(pct=True)
            ranks[factor] = rank
            if DIRECTION[factor] == "lower":
                rank = 1 - rank
            score = score + w * rank.fillna(0.5)
        g["score"] = (score * 100).round(2)

        for sym, row in g.iterrows():
            s = float(row["score"])
            signals.append({
                "symbol": sym,
                "sector": sector,
                "name": row.get("name", ""),
                "trade_date": trade_date,
                "score": s,
                "grade": _grade(s),
                "reasons": _reasons(ranks.loc[sym]),
            })

    signals.sort(key=lambda x: (x["sector"], -x["score"]))
    return signals


def generate_signals(repo: Repository, trade_date: str | None = None,
                     version: str = FACTOR_VERSION) -> list[dict]:
    """生成 C0 板块信号并写入 fact_signal(phase='eod')，返回信号列表。"""
    signals = compute_signals(repo, trade_date=trade_date, version=version, universe="C0")
    if not signals:
        return []
    repo.upsert(
        "fact_signal",
        [{
            "symbol": x["symbol"], "trade_date": x["trade_date"], "phase": "eod",
            "score": x["score"], "grade": x["grade"], "reasons": json.dumps(x["reasons"], ensure_ascii=False),
        } for x in signals],
        ["symbol", "trade_date", "phase"],
    )
    return signals
