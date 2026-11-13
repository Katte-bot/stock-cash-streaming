"""每日策略研究跑批：把策略库在「内置池 + 自定义池」上的回测结果落库。

Web 端「策略研究」页只读 `fact_strategy_run`，如果不在后台跑批，页面永远是
空白（每张卡片都显示“未运行”）。这里把跑批挂到每日流水线：

- 跳过 A0 全 A：研究因子缓存体积与内存不可控，2 GB 云主机承担不了；
- 单池成员数有上限（`SCS_DAILY_RESEARCH_MAX_MEMBERS`，默认 300）；
- 幂等：该池最近完整交易日已经跑过的策略直接跳过；
- 单策略失败只记录，不影响其它策略与后续步骤；
- 可用 `SCS_DAILY_RESEARCH=0` 关闭，或 `SCS_DAILY_STRATEGIES` 指定策略清单。
"""

from __future__ import annotations

import os
from datetime import datetime

from ..common.logging import logger
from ..ingest.incremental import latest_complete_quote_date
from ..research.universe import universe_members
from ..storage.repository import Repository
from ..strategy.registry import ensure_registered, list_specs

DEFAULT_STRATEGIES = (
    "defensive",
    "value_quality",
    "residual_momentum",
    "conservative_investment",
    "pead",
    "low_vol_multidim",
)


def _env_list(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.getenv(name, "")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items or list(default)


def research_universes(repo: Repository) -> list[str]:
    """参与每日跑批的池：非 A0、成员数在 [3, 上限] 之间。"""
    limit = int(os.getenv("SCS_DAILY_RESEARCH_MAX_MEMBERS", "300"))
    rows = repo.query(
        "SELECT u.universe_id AS uid, COUNT(m.symbol) AS n "
        "FROM dim_universe u LEFT JOIN dim_universe_member m "
        "ON m.universe_id = u.universe_id "
        "WHERE u.universe_id <> 'A0' GROUP BY u.universe_id ORDER BY u.universe_id")
    return [r["uid"] for r in rows if 3 <= int(r["n"] or 0) <= limit]


def _universe_as_of(repo: Repository, universe_id: str) -> str | None:
    """该池最近一个成员覆盖率 ≥90% 的交易日。"""
    symbols = list(universe_members(repo, universe_id) or {})
    if not symbols:
        return None
    return latest_complete_quote_date(repo, symbols)


def _already_ran(repo: Repository, strategy_id: str, universe_id: str,
                 run_date: str) -> bool:
    """当天是否已跑过该组合（按运行日期判重，避免因各池数据末日不同而重复回测）。"""
    rows = repo.query(
        "SELECT 1 FROM fact_strategy_run "
        "WHERE strategy_id=? AND universe_id=? AND substr(created_at,1,10)=? LIMIT 1",
        (strategy_id, universe_id, run_date))
    return bool(rows)


def run_daily_research(repo: Repository, universes: list[str] | None = None,
                       strategies: list[str] | None = None) -> dict:
    """跑一遍策略研究回测并落库；返回 {池:策略 -> 结果/跳过原因}。"""
    if os.getenv("SCS_DAILY_RESEARCH", "1") == "0":
        return {"status": "disabled"}

    from ..research.runner import run_strategy  # 延迟导入，避免循环依赖

    ensure_registered()
    known = {s.id for s in list_specs()}
    wanted = strategies or _env_list("SCS_DAILY_STRATEGIES", DEFAULT_STRATEGIES)
    strategies = [s for s in wanted if s in known]
    universes = universes or research_universes(repo)
    run_date = datetime.now().strftime("%Y-%m-%d")

    out: dict[str, dict] = {}
    for uid in universes:
        as_of = _universe_as_of(repo, uid)
        for sid in strategies:
            key = f"{uid}:{sid}"
            if _already_ran(repo, sid, uid, run_date):
                out[key] = {"status": "skipped", "end": as_of}
                continue
            try:
                res = run_strategy(repo, sid, universe=uid, persist=True)
                m = res["metrics"]
                out[key] = {
                    "status": "ok",
                    "end": res.get("end"),
                    "annual": round(float(m.get("annual_return") or 0), 4),
                    "sharpe": round(float(m.get("sharpe") or 0), 3),
                    "score": res["scorecard"]["score"],
                }
                logger.info(f"研究跑批 {key}: 年化 {m.get('annual_return', 0):.2%} "
                            f"夏普 {m.get('sharpe', 0):.2f}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"研究跑批 {key} 失败: {e}")
                out[key] = {"status": "failed", "error": str(e)}
    ok = sum(1 for v in out.values() if v.get("status") == "ok")
    return {"ran": ok, "total": len(out), "items": out}
