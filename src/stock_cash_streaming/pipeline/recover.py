"""异常中断恢复：补行情 → 重算因子 → 补漏掉的调仓快照 → 重放跟踪。

适用场景：机器停机、进程被杀、数据源故障，导致若干交易日没有运行每日流水线。

设计要点：
- 幂等：所有写入均为 upsert，可重复执行；`--dry-run` 只读不写。
- 只重放 `is_rebalance=1` 的调仓快照（跟踪只认这些快照），按交易日升序。
- 行情回看窗口由 `lookback_days` 控制，需覆盖中断天数。
- 首次使用（库内无任何快照）时只建立最新日基线，不回放历史调仓日。

说明：本模块只补齐数据与跟踪，不构成投资建议。
"""

from __future__ import annotations

import json
from datetime import datetime

from ..common.logging import logger
from ..datasource.registry import get_source
from ..features.engine import compute_all
from ..ingest.incremental import run_incremental
from ..storage.repository import open_repository
from ..strategy.portfolio import (
    REBALANCE_PERIOD,
    STRATEGY_NAME,
    build_target_portfolio,
)
from ..strategy.signal import generate_signals
from ..strategy.tracking import simulate_tracking


def _quote_dates(repo) -> list[str]:
    return [r["d"] for r in repo.query(
        "SELECT DISTINCT trade_date AS d FROM fact_quote ORDER BY trade_date")]


def _last_snapshot(repo) -> str | None:
    r = repo.query(
        "SELECT MAX(trade_date) AS d FROM fact_target_portfolio WHERE strategy=?",
        (STRATEGY_NAME,),
    )
    return r[0]["d"] if r else None


def _missing_rebalances(dates: list[str], last: str | None) -> list[str]:
    """返回 last 之后、且落在 20 交易日调仓周期上的日期（升序）。

    与 portfolio.py / backtest 的 `i % 20 == 0` 口径一致；因此调用前需保证
    行情已补齐，否则交易日序号会错位。
    """
    return [d for i, d in enumerate(dates)
            if (last is None or d > last) and i % REBALANCE_PERIOD == 0]


def run_recovery(
    lookback_days: int = 30,
    skip_ingest: bool = False,
    dry_run: bool = False,
) -> dict:
    """执行一次中断恢复，返回各步骤结果。"""
    started = datetime.now()
    today = started.strftime("%Y-%m-%d")
    task_id = f"recover_{started.strftime('%Y%m%d')}"
    result: dict = {
        "task_id": task_id, "date": today, "status": "ok",
        "steps": {}, "missing_rebalances": [], "replayed": [],
    }
    repo = open_repository()
    try:
        before = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")[0]["d"]
        result["quote_before"] = before
        dates = _quote_dates(repo)
        if not dates:
            result.update(status="failed", error="库内无行情，请先执行 cli init")
            return result

        last = _last_snapshot(repo)
        result["last_snapshot"] = last

        # 只读模式：按现有行情推算缺口，不写库
        if dry_run:
            result["missing_rebalances"] = _missing_rebalances(dates, last)
            result["status"] = "dry-run"
            return result

        # 1) 补行情（回看窗口需覆盖中断天数）
        if not skip_ingest:
            result["steps"]["incremental"] = run_incremental(
                get_source(), repo, lookback_days)
        after = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")[0]["d"]
        result["quote_after"] = after
        dates = _quote_dates(repo)

        # 2) 全量重算因子（滚动窗口按交易日序列计算）
        result["steps"]["factors"] = compute_all(repo)

        # 3) 信号
        sigs = generate_signals(repo)
        result["steps"]["signals"] = {"count": len(sigs)}

        # 4) 重放漏掉的调仓快照（必须按日期升序，保证持仓缓冲与变化清单正确）
        last = _last_snapshot(repo)
        if last is None:
            # 首次运行：只建最新日基线，不回放历史调仓日
            missing: list[str] = []
        else:
            missing = _missing_rebalances(dates, last)
        result["missing_rebalances"] = missing
        for d in missing:
            pf = build_target_portfolio(repo, trade_date=d, persist=True)
            result["replayed"].append({
                "date": d,
                "action": pf.get("action"),
                "positions": len(pf.get("positions", [])),
            })

        # 5) 最新交易日快照（若本身是调仓日则已在上面重放）
        latest = dates[-1]
        if latest not in missing:
            pf = build_target_portfolio(repo, trade_date=latest, persist=True)
            result["steps"]["portfolio"] = {
                "date": latest,
                "action": pf.get("action"),
                "positions": len(pf.get("positions", [])),
            }

        # 6) 从首个快照完整重放跟踪净值
        tr = simulate_tracking(repo, persist=True)
        result["steps"]["tracking"] = {
            "status": tr.get("status"),
            "days": tr.get("days", 0),
            "rebalances": len(tr.get("rebalances", [])),
        }
        result["status"] = "ok"
        logger.info(
            f"中断恢复完成 {today}：补 {len(missing)} 个调仓快照，跟踪 {tr.get('days', 0)} 个交易日")
        return result
    except Exception as e:  # noqa: BLE001
        logger.exception(f"中断恢复失败: {e}")
        result.update(status="failed", error=str(e))
        return result
    finally:
        if not dry_run:
            ended = datetime.now()
            try:
                repo.log_task(
                    task_id, "recover", result.get("status", "unknown"),
                    started.strftime("%Y-%m-%d %H:%M:%S"),
                    ended.strftime("%Y-%m-%d %H:%M:%S"),
                    json.dumps(result, ensure_ascii=False, default=str),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"task_log 写入失败: {e}")
        repo.close()
