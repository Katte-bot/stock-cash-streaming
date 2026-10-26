"""轻量每日调度器（无第三方依赖）。

用法：
    python -m stock_cash_streaming.cli scheduler --time 16:10

行为：
- 工作日到达指定时间后运行一次每日流水线；
- 启动时若当天已过点且尚未成功运行，会立即补跑一次；
- 每个进程每天最多尝试一次，失败不自动重试（可手动执行 `cli daily`）。
"""

from __future__ import annotations

import time
from datetime import datetime

from ..common.logging import logger
from ..storage.repository import open_repository
from .daily import run_daily_pipeline


def _already_ran(day: str) -> bool:
    task_id = f"daily_{day.replace('-', '')}"
    repo = open_repository()
    try:
        rows = repo.query("SELECT status FROM task_log WHERE task_id=?", (task_id,))
        return bool(rows and rows[0]["status"] in ("ok", "skipped"))
    finally:
        repo.close()


def run_scheduler(target_time: str = "16:10", force: bool = False) -> None:
    try:
        hh, mm = (int(x) for x in target_time.split(":"))
    except ValueError as e:
        raise ValueError(f"时间格式应为 HH:MM，收到 {target_time}") from e

    logger.info(f"每日调度器已启动：工作日 {hh:02d}:{mm:02d} 运行流水线")
    attempted_day: str | None = None
    while True:
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now.weekday() < 5 and now >= target and attempted_day != day:
            attempted_day = day
            if _already_ran(day):
                logger.info(f"{day} 已运行，跳过")
            else:
                logger.info(f"{day} 触发每日流水线")
                try:
                    res = run_daily_pipeline(force=force)
                    logger.info(f"{day} 流水线结束：{res.get('status')} {res.get('reason', '')}")
                except Exception as e:  # noqa: BLE001
                    logger.exception(f"{day} 流水线异常：{e}")
        time.sleep(30)
