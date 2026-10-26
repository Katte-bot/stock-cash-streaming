"""防御增强版研究实验：因子消融、组合构建、风险参数与滚动验证。

所有实验共享同一份面板（行情 + v2 技术因子 + v3 基本面因子）与同一套
股票池/可交易性掩码，避免重复查库与口径漂移。

原则：
- 每次只改变有限维度；
- 同时报告全期、样本内(2018-2022)、样本外(2023-2026)；
- 不按单一区间收益择优，全部结果落 CSV。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest.engine import (
    DEFAULT_COST_RATE, FACTOR_VERSION,
    prepare_panel, run_backtest,
)
from ..common.logging import logger
from ..research.universe import C0, eligibility_panel, universe_members
from ..storage.repository import Repository

DEFAULT_TOP_K = 12

TECH_NAMES = ["vol_20", "turnover_5", "turnover_std_20", "amount_20d",
              "max_ret_20", "ret_20", "boll_pos", "amihud_20", "ma60_ratio"]

BASE_WEIGHTS = {
    "vol_20": 0.20, "turnover_5": 0.13, "turnover_std_20": 0.06,
    "amount_20d": 0.11, "max_ret_20": 0.17, "ret_20": 0.09,
    "boll_pos": 0.06, "amihud_20": 0.18,
}

ENHANCED_WEIGHTS = {
    "vol_20": 0.20, "max_ret_20": 0.14, "amihud_20": 0.10, "turnover_5": 0.08,
    "quality": 0.15, "cash_quality": 0.10, "valuation": 0.08, "dividend": 0.06,
    "ret_20": 0.04, "boll_pos": 0.03, "turnover_std_20": 0.02,
}

POSITIVE = {"amihud_20"}

COMPONENTS = {
    "quality": [("roe_ttm", 0.4, 1), ("roe_std_8q", 0.3, -1), ("cfo_to_np", 0.3, 1)],
    "cash_quality": [("cfo_to_np", 0.5, 1), ("cfo_to_np_std_8q", 0.3, -1), ("cfo_to_rev", 0.2, 1)],
    "valuation": [("ep_ttm", 0.45, 1), ("bp_mrq", 0.35, 1), ("fcf_yield", 0.2, 1)],
    "dividend": [("div_continuity_3y", 0.4, 1), ("div_yield_3y", 0.3, 1),
                 ("div_payout", 0.2, 1), ("leverage", 0.1, -1)],
    "liquidity": [("amount_20d", 1.0, -1), ("turnover_5", 1.0, -1), ("amihud_20", 1.0, 1)],
}

PERIODS = {
    "full": (None, None),
    "in": ("2018-01-01", "2022-12-31"),
    "out": ("2023-01-01", None),
}

B3_WEIGHTS = {k: v * 0.92 for k, v in BASE_WEIGHTS.items()}
B3_WEIGHTS["valuation"] = 0.08


# ---------- 评分 ----------

def _renorm(w: dict[str, float]) -> dict[str, float]:
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def _sector_rank(mat: pd.DataFrame, cols: list[str], sector_map: dict[str, str] | None) -> pd.DataFrame:
    out = pd.DataFrame(np.nan, index=mat.index, columns=cols)
    groups: dict[str, list[str]] = {}
    for s in cols:
        groups.setdefault((sector_map or {}).get(s, ""), []).append(s)
    for syms in groups.values():
        sub = [s for s in syms if s in mat.columns]
        if len(sub) >= 2:
            out[sub] = mat[sub].rank(axis=1, pct=True)
        elif sub:
            out[sub] = 0.5
    return out


def score_from_weights(piv: dict[str, pd.DataFrame], close: pd.DataFrame,
                       weights: dict[str, float], sector_map: dict[str, str] | None = None,
                       neutral: bool = False, min_amount: float = 1e8) -> pd.DataFrame:
    """按权重合成评分；支持子分数（quality/valuation/...）与行业内排名。"""
    idx, cols = close.index, list(close.columns)

    def rank_of(name: str) -> pd.DataFrame | None:
        mat = piv.get(name)
        if mat is None:
            return None
        mat = mat.reindex(index=idx, columns=cols)
        return _sector_rank(mat, cols, sector_map) if neutral else mat.rank(axis=1, pct=True)

    def component(spec: list[tuple[str, float, int]]) -> pd.DataFrame | None:
        parts: list[tuple[float, pd.DataFrame]] = []
        for name, w, direction in spec:
            r = rank_of(name)
            if r is None:
                continue
            parts.append((w, r if direction > 0 else 1 - r))
        if not parts:
            return None
        total = sum(w for w, _ in parts)
        out = pd.DataFrame(0.0, index=idx, columns=cols)
        for w, m in parts:
            out = out + w * m.fillna(0.5)
        return out / total

    total, score = 0.0, pd.DataFrame(0.0, index=idx, columns=cols)
    for name, w in weights.items():
        if name in COMPONENTS:
            sub = component(COMPONENTS[name])
        else:
            r = rank_of(name)
            sub = None if r is None else (r if name in POSITIVE else 1 - r)
        if sub is None:
            continue
        score += w * sub.fillna(0.5)
        total += w
    if total == 0:
        return pd.DataFrame(index=idx, columns=cols, dtype=float)
    score /= total

    valid = pd.DataFrame(True, index=idx, columns=cols)
    if "amount_20d" in piv:
        valid &= piv["amount_20d"].reindex(index=idx, columns=cols) > min_amount
    if "ma60_ratio" in piv:
        valid &= piv["ma60_ratio"].reindex(index=idx, columns=cols).notna()
    return score.where(valid)


# ---------- 公共执行 ----------

def _yearly(nav: pd.Series) -> dict[str, float]:
    ret = nav.pct_change().fillna(0)
    out = {}
    for y, s in ret.groupby(ret.index.str[:4]):
        out[f"y{y}"] = float((1 + s).prod() - 1)
    return out


def _evaluate(repo: Repository, score: pd.DataFrame, panel: dict, universe: list[str],
              start: str | None, end: str | None, **kwargs) -> dict:
    return run_backtest(
        repo, score_override=score, panel=panel, universe=universe,
        eligibility=panel.get("eligibility"), start_date=start, end_date=end,
        persist=False, strategy="research", **kwargs,
    )


def _row(repo: Repository, name: str, score: pd.DataFrame, panel: dict,
         universe: list[str], **kwargs) -> dict:
    row: dict = {"experiment": name}
    for period, (start, end) in PERIODS.items():
        res = _evaluate(repo, score, panel, universe, start, end, **kwargs)
        m = res["metrics"]
        if period == "full":
            row.update({
                "annual": m["annual_return"], "vol": m["annual_vol"],
                "sharpe": m["sharpe"], "max_dd": m["max_drawdown"],
                "calmar": m["calmar"], "excess": m.get("excess_annual"),
                "cost": m.get("cost_paid"), **_yearly(res["nav"]),
            })
        else:
            row[f"ann_{period}"] = m["annual_return"]
            row[f"sharpe_{period}"] = m["sharpe"]
            row[f"dd_{period}"] = m["max_drawdown"]
    return row


def _panel(repo: Repository, universe_id: str) -> tuple[list[str], dict]:
    members = universe_members(repo, universe_id)
    if not members:
        raise RuntimeError(f"股票池 {universe_id} 为空")
    symbols = list(members)
    eligibility = eligibility_panel(repo, symbols)
    panel = prepare_panel(repo, TECH_NAMES, FACTOR_VERSION, universe=symbols,
                          eligibility=eligibility, include_fundamentals=True,
                          universe_sector_map=members)
    if not panel["piv"]:
        raise RuntimeError("无因子数据")
    return symbols, panel


# ---------- 第二轮：增强因子增量 ----------

def _with_extra(extra_name: str, weight: float) -> dict[str, float]:
    base = {k: v * (1 - weight) for k, v in BASE_WEIGHTS.items()}
    base[extra_name] = weight
    return base


ROUND2_EXPERIMENTS: dict[str, dict[str, float]] = {
    "A0 原策略": BASE_WEIGHTS,
    "B1 +财务质量": _with_extra("quality", 0.15),
    "B2 +现金流质量": _with_extra("cash_quality", 0.10),
    "B3 +估值": _with_extra("valuation", 0.08),
    "B4 +股息可持续": _with_extra("dividend", 0.06),
    "B5 完整增强": ENHANCED_WEIGHTS,
}


def run_round2(repo: Repository, universe_id: str = C0,
               cost_model: str = "a_share", limit_model: str = "board") -> pd.DataFrame:
    symbols, panel = _panel(repo, universe_id)
    rows = []
    for name, weights in ROUND2_EXPERIMENTS.items():
        score = score_from_weights(panel["piv"], panel["close"], weights,
                                   panel["sector_map"], neutral=False)
        rows.append(_row(repo, name, score, panel, symbols,
                         top_k=DEFAULT_TOP_K, cost_rate=DEFAULT_COST_RATE,
                         rebalance="monthly", hold_buffer=1.0, weighting="vol_inv",
                         vol_target=0.25, cost_model=cost_model, limit_model=limit_model))
    return pd.DataFrame(rows)


# ---------- 第一轮：因子消融 A1-A7 ----------

REST_WEIGHTS = {k: v for k, v in BASE_WEIGHTS.items()
                if k not in ("amount_20d", "turnover_5", "amihud_20")}


def _liquidity_weights(total: float) -> dict[str, float]:
    scale = (1 - total) / sum(REST_WEIGHTS.values())
    w = {k: v * scale for k, v in REST_WEIGHTS.items()}
    w["liquidity"] = total
    return w


ABLATION_EXPERIMENTS: dict[str, dict] = {
    "A0 原策略": {"weights": BASE_WEIGHTS},
    "A1 去低成交额": {"weights": _renorm({k: v for k, v in BASE_WEIGHTS.items() if k != "amount_20d"})},
    "A2 去低换手": {"weights": _renorm({k: v for k, v in BASE_WEIGHTS.items() if k != "turnover_5"})},
    "A3 去非流动性": {"weights": _renorm({k: v for k, v in BASE_WEIGHTS.items() if k != "amihud_20"})},
    "A4a 流动性组20%": {"weights": _liquidity_weights(0.20)},
    "A4b 流动性组25%": {"weights": _liquidity_weights(0.25)},
    "A4c 流动性组30%": {"weights": _liquidity_weights(0.30)},
    "A5 去布林低位": {"weights": _renorm({k: v for k, v in BASE_WEIGHTS.items() if k != "boll_pos"})},
    "A6 去换手波动": {"weights": _renorm({k: v for k, v in BASE_WEIGHTS.items() if k != "turnover_std_20"})},
    "A7a 原策略+行业中性": {"weights": BASE_WEIGHTS, "neutral": True},
    "A7b 估值版+行业中性": {"weights": B3_WEIGHTS, "neutral": True},
}


def run_ablation(repo: Repository, universe_id: str = C0,
                 cost_model: str = "a_share", limit_model: str = "board") -> pd.DataFrame:
    symbols, panel = _panel(repo, universe_id)
    rows = []
    for name, cfg in ABLATION_EXPERIMENTS.items():
        score = score_from_weights(panel["piv"], panel["close"], cfg["weights"],
                                   panel["sector_map"], neutral=cfg.get("neutral", False))
        rows.append(_row(repo, name, score, panel, symbols,
                         top_k=DEFAULT_TOP_K, cost_rate=DEFAULT_COST_RATE,
                         rebalance="monthly", hold_buffer=1.0, weighting="vol_inv",
                         vol_target=0.25, cost_model=cost_model, limit_model=limit_model))
    return pd.DataFrame(rows)


# ---------- 第三轮：组合构建 ----------

PORTFOLIO_CONFIGS: dict[str, dict] = {
    "P1 top12 vol_inv": dict(top_k=12, weighting="vol_inv"),
    "P2 top20 vol_inv": dict(top_k=20, weighting="vol_inv"),
    "P3 top30 vol_inv": dict(top_k=30, weighting="vol_inv"),
    "P4 top20 equal": dict(top_k=20, weighting="equal"),
    "P5 top20 单股8%": dict(top_k=20, weighting="vol_inv", max_weight=0.08),
    "P6 top20 单股10%": dict(top_k=20, weighting="vol_inv", max_weight=0.10),
    "P7 top20 单股12%": dict(top_k=20, weighting="vol_inv", max_weight=0.12),
    "P8 top20 行业25%": dict(top_k=20, weighting="vol_inv", max_sector=0.25),
    "P9 top20 行业30%": dict(top_k=20, weighting="vol_inv", max_sector=0.30),
    "P10 top20 行业35%": dict(top_k=20, weighting="vol_inv", max_sector=0.35),
    "P11 top20 双月": dict(top_k=20, weighting="vol_inv", rebalance="bimonthly"),
    "P12 top20 季度": dict(top_k=20, weighting="vol_inv", rebalance="quarterly"),
    "P13 top20 buffer0": dict(top_k=20, weighting="vol_inv", hold_buffer=0.0),
    "P14 top20 buffer2": dict(top_k=20, weighting="vol_inv", hold_buffer=2.0),
}


def run_portfolio(repo: Repository, universe_id: str = C0,
                  cost_model: str = "a_share", limit_model: str = "board") -> pd.DataFrame:
    symbols, panel = _panel(repo, universe_id)
    score = score_from_weights(panel["piv"], panel["close"], B3_WEIGHTS,
                               panel["sector_map"], neutral=False)
    rows = []
    for name, cfg in PORTFOLIO_CONFIGS.items():
        kwargs = dict(cost_rate=DEFAULT_COST_RATE, rebalance="monthly",
                      hold_buffer=1.0, vol_target=0.25,
                      cost_model=cost_model, limit_model=limit_model)
        kwargs.update(cfg)
        rows.append(_row(repo, name, score, panel, symbols, **kwargs))
    return pd.DataFrame(rows)


# ---------- 探索：估值版精简因子集（基于 A5/A6 的消融结果） ----------

LEAN_CONFIGS: dict[str, dict[str, float]] = {
    "L1 B3-换手波动": _renorm({k: v for k, v in B3_WEIGHTS.items() if k != "turnover_std_20"}),
    "L2 B3-布林低位": _renorm({k: v for k, v in B3_WEIGHTS.items() if k != "boll_pos"}),
    "L3 B3-换手波动-布林": _renorm({k: v for k, v in B3_WEIGHTS.items()
                                   if k not in ("turnover_std_20", "boll_pos")}),
    "L4 B3-成交额-换手波动-布林": _renorm({k: v for k, v in B3_WEIGHTS.items()
                                        if k not in ("amount_20d", "turnover_std_20", "boll_pos")}),
}


def run_lean(repo: Repository, universe_id: str = C0,
             cost_model: str = "a_share", limit_model: str = "board") -> pd.DataFrame:
    symbols, panel = _panel(repo, universe_id)
    rows = []
    for name, weights in LEAN_CONFIGS.items():
        score = score_from_weights(panel["piv"], panel["close"], weights,
                                   panel["sector_map"], neutral=False)
        rows.append(_row(repo, name, score, panel, symbols,
                         top_k=DEFAULT_TOP_K, cost_rate=DEFAULT_COST_RATE,
                         rebalance="monthly", hold_buffer=1.0, weighting="vol_inv",
                         vol_target=0.25, cost_model=cost_model, limit_model=limit_model))
    return pd.DataFrame(rows)


# ---------- 股票池对比 C0/C1/C2/C3 ----------

def run_universe_compare(repo: Repository, universes: tuple[str, ...] = ("C0", "C1", "C2", "C3"),
                         cost_model: str = "a_share", limit_model: str = "board") -> pd.DataFrame:
    """同一评分方案在不同股票池上的对照（A0 与 B3）。"""
    rows: list[dict] = []
    for uid in universes:
        members = universe_members(repo, uid)
        if not members:
            logger.warning(f"股票池 {uid} 为空，跳过")
            continue
        symbols, panel = _panel(repo, uid)
        for label, weights in (("A0 原策略", BASE_WEIGHTS), ("B3 +估值", B3_WEIGHTS)):
            score = score_from_weights(panel["piv"], panel["close"], weights,
                                       panel["sector_map"], neutral=False)
            row = _row(repo, f"{uid} {label}", score, panel, symbols,
                       top_k=DEFAULT_TOP_K, cost_rate=DEFAULT_COST_RATE,
                       rebalance="monthly", hold_buffer=1.0, weighting="vol_inv",
                       vol_target=0.25, cost_model=cost_model, limit_model=limit_model)
            row["universe"] = uid
            row["members"] = len(members)
            rows.append(row)
    return pd.DataFrame(rows)


# ---------- 第四轮：风险参数 ----------

RISK_CONFIGS: dict[str, dict] = {
    "R0 波动目标关闭": dict(vol_target=0.0),
    "R1 目标12%": dict(vol_target=0.12),
    "R2 目标15%": dict(vol_target=0.15),
    "R3 目标18%": dict(vol_target=0.18),
    "R4 目标20%": dict(vol_target=0.20),
    "R5 目标25%(现行)": dict(vol_target=0.25),
    "R6 25%+沪深300 MA200": dict(vol_target=0.25, market_filter=True),
}


def run_risk(repo: Repository, universe_id: str = C0,
             cost_model: str = "a_share", limit_model: str = "board") -> pd.DataFrame:
    symbols, panel = _panel(repo, universe_id)
    score = score_from_weights(panel["piv"], panel["close"], B3_WEIGHTS,
                               panel["sector_map"], neutral=False)
    rows = []
    for name, cfg in RISK_CONFIGS.items():
        kwargs = dict(top_k=DEFAULT_TOP_K, weighting="vol_inv",
                      rebalance="monthly", hold_buffer=1.0,
                      cost_rate=DEFAULT_COST_RATE,
                      cost_model=cost_model, limit_model=limit_model)
        kwargs.update(cfg)
        rows.append(_row(repo, name, score, panel, symbols, **kwargs))
    return pd.DataFrame(rows)


# ---------- 第五轮：滚动验证 ----------

def run_walkforward(repo: Repository, universe_id: str = C0,
                    cost_model: str = "a_share", limit_model: str = "board") -> dict:
    """滚动前推：每年用过去 3 年夏普在 Top12/20/30 中选一个，用于下一年。"""
    symbols, panel = _panel(repo, universe_id)
    score = score_from_weights(panel["piv"], panel["close"], B3_WEIGHTS,
                               panel["sector_map"], neutral=False)
    navs: dict[str, pd.Series] = {}
    for k, top_k in {"top12": 12, "top20": 20, "top30": 30}.items():
        res = _evaluate(repo, score, panel, symbols, None, None, top_k=top_k,
                        cost_rate=DEFAULT_COST_RATE, rebalance="monthly",
                        hold_buffer=1.0, weighting="vol_inv", vol_target=0.25,
                        cost_model=cost_model, limit_model=limit_model)
        navs[k] = res["nav"]

    years = sorted({d[:4] for d in navs["top12"].index})
    picks: list[dict] = []
    pieces: list[pd.Series] = []
    base_nav = 1.0
    for y in years[3:]:
        train_lo, train_hi = f"{int(y) - 3}-01-01", f"{int(y) - 1}-12-31"
        best, best_sharpe = "top12", -np.inf
        for k, nav in navs.items():
            seg = nav[(nav.index >= train_lo) & (nav.index <= train_hi)]
            if len(seg) < 60:
                continue
            ret = seg.pct_change().dropna()
            sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() else -np.inf
            if sharpe > best_sharpe:
                best, best_sharpe = k, sharpe
        nav = navs[best]
        seg = nav[nav.index.str[:4] == y]
        if len(seg) < 2:
            continue
        scaled = base_nav * seg / seg.iloc[0]
        pieces.append(scaled)
        base_nav = float(scaled.iloc[-1])
        picks.append({"year": y, "pick": best, "train_sharpe": float(best_sharpe)})

    if not pieces:
        return {"error": "滚动验证样本不足"}
    wf_nav = pd.concat(pieces)
    wf_nav = wf_nav[~wf_nav.index.duplicated(keep="last")]
    from ..backtest.metrics import compute_metrics

    metrics = compute_metrics(wf_nav)
    return {"nav": wf_nav, "metrics": metrics, "picks": picks}


# ---------- 输出 ----------

def write_csv(df: pd.DataFrame, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, encoding="utf-8-sig")
    return p


__all__ = [
    "run_round2", "run_ablation", "run_lean", "run_portfolio", "run_risk",
    "run_walkforward", "run_universe_compare", "write_csv",
    "BASE_WEIGHTS", "ENHANCED_WEIGHTS", "B3_WEIGHTS",
]
