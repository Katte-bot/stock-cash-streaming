"""统一日志（loguru）：控制台 + 按日轮转文件。"""

import sys

from loguru import logger

_configured = False


def setup_logging(level: str = "INFO"):
    """初始化日志（幂等）。"""
    global _configured
    if _configured:
        return logger

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{message}</cyan>",
    )
    logger.add(
        "logs/app_{time:YYYY-MM-DD}.log",
        level=level,
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
    )
    _configured = True
    return logger


__all__ = ["logger", "setup_logging"]
