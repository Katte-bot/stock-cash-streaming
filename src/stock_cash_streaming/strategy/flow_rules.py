"""资金流横向对比策略（me.md 规则）+ 趋势择时。

入场条件（板块内）：
1. 连续净流入天数 >= 3
2. 主力净流入占比(5日均) > 板块中位数
3. 超大单净流入占比(5日均) > 0
4. 5日涨幅 < 板块中位数（股价未反应）
5. 股价位于布林带中轨下方（boll_pos < 0.5）
风控：
6. 非 ST / 退市风险
7. 20日均成交额 > 5000 万
8. 非涨跌停
择时：
9. 股价在 60 日均线之上（趋势过滤，规避系统性下跌）
"""

import pandas as pd


def _sector_median(f: pd.DataFrame, sector_map: dict, columns) -> pd.DataFrame:
    """按板块计算截面中位数（每个交易日、每个板块）。"""
    sec = pd.Series({s: sector_map.get(s, "NA") for s in columns})
    return f.T.groupby(sec).transform("median").T


def build_flow_rules_score(piv: dict[str, pd.DataFrame], close: pd.DataFrame,
                           pct: pd.DataFrame, sector_map: dict,
                           st_symbols: set[str], min_amount: float = 5e7,
                           trend_filter: bool = True) -> pd.DataFrame:
    idx, cols = close.index, close.columns
    valid = pd.DataFrame(True, index=idx, columns=cols)

    mnr = piv.get("main_net_ratio_5d")
    xlr = piv.get("xl_ratio_5d")
    inf = piv.get("inflow_days_5")
    ret5 = piv.get("ret_5")
    boll = piv.get("boll_pos")
    inst = piv.get("institution_trend")
    amt = piv.get("amount_20d")
    ma60 = piv.get("ma60_ratio")

    if inf is not None:
        valid &= (inf >= 3)
    if xlr is not None:
        valid &= (xlr > 0)
    if boll is not None:
        valid &= (boll < 0.5)
    if amt is not None:
        valid &= (amt > min_amount)
    if ret5 is not None:
        valid &= (ret5 < _sector_median(ret5.reindex(columns=cols), sector_map, cols))
    if mnr is not None:
        valid &= (mnr > _sector_median(mnr.reindex(columns=cols), sector_map, cols))
    if trend_filter and ma60 is not None:
        valid &= (ma60 > 0)
    valid &= (pct.abs() < 9.8)  # 非涨跌停
    for s in st_symbols:
        if s in valid.columns:
            valid[s] = False

    def R(x):
        return x.rank(axis=1, pct=True)

    score = pd.DataFrame(0.0, index=idx, columns=cols)
    wsum = 0.0
    for w, f in ((0.5, mnr), (0.3, inst), (0.2, inf)):
        if f is not None:
            score = score + w * R(f.reindex(index=idx, columns=cols)).fillna(0)
            wsum += w
    if wsum:
        score = score / wsum
    return score.where(valid)
