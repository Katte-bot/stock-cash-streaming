"""研究因子缓存（本地 pickle）。

全 A 日频因子不写入 SQLite，避免库体积膨胀（约 3–4 亿行）。
缓存键：因子版本 + 股票池 + 区间 + 股票集合哈希；默认 7 天过期。
"""

from __future__ import annotations

import hashlib
import pickle
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..common.logging import logger
from ..config.settings import get_settings
from ..storage.repository import Repository
from .fundamental_v5 import compute_v5
from .panel import Panel, load_panel
from .technical_v4 import compute_all_panel_factors

CACHE_TTL_DAYS = 7


def _cache_dir() -> Path:
    root = Path(get_settings().db_path).resolve().parent / "research_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _key(version: str, universe: str | None, start: str | None, end: str | None,
         symbols: list[str]) -> str:
    h = hashlib.md5(",".join(sorted(symbols)).encode()).hexdigest()[:10]
    return f"{version}_{universe or 'ALL'}_{start or 'na'}_{end or 'na'}_{h}"


def _is_fresh(path: Path, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    if not path.exists():
        return False
    age = (time.time() - path.stat().st_mtime) / 86400
    return age <= ttl_days


def load_stored_factors(repo: Repository, names: list[str], versions: list[str],
                        symbols: list[str], start: str | None, end: str | None
                        ) -> dict[str, pd.DataFrame]:
    """从 fact_factor 读取已有因子并透视为宽表。"""
    if not names or not symbols:
        return {}
    ph_n = ",".join("?" for _ in names)
    ph_v = ",".join("?" for _ in versions)
    frames: list[pd.DataFrame] = []
    chunk = 900
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        ph_s = ",".join("?" for _ in part)
        sql = (f"SELECT factor, symbol, trade_date, value FROM fact_factor "
               f"WHERE factor IN ({ph_n}) AND version IN ({ph_v}) AND symbol IN ({ph_s})")
        params: list = list(names) + list(versions) + list(part)
        if start:
            sql += " AND trade_date >= ?"
            params.append(start)
        if end:
            sql += " AND trade_date <= ?"
            params.append(end)
        rows = repo.query(sql, tuple(params))
        if rows:
            frames.append(pd.DataFrame([dict(r) for r in rows]))
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    out: dict[str, pd.DataFrame] = {}
    for name, sub in df.groupby("factor"):
        out[name] = sub.pivot(index="trade_date", columns="symbol", values="value").astype("float32")
    return out


def build_factor_set(repo: Repository, panel: Panel, with_v5: bool = True,
                     stored_only: bool = False) -> dict[str, pd.DataFrame]:
    """构建策略所需的完整因子集合。"""
    from .registry import by_version

    factors: dict[str, pd.DataFrame] = {}
    if not stored_only:
        factors.update(compute_all_panel_factors(panel))

    stored_names = [s.name for s in by_version("v2") + by_version("v3")]
    stored = load_stored_factors(
        repo, stored_names, ["v2", "v3"],
        list(panel.close.columns), str(panel.close.index[0]), str(panel.close.index[-1]),
    )
    for k, v in stored.items():
        if k not in factors:
            factors[k] = v.reindex(index=panel.close.index, columns=panel.close.columns)

    if with_v5:
        factors.update(compute_v5(repo, panel))
    return factors


def load_cached_factors(repo: Repository, universe: str | None,
                        start: str | None, end: str | None,
                        version: str = "research-v1", force: bool = False,
                        with_v5: bool = True) -> tuple[Panel, dict[str, pd.DataFrame], dict]:
    """加载面板与因子；命中缓存则直接复用。"""
    panel = load_panel(repo, universe=universe, start=start, end=end)
    if panel.close.empty:
        return panel, {}, {"cache": "empty"}
    key = _key(version, universe, start, end, list(panel.close.columns))
    path = _cache_dir() / f"{key}.pkl"
    if not force and _is_fresh(path):
        try:
            with path.open("rb") as f:
                payload = pickle.load(f)
            return panel, payload["factors"], {
                "cache": "hit", "file": path.name,
                "created_at": payload.get("created_at"),
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(f"因子缓存读取失败，重算：{e}")
    factors = build_factor_set(repo, panel, with_v5=with_v5)
    try:
        with path.open("wb") as f:
            pickle.dump({"factors": factors,
                         "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f,
                        protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"因子缓存写入失败：{e}")
    return panel, factors, {"cache": "miss", "file": path.name}
