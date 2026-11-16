"""内存护栏：重计算前检查系统可用内存，吃紧时主动中止而不是被内核 OOM kill。

背景：全 A 研究因子一次载入数亿行长表曾把进程推到 40 GB，被内核直接杀死，
日志里只剩半截输出。这里改成「先算小批、每批前检查余量」，宁可失败退出并
保留已落盘的中间结果，也不让整机进入 swap 抖动。
"""

from __future__ import annotations

import os

from .logging import logger


class MemoryGuardError(RuntimeError):
    """可用内存低于阈值，主动中止。"""


def available_gb() -> float:
    """系统可用内存（GB）；非 Linux 或读取失败时返回无穷大。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1048576
    except OSError:
        pass
    return float("inf")


def rss_gb() -> float:
    """当前进程 RSS（GB）。"""
    try:
        with open("/proc/self/statm", encoding="utf-8") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1073741824
    except (OSError, ValueError, IndexError):
        return 0.0


def total_gb() -> float:
    """系统物理内存（GB）；读取失败时返回无穷大。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1048576
    except OSError:
        pass
    return float("inf")


def min_available_gb() -> float:
    """保底可用内存阈值。

    未显式设置 `SCS_MIN_AVAIL_GB` 时按物理内存的 20% 自适应：
    32 GB 机器约 6.4 GB，2 GB 云主机约 0.4 GB。原先固定 6 GB 会让 2 GB
    的云主机在第一次读因子时就中止，页面长期空白。
    """
    raw = os.getenv("SCS_MIN_AVAIL_GB")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    total = total_gb()
    if total != float("inf"):
        return max(0.3, round(total * 0.2, 2))
    return 6.0


def _cap_need(need: float) -> float:
    """把单步需求压到物理内存的 60% 以内，避免小机器上阈值永远达不到。

    明显不合理的需求（超过物理内存 10 倍）原样保留，让护栏直接拒绝，
    否则调用方写了天文数字也会被静默放行。
    """
    total = total_gb()
    if total == float("inf") or need > total * 10:
        return need
    return min(need, max(0.2, round(total * 0.6, 2)))


def ensure_memory(need_gb: float | None = None, tag: str = "") -> None:
    """确认可用内存足够；不足则抛 MemoryGuardError（附当前余量与 RSS）。"""
    need = min_available_gb() if need_gb is None else _cap_need(need_gb)
    avail = available_gb()
    if avail < need:
        raise MemoryGuardError(
            f"可用内存 {avail:.1f} GB < 阈值 {need:.1f} GB，已中止 {tag}"
            f"（当前进程 RSS {rss_gb():.1f} GB）")
    logger.debug(f"内存检查通过 {tag}：可用 {avail:.1f} GB，进程 RSS {rss_gb():.1f} GB")
