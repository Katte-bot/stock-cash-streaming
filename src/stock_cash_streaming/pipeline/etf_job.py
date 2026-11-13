"""ETF（159207）分析每日任务：子进程隔离运行分析脚本。

为什么用子进程：
- 脚本要拉 50 只成分股的历史行情/分红，内存与网络波动都独立于 Web 进程；
- uvicorn 只有 2 GB 内存时，把分析放在 Web 进程里容易被 OOM 连带杀掉。

结果写入 `data/etf159207_cache/`，页面读最近一次结果；失败时保留上次结果，
并在 `task_log`（task_id = `etf_YYYYMMDD`）标记失败，供调度器重试。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ..common.logging import logger
from ..storage.repository import open_repository

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "etf_159207_rebalance.py"


def etf_ran_ok_today() -> bool:
    """当日是否已成功跑过 ETF 分析（避免流水线重试时重复跑 2 分钟）。"""
    task_id = f"etf_{datetime.now():%Y%m%d}"
    try:
        repo = open_repository()
        try:
            rows = repo.query(
                "SELECT status FROM task_log WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                (task_id,))
            return bool(rows) and rows[0]["status"] == "ok"
        finally:
            repo.close()
    except Exception:  # noqa: BLE001
        return False


def run_etf_analysis(timeout: int | None = None) -> dict:
    """执行一次 ETF 分析；返回状态摘要，不抛异常（调用方决定是否告警）。"""
    if not SCRIPT.exists():
        return {"status": "skipped", "reason": f"脚本不存在：{SCRIPT}"}

    limit = timeout or int(os.getenv("SCS_ETF_TIMEOUT", "1800"))
    started = datetime.now()
    day = started.strftime("%Y%m%d")
    task_id = f"etf_{day}"
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")

    logger.info(f"ETF 分析开始：{SCRIPT} run（超时 {limit}s）")
    result: dict
    try:
        proc = subprocess.run(
            [sys.executable, "-u", str(SCRIPT), "run"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=limit)
        tail = (proc.stdout or "")[-1500:]
        result = {
            "status": "ok" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "tail": tail,
        }
        if proc.returncode != 0:
            result["stderr"] = (proc.stderr or "")[-800:]
    except subprocess.TimeoutExpired:
        result = {"status": "failed", "error": f"超时 {limit}s"}
    except Exception as e:  # noqa: BLE001
        result = {"status": "failed", "error": str(e)}

    ended = datetime.now()
    result["elapsed_s"] = round((ended - started).total_seconds(), 1)
    logger.info(f"ETF 分析结束：{result['status']}（{result['elapsed_s']}s）")

    # 单独落一条 task_log，便于调度器判断是否需要重试
    try:
        repo = open_repository()
        try:
            repo.log_task(
                task_id, "etf", result["status"],
                started.strftime("%Y-%m-%d %H:%M:%S"),
                ended.strftime("%Y-%m-%d %H:%M:%S"),
                str(result)[:4000])
        finally:
            repo.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ETF task_log 写入失败: {e}")

    return result
