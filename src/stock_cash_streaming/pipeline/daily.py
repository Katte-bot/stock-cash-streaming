"""每日自动流水线：增量行情 → 因子 → 信号 → 目标组合 → 模拟跟踪。

设计原则：
- 幂等：所有写入均为 upsert，重复执行不会产生重复数据。
- 可跳过：非交易日或行情未更新到今日时，记录 skipped 并停止后续重计算。
- 可审计：每一步结果写入 task_log（task_id = daily_YYYYMMDD），便于回溯。
"""

from __future__ import annotations

import json
from datetime import datetime

from ..common.logging import logger
from ..datasource.registry import get_source
from ..features.engine import compute_all
from ..features.fundamentals import compute_fundamental_factors
from ..ingest.incremental import is_trading_day, run_incremental
from ..research.universe import C0, universe_symbols
from ..storage.repository import open_repository
from ..strategy.portfolio import STRATEGY_NAMES, build_target_portfolio
from ..strategy.signal import generate_signals
from ..strategy.tracking import simulate_tracking


def _latest_quote_date(repo) -> str | None:
    r = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")
    return r[0]["d"] if r else None


def run_daily_pipeline(force: bool = False, lookback_days: int = 10) -> dict:
    """执行一次完整流水线，返回各步骤结果。"""
    started = datetime.now()
    today = started.strftime("%Y-%m-%d")
    task_id = f"daily_{started.strftime('%Y%m%d')}"
    result: dict = {"task_id": task_id, "date": today, "status": "ok", "steps": {}}
    repo = open_repository()
    try:
        if not is_trading_day() and not force:
            result.update(status="skipped", reason="非交易日")
            return result

        before = _latest_quote_date(repo)
        result["quote_before"] = before

        # 1) 增量行情与资金流快照
        try:
            result["steps"]["incremental"] = run_incremental(get_source(), repo, lookback_days)
        except Exception as e:  # noqa: BLE001
            result["steps"]["incremental"] = {"error": str(e)}
            result.update(status="failed", error=f"增量失败: {e}")
            return result

        after = _latest_quote_date(repo)
        result["quote_after"] = after
        if after != today and not force:
            result.update(status="skipped",
                          reason=f"行情未更新到今日（最新 {after}）")
            return result

        # 2) 因子（v2 技术/资金流 + v3 基本面）
        result["steps"]["factors"] = compute_all(repo)
        try:
            result["steps"]["fundamentals"] = compute_fundamental_factors(
                repo, symbols=universe_symbols(repo, C0))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"基本面因子重算失败（候选策略将缺估值分）: {e}")
            result["steps"]["fundamentals"] = {"error": str(e)}

        # 3) 板块内信号
        sigs = generate_signals(repo)
        result["steps"]["signals"] = {"count": len(sigs)}

        # 4) 目标组合与调仓清单（生产 + 候选纸面跟踪，各自独立 strategy 名）
        pf = build_target_portfolio(repo, persist=True)
        result["steps"]["portfolio"] = {
            "positions": len(pf.get("positions", [])),
            "action": pf.get("action"),
            "exposure": pf.get("exposure"),
        }
        cand = build_target_portfolio(repo, persist=True, strategy="defensive_valuation")
        result["steps"]["portfolio_candidate"] = {
            "strategy": cand.get("strategy"),
            "positions": len(cand.get("positions", [])),
            "action": cand.get("action"),
            "exposure": cand.get("exposure"),
        }

        # 5) 模拟跟踪净值（生产 + 候选）
        tr = simulate_tracking(repo, persist=True)
        result["steps"]["tracking"] = {
            "status": tr.get("status"),
            "days": tr.get("days", 0),
        }
        trc = simulate_tracking(repo, strategy=STRATEGY_NAMES["defensive_valuation"],
                                persist=True)
        result["steps"]["tracking_candidate"] = {
            "status": trc.get("status"),
            "days": trc.get("days", 0),
        }

        # 6) 风险监控（回撤告警；IC 监控按需手动运行）
        try:
            from ..research.monitor import run_drawdown_monitor

            dd = run_drawdown_monitor(repo)
            result["steps"]["risk_monitor"] = {
                "alerts": [x["strategy"] for x in dd if x.get("status") == "alert"],
                "checked": [x["kind"] for x in dd],
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"回撤监控失败: {e}")
            result["steps"]["risk_monitor"] = {"error": str(e)}

        result["status"] = "ok"
        logger.info(f"每日流水线完成 {today}: {result['status']}")
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception(f"每日流水线失败: {e}")
        result.update(status="failed", error=str(e))
        return result
    finally:
        ended = datetime.now()
        try:
            repo.log_task(
                task_id, "daily", result.get("status", "unknown"),
                started.strftime("%Y-%m-%d %H:%M:%S"),
                ended.strftime("%Y-%m-%d %H:%M:%S"),
                json.dumps(result, ensure_ascii=False, default=str),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"task_log 写入失败: {e}")
        repo.close()
