"""策略评分卡（仅用于研究排序，不构成投资建议）。"""

from __future__ import annotations


def _norm(v: float | None, lo: float, hi: float) -> float:
    if v is None or v != v:
        return 0.0
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def score_strategy(full: dict, oos: dict | None = None) -> dict:
    """0–100 分；分项权重见计划文档。"""
    cagr = _norm(full.get("annual_return"), 0.0, 0.30)
    sharpe = _norm(full.get("sharpe"), 0.0, 2.0)
    calmar = _norm(full.get("calmar"), 0.0, 2.0)
    mdd = _norm(-abs(full.get("max_drawdown") or 0.0), -0.50, 0.0)
    excess = _norm(full.get("excess_annual"), 0.0, 0.15)
    oos_ok = 0.0
    if oos and oos.get("annual_return") is not None:
        oos_ok = 1.0 if (oos.get("annual_return") or 0) > 0 and (oos.get("sharpe") or 0) > 0 else 0.0
    score = 100 * (0.25 * cagr + 0.20 * sharpe + 0.15 * calmar
                   + 0.15 * mdd + 0.15 * excess + 0.10 * oos_ok)
    reasons: list[str] = []
    if (full.get("annual_return") or 0) <= 0:
        reasons.append("全样本年化为负")
    if (full.get("sharpe") or 0) < 0.3:
        reasons.append("夏普偏低")
    if (full.get("max_drawdown") or 0) < -0.35:
        reasons.append("最大回撤超过 35%")
    if oos and (oos.get("annual_return") or 0) <= 0:
        reasons.append("样本外年化为负")
    return {"score": round(score, 1), "oos_ok": bool(oos_ok), "reasons": reasons}
