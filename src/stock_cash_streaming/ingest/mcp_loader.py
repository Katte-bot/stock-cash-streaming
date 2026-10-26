"""MCP 网关种子数据加载。

受限环境下的聚合方案：由 Agent 通过 `mcp_stock-data` 抓取资金流，
落成原始 CSV（data/raw/mcp/*.csv），本模块解析后幂等入库。
板块成分因网关熔断暂不可得，改用 config/seed_members.csv 手工种子。
"""

import csv
import io
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..common.logging import logger
from ..datasource.akshare_src import infer_market
from ..storage.repository import Repository
from .universe import SECTORS_PATH, load_sectors

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SEED_MEMBERS = PROJECT_ROOT / "config" / "seed_members.csv"
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "mcp"

# MCP 资金流列（中文） -> 本系统 schema 列（英文）
COL_MAP = {
    "股票代码": "symbol",
    "日期": "trade_date",
    "主力净流入": "main_net",
    "主力净流入占比": "main_ratio",
    "超大单净流入": "xl_net",
    "超大单流入净占比": "xl_ratio",
    "大单净流入": "l_net",
    "大单流入净占比": "l_ratio",
    "中单净流入": "m_net",
    "中单流入净占比": "m_ratio",
    "小单净流入": "s_net",
    "小单流入净占比": "s_ratio",
    "收盘价": "close",
    "涨跌幅": "pct_chg",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_seed_members(repo: Repository) -> int:
    """载入板块/成分/股票维度（来自 config/seed_members.csv + sectors.yaml）。"""
    now = _now()
    # 板块维度
    repo.upsert(
        "dim_sector",
        [{"sector_code": s["name"], "sector_name": s["name"], "sector_type": s["type"], "updated_at": now}
         for s in load_sectors()],
        ["sector_code"],
    )
    rows = list(csv.DictReader(SEED_MEMBERS.read_text(encoding="utf-8").splitlines()))
    repo.upsert(
        "dim_stock",
        [{"symbol": r["symbol"], "name": r["name"], "market": infer_market(r["symbol"]), "updated_at": now} for r in rows],
        ["symbol"],
    )
    repo.upsert(
        "dim_sector_member",
        [{"sector_code": r["sector_code"], "symbol": r["symbol"], "in_date": _today(), "out_date": None} for r in rows],
        ["sector_code", "symbol", "in_date"],
    )
    logger.info(f"种子成分 {len(rows)} 条")
    return len(rows)


def load_fund_flow(repo: Repository, path: Path) -> int:
    """解析一个 MCP 资金流 CSV 并入库（跳过 # 注释行）。"""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]
    df = pd.read_csv(io.StringIO("\n".join(lines)), dtype={"股票代码": str})
    df = df.rename(columns=COL_MAP)
    keep = [c for c in COL_MAP.values() if c in df.columns]
    df = df[keep]
    now = _now()
    rows = df.to_dict("records")
    for r in rows:
        r["symbol"] = str(r["symbol"]).zfill(6)
        r["src"] = "mcp"
        r["fetched_at"] = now
    n = repo.upsert("fact_stock_flow", rows, ["symbol", "trade_date"])
    logger.info(f"{path.name}: 入库 {n} 行")
    return n


def load_seed(repo: Repository) -> dict:
    """载入全部种子数据，返回统计。"""
    members = load_seed_members(repo)
    total = 0
    for path in sorted(RAW_DIR.glob("fundflow*.csv")):
        total += load_fund_flow(repo, path)
    repo.set_watermark("fact_stock_flow", _today(), status="ok(seed)")
    repo.set_watermark("dim_sector_member", _today(), status="ok(seed)")
    return {"members": members, "fund_flow_rows": total}
