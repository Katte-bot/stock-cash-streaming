"""轻量每日调度器（无第三方依赖）。

用法：
    python -m stock_cash_streaming.cli scheduler --time 16:10

行为：
- 工作日到达指定时间后运行一次每日流水线；
- 启动时若当天已过点且尚未成功运行，会立即补跑一次；
- 行情数据源收盘后分批发布日线，若当日覆盖不全（流水线返回 `retryable`），
  每隔 `RETRY_INTERVAL_MIN` 分钟重试，最多 `MAX_ATTEMPTS` 次；
- 失败不自动重试（需手动执行 `cli daily` 排查）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from ..common.logging import logger
from ..storage.repository import open_repository
from .daily import run_daily_pipeline

RETRY_INTERVAL_MIN = 20    # 行情未发布完整时的重试间隔（分钟）
MAX_ATTEMPTS = 6           # 每个交易日最多尝试次数（约覆盖 2 小时）


def _task_state(day: str) -> tuple[str | None, bool]:
    """读取当日流水线状态：返回 (status, 是否可重试)。无记录时为 (None, False)。"""
    task_id = f"daily_{day.replace('-', '')}"
    repo = open_repository()
    try:
        rows = repo.query("SELECT status, message FROM task_log WHERE task_id=?", (task_id,))
    finally:
        repo.close()
    if not rows:
        return None, False
    status = rows[0]["status"]
    retryable = False
    try:
        retryable = bool(json.loads(rows[0]["message"] or "{}").get("retryable"))
    except (TypeError, ValueError):
        retryable = False
    return status, retryable


def run_scheduler(target_time: str = "16:10", force: bool = False) -> None:
    try:
        hh, mm = (int(x) for x in target_time.split(":"))
    except ValueError as e:
        raise ValueError(f"时间格式应为 HH:MM，收到 {target_time}") from e

    logger.info(f"每日调度器已启动：工作日 {hh:02d}:{mm:02d} 运行流水线")
    attempts: dict[str, int] = {}     # 当日已尝试次数
    last_attempt: datetime | None = None
    while True:
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        cooled = (last_attempt is None
                  or (now - last_attempt).total_seconds() >= RETRY_INTERVAL_MIN * 60)
        if now.weekday() < 5 and now >= target and cooled \
                and attempts.get(day, 0) < MAX_ATTEMPTS:
            status, retryable = _task_state(day)
            if status == "ok" or status == "failed" or (status == "skipped" and not retryable):
                logger.info(f"{day} 已处理（{status}），跳过")
                attempts[day] = MAX_ATTEMPTS
            else:
                last_attempt = now
                attempts[day] = attempts.get(day, 0) + 1
                logger.info(f"{day} 第 {attempts[day]} 次触发每日流水线")
                try:
                    res = run_daily_pipeline(force=force)
                    logger.info(f"{day} 流水线结束：{res.get('status')} "
                                f"{res.get('reason', '')}")
                    if res.get("retryable") and attempts[day] >= MAX_ATTEMPTS:
                        logger.warning(f"{day} 已重试 {MAX_ATTEMPTS} 次仍未拿到完整行情，"
                                       f"请手动执行 cli daily 检查数据源")
                except Exception as e:  # noqa: BLE001
                    logger.exception(f"{day} 流水线异常：{e}")
        time.sleep(30)
