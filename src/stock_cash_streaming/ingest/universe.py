"""目标板块与成分股（config/sectors.yaml + 手工种子）。"""

import csv
from pathlib import Path

import yaml

# 项目根目录：src/stock_cash_streaming/ingest/universe.py -> parents[3]
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECTORS_PATH = PROJECT_ROOT / "config" / "sectors.yaml"
SEED_MEMBERS_PATH = PROJECT_ROOT / "config" / "seed_members.csv"


def load_sectors() -> list[dict]:
    """返回 [{'name': '电网设备', 'type': 'industry'}, ...]。"""
    cfg = yaml.safe_load(SECTORS_PATH.read_text(encoding="utf-8"))
    return cfg.get("sectors", [])


def load_seed_members() -> dict[str, list[tuple[str, str]]]:
    """读取手工种子成分，返回 {sector_code: [(symbol, name), ...]}。

    用于板块成分接口不可用时的回退（受限环境）。
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for r in csv.DictReader(SEED_MEMBERS_PATH.read_text(encoding="utf-8").splitlines()):
        out.setdefault(r["sector_code"], []).append((r["symbol"], r["name"]))
    return out

