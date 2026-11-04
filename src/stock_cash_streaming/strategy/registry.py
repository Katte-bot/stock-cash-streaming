"""策略注册表：策略元数据、默认参数与评分构造函数。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import pandas as pd

from ..features.panel import Panel
from ..storage.repository import Repository


@dataclass
class StrategySpec:
    id: str
    name: str
    category: str            # fundamental / price / event / meta / baseline
    description: str
    factors: list[str] = field(default_factory=list)
    defaults: dict = field(default_factory=dict)
    builder: Callable[[Panel, dict[str, pd.DataFrame], dict], pd.DataFrame] | None = None


_REGISTRY: dict[str, StrategySpec] = {}


def register(spec: StrategySpec) -> StrategySpec:
    if spec.id in _REGISTRY:
        raise ValueError(f"策略重复注册：{spec.id}")
    _REGISTRY[spec.id] = spec
    return spec


def get(strategy_id: str) -> StrategySpec | None:
    return _REGISTRY.get(strategy_id)


def list_specs() -> list[StrategySpec]:
    return sorted(_REGISTRY.values(), key=lambda s: (s.category, s.id))


def sync_to_db(repo: Repository) -> int:
    """把注册表写入 dim_strategy（幂等；已存在时保留 status/created_at）。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for s in list_specs():
        rows.append((
            s.id, s.name, s.category, s.description,
            json.dumps({"factors": s.factors, "defaults": s.defaults}, ensure_ascii=False),
            "production" if s.id == "defensive" else "research", now, now,
        ))
    repo.conn.executemany(
        """INSERT INTO dim_strategy
               (strategy_id, name, category, description, spec_json, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(strategy_id) DO UPDATE SET
               name=excluded.name, category=excluded.category,
               description=excluded.description, spec_json=excluded.spec_json,
               updated_at=excluded.updated_at""",
        rows,
    )
    repo.conn.commit()
    return len(rows)


def ensure_registered() -> None:
    """导入策略库以完成注册（延迟导入避免循环依赖）。"""
    from . import library  # noqa: F401
