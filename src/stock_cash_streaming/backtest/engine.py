"""策略回测引擎（日频再平衡）。

规则：
- 信号在 t 日收盘计算，t+1 日收盘执行（1 日滞后，避免未来函数）。
- 持仓从执行日收盘持有到次日收盘，天然满足 T+1。
- 涨跌停约束：涨停不可买、跌停不可卖（保留原仓位）。
- 成本：按换手率收取（佣金+印花税+过户费+滑点，默认单边合计约 0.13%）。

复合评分（技术面，越低估值/越超跌越优）：
score = 0.35·(1-rank(ret_5)) + 0.30·(1-rank(boll_pos))
      + 0.20·rank(macd_hist) + 0.15·(1-rank(rsi14))
"""

from datetime import datetime
import json

import numpy as np
import pandas as pd

from ..common.logging import logger
from ..features.engine import FACTOR_VERSION
from ..features.fundamentals import FUNDAMENTAL_VERSION
from ..storage.repository import Repository
from .metrics import compute_metrics

DEFAULT_COST_RATE = 0.0013  # 单边换手成本率（约 0.13%）
LIMIT_PCT = 9.8  # 主板涨跌停近似阈值（%）


def _limit_threshold(symbol: str, trade_date: str, model: str = "flat") -> float:
    """涨跌停阈值（%）。flat=统一 9.8；board=按板块与历史制度。"""
    if model == "flat":
        return LIMIT_PCT
    if symbol.startswith("688"):
        return 19.8
    if symbol.startswith("30"):
        return 19.8 if trade_date >= "2020-08-24" else 9.8
    if symbol.startswith(("4", "8", "92")):
        return 29.8
    return 9.8


def _a_share_rates(trade_date: str, cost_rate: float) -> tuple[float, float]:
    """A 股单边费率（含滑点）：(买入, 卖出)。

    佣金 0.025%（最低 5 元未计，组合规模下可忽略）、过户费 0.001%、
    印花税卖出 0.1%（2023-08-28 起 0.05%）、滑点按 cost_rate 的一半计。
    """
    slippage = cost_rate / 2
    commission, transfer = 0.00025, 0.00001
    stamp = 0.001 if trade_date < "2023-08-28" else 0.0005
    return commission + transfer + slippage, commission + transfer + stamp + slippage



def _load_quotes(repo: Repository) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows = repo.query("SELECT symbol, trade_date, close, pct_chg FROM fact_quote ORDER BY symbol, trade_date")
    df = pd.DataFrame([dict(r) for r in rows])
    close = df.pivot(index="trade_date", columns="symbol", values="close").sort_index()
    pct = df.pivot(index="trade_date", columns="symbol", values="pct_chg").sort_index()
    ret = close.pct_change().replace([np.inf, -np.inf], np.nan)
    return close, ret, pct


def _pivot_factors(repo: Repository, names: list[str], version: str = "v1") -> dict[str, pd.DataFrame]:
    placeholders = ",".join("?" for _ in names)
    rows = repo.query(
        f"SELECT trade_date, symbol, factor, value FROM fact_factor "
        f"WHERE version=? AND factor IN ({placeholders})",
        tuple([version] + names),
    )
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return {}
    out = {}
    for f in names:
        sub = df[df["factor"] == f]
        if not sub.empty:
            out[f] = sub.pivot(index="trade_date", columns="symbol", values="value")
    return out


def prepare_panel(repo: Repository, factor_names: list[str], version: str = "v1",
                  universe: list[str] | None = None,
                  eligibility: pd.DataFrame | None = None,
                  include_fundamentals: bool = False,
                  universe_sector_map: dict[str, str] | None = None) -> dict:
    """一次性加载行情+因子+板块/ST 标记，供批量回测复用（避免重复查库）。

    universe   : 限定股票池成员（None 表示 fact_quote 全部标的）
    eligibility: dates × symbols 的 bool 掩码，False 表示当日不可选（ST/停牌/退市）
    """
    close, ret, pct = _load_quotes(repo)
    if universe:
        keep = [c for c in close.columns if c in set(universe)]
        close, ret, pct = close[keep], ret[keep], pct[keep]
    piv = _pivot_factors(repo, factor_names, version)
    if include_fundamentals:
        piv.update(_pivot_factors(repo, FUNDAMENTAL_FACTOR_NAMES, FUNDAMENTAL_VERSION))
    if universe and piv:
        piv = {k: v.reindex(columns=[c for c in v.columns if c in set(universe)]) for k, v in piv.items()}
    sector_map: dict[str, str] = {}
    for r in repo.query("SELECT symbol, sector_code FROM dim_sector_member"):
        sector_map.setdefault(r["symbol"], r["sector_code"])
    if universe_sector_map:
        sector_map.update(universe_sector_map)
    st_symbols = {
        r["symbol"] for r in repo.query("SELECT symbol, name FROM dim_stock")
        if "ST" in (r["name"] or "") or "退" in (r["name"] or "")
    }
    if eligibility is not None:
        eligibility = eligibility.reindex(index=close.index, columns=close.columns).fillna(False).astype(bool)
    return {"close": close, "ret": ret, "pct": pct, "piv": piv,
            "sector_map": sector_map, "st_symbols": st_symbols, "eligibility": eligibility}


def composite_score(piv: dict[str, pd.DataFrame], index, columns) -> pd.DataFrame:
    """复合评分（截面百分位，0-1）：技术面 + 资金流。

    缺失的因子自动跳过，权重按可用因子归一化。
    """
    def rank(x):
        return x.rank(axis=1, pct=True)

    parts: list[tuple[float, pd.DataFrame]] = []
    if "ret_5" in piv:
        parts.append((0.28, 1 - rank(piv["ret_5"])))
    if "boll_pos" in piv:
        parts.append((0.20, 1 - rank(piv["boll_pos"])))
    if "macd_hist" in piv:
        parts.append((0.12, rank(piv["macd_hist"])))
    if "rsi14" in piv:
        parts.append((0.10, 1 - rank(piv["rsi14"])))
    if "main_net_ratio_5d" in piv:
        parts.append((0.15, rank(piv["main_net_ratio_5d"])))
    if "inflow_days_5" in piv:
        parts.append((0.10, rank(piv["inflow_days_5"])))
    if "institution_trend" in piv:
        parts.append((0.05, rank(piv["institution_trend"])))

    if not parts:
        return pd.DataFrame(index=index, columns=columns, dtype=float)
    total_w = sum(w for w, _ in parts)
    score = sum(w * s for w, s in parts) / total_w
    return score.reindex(index=index, columns=columns)


def _cap_single(w: pd.Series, cap: float) -> pd.Series:
    """单股权重上限：超限部分按现有权重比例分配给未触顶标的。"""
    w = w.copy()
    for _ in range(20):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        free = (~over) & (w > 0)
        if not free.any() or w[free].sum() <= 0:
            break
        w[free] += excess * w[free] / w[free].sum()
    return w


def _cap_sector(w: pd.Series, sector_map: dict[str, str], cap: float,
                max_weight: float | None = None) -> pd.Series:
    """行业权重上限：超限行业按比例缩减，超出的权重分配给其他行业。"""
    w = w.copy()
    for _ in range(20):
        groups: dict[str, list[str]] = {}
        for s, x in w.items():
            if x > 0:
                groups.setdefault(sector_map.get(s, ""), []).append(s)
        over = {k: v for k, v in groups.items() if float(w[v].sum()) > cap + 1e-12}
        if not over:
            break
        cut: list[str] = []
        excess = 0.0
        for syms in over.values():
            total = float(w[syms].sum())
            excess += total - cap
            w[syms] *= cap / total
            cut.extend(syms)
        free = [s for s in w.index if w[s] > 0 and s not in cut]
        if not free or w[free].sum() <= 0:
            break
        w[free] += excess * w[free] / w[free].sum()
        if max_weight:
            w = _cap_single(w, max_weight)
    return w


def _target_weights(row: pd.Series, top_k: int, held: list[str] | None = None,
                    hold_buffer: float = 0.0, weighting: str = "equal",
                    vol: pd.Series | None = None,
                    sector_map: dict[str, str] | None = None,
                    max_weight: float = 1.0,
                    max_sector: float = 1.0) -> pd.Series:
    """在截面评分上选 top_k。held 内的持仓获得 hold_buffer 的排名加成（降低换手）。

    weighting:
      - equal   : 等权
      - score   : 按截面分数线性加权（最低分归零后归一）
      - rank    : 按名次线性加权（第 1 名权重最大）
      - vol_inv : 按 1/波动率 加权（需提供 vol 截面）
    """
    w = pd.Series(0.0, index=row.index)
    s = row.dropna()
    if s.empty:
        return w
    rank = s.rank(ascending=False, method="first")
    if held and hold_buffer > 0:
        for h in held:
            if h in rank.index:
                rank[h] -= hold_buffer
    top = rank.nsmallest(min(top_k, len(rank))).index
    if weighting == "score":
        v = s[top] - s[top].min()
        if v.sum() <= 0:
            w[top] = 1.0 / len(top)
        else:
            w[top] = (v + v[v > 0].min() * 0.1) / (v + v[v > 0].min() * 0.1).sum()
    elif weighting == "rank":
        r = rank[top]
        wt = (len(top) + 1 - r)
        w[top] = wt / wt.sum()
    elif weighting == "vol_inv" and vol is not None:
        v = vol.reindex(top).replace(0, np.nan)
        inv = (1.0 / v).replace([np.inf, -np.inf], np.nan).fillna(0)
        if inv.sum() > 0:
            w[top] = inv / inv.sum()
        else:
            w[top] = 1.0 / len(top)
    else:
        w[top] = 1.0 / len(top)
    if max_weight < 1.0:
        w = _cap_single(w, max_weight)
    if max_sector < 1.0 and sector_map:
        w = _cap_sector(w, sector_map, max_sector, max_weight if max_weight < 1.0 else None)
    return w


def inverted_score(piv: dict[str, pd.DataFrame], close: pd.DataFrame, min_amount: float = 1e8,
                   st_symbols: set[str] | None = None) -> pd.DataFrame:
    """反向因子组合：做多「低波动 / 低换手 / 低流动性 / 弱动量 / 低资金流」的一篮子。

    因子诊断显示这些因子在本样本中 IC 稳定为负，故取 1-rank 作为评分。
    """
    idx, cols = close.index, close.columns

    def R(x):
        return x.rank(axis=1, pct=True)

    parts = []
    for f in ("vol_20", "turnover_5", "amount_20d", "ret_20", "boll_pos"):
        if f in piv:
            parts.append(1 - R(piv[f].reindex(index=idx, columns=cols)))
    if not parts:
        return pd.DataFrame(index=idx, columns=cols, dtype=float)

    score = sum(parts) / len(parts)
    valid = pd.DataFrame(True, index=idx, columns=cols)
    if "amount_20d" in piv:
        valid &= (piv["amount_20d"].reindex(index=idx, columns=cols) > min_amount)
    if "ma60_ratio" in piv:  # 上市不足 ~60 日的不参与
        valid &= piv["ma60_ratio"].reindex(index=idx, columns=cols).notna()
    if st_symbols:
        for s in st_symbols:
            if s in valid.columns:
                valid[s] = False
    return score.where(valid)


DEFENSIVE_WEIGHTS = {
    "vol_20": 0.20, "turnover_5": 0.13, "turnover_std_20": 0.06,
    "amount_20d": 0.11, "max_ret_20": 0.17, "ret_20": 0.09,
    "boll_pos": 0.06, "amihud_20": 0.18,
}
# 因子方向：amihud_20（非流动性）IC 为正，其余 IC 为负
DEFENSIVE_POSITIVE = {"amihud_20"}

# 防御增强版（方案 §5.1 的起始权重，待样本外验证）
DEFENSIVE_ENHANCED_WEIGHTS = {
    "vol_20": 0.20, "max_ret_20": 0.14, "amihud_20": 0.10, "turnover_5": 0.08,
    "quality": 0.15, "cash_quality": 0.10, "valuation": 0.08, "dividend": 0.06,
    "ret_20": 0.04, "boll_pos": 0.03, "turnover_std_20": 0.02,
}

FUNDAMENTAL_FACTOR_NAMES = [
    "roe_ttm", "roe_std_8q", "cfo_to_np", "cfo_to_np_std_8q", "cfo_to_rev",
    "ep_ttm", "bp_mrq", "fcf_yield", "div_yield_3y", "div_continuity_3y",
    "div_payout", "leverage",
]

# 候选方案（第二轮结论）：原策略 + 8% 估值，其余权重按比例缩放
DEFENSIVE_VALUATION_WEIGHTS = {k: v * 0.92 for k, v in DEFENSIVE_WEIGHTS.items()}
DEFENSIVE_VALUATION_WEIGHTS["valuation"] = 0.08


def defensive_score(piv: dict[str, pd.DataFrame], close: pd.DataFrame, min_amount: float = 1e8,
                    st_symbols: set[str] | None = None) -> pd.DataFrame:
    """防御型复合评分（v2 研究结论）。

    「低波动 + 低换手 + 低流动性 + 低彩票特征 + 弱动量 + 高非流动性溢价」。
    样本内(2018-2022)/样本外(2023-2026) 均验证有效。
    """
    idx, cols = close.index, close.columns

    def R(x):
        return x.rank(axis=1, pct=True)

    total, score = 0.0, pd.DataFrame(0.0, index=idx, columns=cols)
    for f, w in DEFENSIVE_WEIGHTS.items():
        mat = piv.get(f)
        if mat is None:
            continue
        r = R(mat.reindex(index=idx, columns=cols))
        score += w * (r if f in DEFENSIVE_POSITIVE else (1 - r))
        total += w
    if total == 0:
        return pd.DataFrame(index=idx, columns=cols, dtype=float)
    score /= total

    valid = pd.DataFrame(True, index=idx, columns=cols)
    if "amount_20d" in piv:
        valid &= (piv["amount_20d"].reindex(index=idx, columns=cols) > min_amount)
    if "ma60_ratio" in piv:  # 上市不足 ~60 日的不参与
        valid &= piv["ma60_ratio"].reindex(index=idx, columns=cols).notna()
    if st_symbols:
        for s in st_symbols:
            if s in valid.columns:
                valid[s] = False
    return score.where(valid)


def _component_score(piv: dict[str, pd.DataFrame], idx, cols,
                     spec: list[tuple[str, float, int]]) -> pd.DataFrame | None:
    """按 (因子, 权重, 方向) 组合成子分数；缺失因子跳过，权重重新归一。"""
    parts: list[tuple[float, pd.DataFrame]] = []
    for name, weight, direction in spec:
        mat = piv.get(name)
        if mat is None:
            continue
        r = mat.reindex(index=idx, columns=cols).rank(axis=1, pct=True)
        parts.append((weight, r if direction > 0 else 1 - r))
    if not parts:
        return None
    total = sum(w for w, _ in parts)
    out = pd.DataFrame(0.0, index=idx, columns=cols)
    for w, m in parts:
        out = out + w * m.fillna(0.5)
    return out / total


def defensive_enhanced_score(piv: dict[str, pd.DataFrame], close: pd.DataFrame,
                             min_amount: float = 1e8,
                             st_symbols: set[str] | None = None,
                             weights: dict[str, float] | None = None) -> pd.DataFrame:
    """防御增强版评分（财务质量 / 现金流质量 / 估值 / 股息可持续性）。

    权重见 DEFENSIVE_ENHANCED_WEIGHTS；基本面子分数由 _component_score 组合。
    所有财务数据均按公告日生效（见 features/fundamentals.py）。
    """
    idx, cols = close.index, close.columns

    components = {
        "quality": _component_score(piv, idx, cols, [
            ("roe_ttm", 0.4, 1), ("roe_std_8q", 0.3, -1), ("cfo_to_np", 0.3, 1),
        ]),
        "cash_quality": _component_score(piv, idx, cols, [
            ("cfo_to_np", 0.5, 1), ("cfo_to_np_std_8q", 0.3, -1), ("cfo_to_rev", 0.2, 1),
        ]),
        "valuation": _component_score(piv, idx, cols, [
            ("ep_ttm", 0.45, 1), ("bp_mrq", 0.35, 1), ("fcf_yield", 0.2, 1),
        ]),
        "dividend": _component_score(piv, idx, cols, [
            ("div_continuity_3y", 0.4, 1), ("div_yield_3y", 0.3, 1),
            ("div_payout", 0.2, 1), ("leverage", 0.1, -1),
        ]),
    }

    total, score = 0.0, pd.DataFrame(0.0, index=idx, columns=cols)
    for f, w in (weights or DEFENSIVE_ENHANCED_WEIGHTS).items():
        if f in components:
            sub = components[f]
            if sub is None:
                continue
        else:
            mat = piv.get(f)
            if mat is None:
                continue
            r = mat.reindex(index=idx, columns=cols).rank(axis=1, pct=True)
            sub = r if f in DEFENSIVE_POSITIVE else 1 - r
        score += w * sub.fillna(0.5)
        total += w
    if total == 0:
        return pd.DataFrame(index=idx, columns=cols, dtype=float)
    score /= total

    valid = pd.DataFrame(True, index=idx, columns=cols)
    if "amount_20d" in piv:
        valid &= (piv["amount_20d"].reindex(index=idx, columns=cols) > min_amount)
    if "ma60_ratio" in piv:
        valid &= piv["ma60_ratio"].reindex(index=idx, columns=cols).notna()
    if st_symbols:
        for s in st_symbols:
            if s in valid.columns:
                valid[s] = False
    return score.where(valid)


def run_backtest(
    repo: Repository,
    top_k: int = 2,
    cost_rate: float = DEFAULT_COST_RATE,
    start_date: str | None = None,
    end_date: str | None = None,
    version: str = FACTOR_VERSION,
    initial_capital: float = 1_000_000,
    rebalance: str = "daily",
    hold_buffer: float = 0.0,
    strategy: str = "composite",
    market_filter: bool = False,
    persist: bool = True,
    score_override: pd.DataFrame | None = None,
    weighting: str = "equal",
    basket_trend: int = 0,
    basket_trend_frac: float = 0.0,
    vol_target: float = 0.0,
    panel: dict | None = None,
    universe: list[str] | None = None,
    eligibility: pd.DataFrame | None = None,
    max_weight: float = 1.0,
    max_sector: float = 1.0,
    cost_model: str = "flat",
    limit_model: str = "flat",
    universe_sector_map: dict[str, str] | None = None,
    return_weights: bool = False,
    weight_smoothing: float = 0.0,
    dynamic_top_k: bool = False,
    dynamic_vol_target: bool = False,
    universe_tag: str | None = None,
) -> dict:
    """运行回测，落库并返回结果。

    score_override: 直接注入外部评分矩阵（研究用），优先级最高。
    weighting    : equal / score / rank / vol_inv。
    basket_trend : >0 时，仅当持仓篮子等权净值在其 N 日均线之上才持仓。
    basket_trend_frac: 篮子跌破均线时保留的仓位比例（0=空仓，0.5=半仓）。
    vol_target   : >0 时按篮子近 20 日年化波动缩放仓位（下限 30%）。
    panel        : 预加载面板（prepare_panel），批量回测时避免重复查库。
    return_weights: 为 True 时额外返回每日持仓权重与收益贡献（归因用）。
    weight_smoothing: 0-1，目标权重向昨日持仓平滑（降低换手，保持总仓位不变）。
    dynamic_top_k : 市场 20 日波动高于过去 250 日中位数 1.2 倍时，TopK 放大 1.5 倍。
    dynamic_vol_target: 高波动区间把波动目标下调 20%。
    universe_tag : 落库策略名追加池后缀（如 defensive_top12_monthly_C2）。
    """
    if panel is not None:
        close, ret, pct = panel["close"], panel["ret"], panel["pct"]
        piv, sector_map, st_symbols = panel["piv"], panel["sector_map"], panel["st_symbols"]
    else:
        close, ret, pct = _load_quotes(repo)
        if close.empty:
            raise RuntimeError("无行情数据")

        if strategy == "flow_rules":
            factor_names = ["ret_5", "boll_pos", "macd_hist", "rsi14", "main_net_ratio_5d",
                            "inflow_days_5", "institution_trend", "xl_ratio_5d", "amount_20d", "ma60_ratio"]
        elif strategy == "inverted":
            factor_names = ["vol_20", "turnover_5", "amount_20d", "ret_20", "boll_pos", "ma60_ratio"]
        elif strategy == "defensive":
            factor_names = ["vol_20", "turnover_5", "turnover_std_20", "amount_20d",
                            "max_ret_20", "ret_20", "boll_pos", "amihud_20", "ma60_ratio"]
        elif strategy == "defensive_enhanced":
            factor_names = ["vol_20", "turnover_5", "turnover_std_20", "amount_20d",
                            "max_ret_20", "ret_20", "boll_pos", "amihud_20", "ma60_ratio"]
        elif strategy == "defensive_valuation":
            factor_names = ["vol_20", "turnover_5", "turnover_std_20", "amount_20d",
                            "max_ret_20", "ret_20", "boll_pos", "amihud_20", "ma60_ratio"]
        else:
            factor_names = ["ret_5", "boll_pos", "macd_hist", "rsi14",
                            "main_net_ratio_5d", "inflow_days_5", "institution_trend"]
        piv = _pivot_factors(repo, factor_names, version)
        if strategy in ("defensive_enhanced", "defensive_valuation"):
            piv.update(_pivot_factors(repo, FUNDAMENTAL_FACTOR_NAMES, FUNDAMENTAL_VERSION))

        sector_map = {}
        for r in repo.query("SELECT symbol, sector_code FROM dim_sector_member"):
            sector_map.setdefault(r["symbol"], r["sector_code"])
        st_symbols = {
            r["symbol"] for r in repo.query("SELECT symbol, name FROM dim_stock")
            if "ST" in (r["name"] or "") or "退" in (r["name"] or "")
        }
    if eligibility is None and panel is not None:
        eligibility = panel.get("eligibility")
    if universe:
        keep = [c for c in close.columns if c in set(universe)]
        close, ret, pct = close[keep], ret[keep], pct[keep]
        piv = {k: v.reindex(columns=keep) for k, v in piv.items()}
        if eligibility is not None:
            eligibility = eligibility.reindex(index=close.index, columns=close.columns)
    if close.empty:
        raise RuntimeError("无行情数据")
    if score_override is not None and "vol_20" not in piv:
        vol_piv = _pivot_factors(repo, ["vol_20"], version)
        piv = {**piv, **vol_piv}

    if strategy == "flow_rules":
        from ..strategy.flow_rules import build_flow_rules_score

        score = build_flow_rules_score(piv, close, pct, sector_map, st_symbols)
    elif score_override is not None:
        score = score_override
    elif strategy == "defensive":
        score = defensive_score(piv, close, st_symbols=st_symbols)
    elif strategy == "defensive_enhanced":
        score = defensive_enhanced_score(piv, close, st_symbols=st_symbols)
    elif strategy == "defensive_valuation":
        score = defensive_enhanced_score(piv, close, st_symbols=st_symbols,
                                         weights=DEFENSIVE_VALUATION_WEIGHTS)
    elif strategy == "inverted":
        score = inverted_score(piv, close, st_symbols=st_symbols)
    else:
        score = composite_score(piv, close.index, close.columns)

    # 时点可交易性：ST/退市/长期停牌/上市不足 → 当日不可选
    if eligibility is not None:
        mask = eligibility.reindex(index=score.index, columns=score.columns).fillna(False).astype(bool)
        score = score.where(mask)

    idx = close.index
    if start_date:
        idx = idx[idx >= start_date]
    if end_date:
        idx = idx[idx <= end_date]
    if len(idx) < 3:
        raise RuntimeError("回测区间过短")

    # 市场择时：沪深300 收盘价在 200 日均线之上才持仓
    market_ok = None
    if market_filter:
        try:
            from ..datasource.akshare_src import tx_index_daily

            idf = tx_index_daily("sh000300")
            idf = idf[(idf["trade_date"] >= str(idx[0])) & (idf["trade_date"] <= str(idx[-1]))]
            if not idf.empty:
                s_idx = idf.set_index("trade_date")["close"].sort_index()
                market_ok = s_idx > s_idx.rolling(200, min_periods=60).mean()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"市场择时数据失败，忽略择时: {e}")

    score_lag = score.shift(1)  # 用 t-1 信号在 t 执行
    symbols = list(close.columns)
    w_prev = pd.Series(0.0, index=symbols)
    ret_filled = ret.replace([np.inf, -np.inf], np.nan).fillna(0)

    # 动态规则：以等权组合 20 日波动作为市场波动代理
    mkt_vol = mkt_vol_med = None
    if dynamic_top_k or dynamic_vol_target:
        mkt_vol = ret_filled.mean(axis=1).rolling(20, min_periods=10).std() * np.sqrt(252)
        mkt_vol_med = mkt_vol.rolling(250, min_periods=60).median()

    # 基准：等权买入持有（期初等权，持有到期末，不计交易成本）
    c = close.loc[idx].ffill()
    bh = (c / c.iloc[0]).mean(axis=1)

    nav = initial_capital
    navs, benches, dds, dates_out = [], [], [], []
    exposures: list[float] = []
    cost_paid = 0.0
    weight_hist: list[pd.Series] = []
    contrib_hist: list[pd.Series] = []
    period = {"daily": 1, "weekly": 5, "monthly": 20, "bimonthly": 40, "quarterly": 60}.get(rebalance, 1)
    vol_20 = piv.get("vol_20")
    proxy_nav, proxy_hist = 1.0, []
    proxy_rets: list[float] = []
    target_prev = pd.Series(0.0, index=symbols)

    for i, d in enumerate(idx):
        # 1) 持仓收益（t-1 收盘建仓 → t 收盘）
        ret_d = ret_filled.loc[d]
        r = (w_prev * ret_d).sum()
        nav *= (1 + r)
        if return_weights:
            weight_hist.append(w_prev.copy())
            contrib_hist.append((w_prev * ret_d).copy())
        # 篮子代理净值（不含成本/空仓），用于篮子趋势择时，严格使用 t 日收盘已知信息
        r_proxy = float((target_prev * ret_d).sum())
        proxy_nav *= (1 + r_proxy)
        proxy_hist.append(proxy_nav)
        proxy_rets.append(r_proxy)

        # 2) 按 t-1 信号生成 t 日目标权重（支持日频/周频/月频、持仓缓冲、加权方式）
        held = [s for s in symbols if w_prev[s] > 0]
        overlays_today = not (period > 1 and (i % period != 0))
        high_vol = False
        if mkt_vol is not None:
            mv, mvmed = mkt_vol.get(d), mkt_vol_med.get(d)
            high_vol = bool(mv is not None and mvmed is not None
                            and not pd.isna(mv) and not pd.isna(mvmed)
                            and mvmed > 0 and mv > 1.2 * mvmed)
        k_today = min(30, max(1, int(round(top_k * 1.5)))) if (dynamic_top_k and high_vol) else top_k
        if not overlays_today:
            target = w_prev.copy()
        elif d in score_lag.index:
            target = _target_weights(score_lag.loc[d], k_today, held, hold_buffer,
                                     weighting=weighting,
                                     vol=None if vol_20 is None else vol_20.loc[d]
                                     if d in vol_20.index else None,
                                     sector_map=universe_sector_map or sector_map,
                                     max_weight=max_weight, max_sector=max_sector)
        else:
            target = pd.Series(0.0, index=symbols)

        # 市场择时：空头区间清仓（每日生效）
        if market_ok is not None:
            ok = market_ok.get(d)
            if ok is None or pd.isna(ok) or not bool(ok):
                target = pd.Series(0.0, index=symbols)

        # 以下叠加层仅在再平衡日生效，避免对已缩放仓位重复缩放
        if overlays_today:
            # 篮子趋势择时：代理净值跌破 N 日均线则空仓（或降低至 frac 仓位）
            if basket_trend and len(proxy_hist) >= basket_trend:
                if proxy_nav < float(np.mean(proxy_hist[-basket_trend:])):
                    target = target * basket_trend_frac

            # 波动率目标：按篮子近期年化波动缩放仓位（下限 30%）
            vt = vol_target * 0.8 if (dynamic_vol_target and high_vol and vol_target) else vol_target
            if vt and len(proxy_rets) >= 20:
                rv = float(np.std(proxy_rets[-20:]) * np.sqrt(252))
                if rv > vt:
                    target = target * max(0.3, vt / rv)

            # 权重平滑：向昨日持仓靠拢，降低换手；保持平滑前后的总仓位不变
            if weight_smoothing > 0:
                raw_sum = float(target.sum())
                target = (1 - weight_smoothing) * target + weight_smoothing * w_prev
                new_sum = float(target.sum())
                if raw_sum > 0 and new_sum > 0:
                    target = target * (raw_sum / new_sum)

        target_prev = target.copy()

        # 3) 涨跌停约束（可切换统一阈值/分板块阈值）
        pct_d = pct.loc[d]
        w_new = target.copy()
        for s in symbols:
            if pd.isna(pct_d.get(s)):
                continue
            lim = _limit_threshold(s, d, limit_model)
            if target[s] > w_prev[s] and pct_d[s] >= lim:      # 涨停不可买
                w_new[s] = w_prev[s]
            elif target[s] < w_prev[s] and pct_d[s] <= -lim:   # 跌停不可卖
                w_new[s] = w_prev[s]

        # 4) 换手与成本（flat=统一费率；a_share=历史印花税 + 佣金/过户费/滑点）
        delta = w_new - w_prev
        buy = float(delta.clip(lower=0).sum())
        sell = float((-delta.clip(upper=0)).sum())
        turnover = 0.5 * (buy + sell)
        if cost_model == "a_share":
            buy_rate, sell_rate = _a_share_rates(d, cost_rate)
            cost = buy * buy_rate + sell * sell_rate
        else:
            cost = turnover * cost_rate
        nav *= (1 - cost)
        cost_paid += nav * cost
        exposures.append(float(w_prev.sum()))

        w_prev = w_new
        dates_out.append(d)
        navs.append(nav)
        benches.append(initial_capital * float(bh.loc[d]))
        dds.append(nav / max(navs) - 1)

    nav_s = pd.Series(navs, index=pd.Index(dates_out, name="trade_date"))
    bench_s = pd.Series(benches, index=pd.Index(dates_out, name="trade_date"))
    metrics = compute_metrics(nav_s, bench_s)
    metrics["avg_exposure"] = float(np.mean(exposures))
    metrics["cost_paid"] = float(cost_paid / initial_capital)

    # 落库
    run_id = datetime.now().strftime("bt_%Y%m%d_%H%M%S_%f")
    if persist:
        repo.upsert(
            "backtest_run",
            [{
                "run_id": run_id,
                "strategy": f"{strategy}_top{top_k}_{rebalance}"
                            + (f"_{universe_tag}" if universe_tag else ""),
                "params": json.dumps({
                    "top_k": top_k, "cost_rate": cost_rate, "lag": 1,
                    "cost_model": cost_model, "limit_model": limit_model,
                    "rebalance": rebalance, "hold_buffer": hold_buffer,
                    "weighting": weighting, "max_weight": max_weight,
                    "max_sector": max_sector, "vol_target": vol_target,
                    "basket_trend": basket_trend,
                    "universe": universe_tag,
                }, ensure_ascii=False),
                "start_date": dates_out[0],
                "end_date": dates_out[-1],
                "data_version": version,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }],
            ["run_id"],
        )
        repo.upsert(
            "backtest_result",
            [{"run_id": run_id, "trade_date": nav_s.index[j], "nav": float(nav_s.iloc[j]),
              "benchmark": float(bench_s.iloc[j]), "drawdown": float(nav_s.iloc[j] / max(navs[:j + 1]) - 1)}
             for j in range(len(nav_s))],
            ["run_id", "trade_date"],
        )
    logger.info(f"回测完成 {run_id}: 年化 {metrics['annual_return']:.2%}, 夏普 {metrics['sharpe']:.2f}, 回撤 {metrics['max_drawdown']:.2%}")

    result = {"run_id": run_id, "metrics": metrics, "nav": nav_s, "benchmark": bench_s,
              "start": dates_out[0], "end": dates_out[-1], "top_k": top_k,
              "rebalance": rebalance, "hold_buffer": hold_buffer, "strategy": strategy}
    if return_weights:
        idx_out = pd.Index(dates_out, name="trade_date")
        result["weights"] = pd.DataFrame(weight_hist, index=idx_out)
        result["contrib"] = pd.DataFrame(contrib_hist, index=idx_out)
    return result
