"""历史初始化：板块成分 → 个股 → 行情 + 资金流（快照）入库。

数据源现状（受限环境）：
- 行情：腾讯日线（全历史）✅
- 个股资金流：腾讯全市场快照 `spot()`（含主力净流入）✅
- 板块资金流：同花顺 `sector_fund_flow()` ✅
- 板块成分：接口不可用 → 回退 config/seed_members.csv

单位说明：腾讯快照主力净流入为“万元”；同花顺板块净额为“亿元”。均按原始值入库，并在 src 中标注来源。
"""

from datetime import datetime

import pandas as pd

from ..common.logging import logger
from ..datasource.akshare_src import infer_market
from ..datasource.base import DataSource
from ..storage.repository import Repository
from .universe import load_sectors, load_seed_members


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _get_members(ds: DataSource, name: str, stype: str) -> pd.DataFrame:
    """取板块成分：优先数据源接口，失败回退手工种子。"""
    try:
        m = ds.board_members(name, stype)
        if not m.empty:
            return m
    except Exception as e:  # noqa: BLE001
        logger.warning(f"成分接口失败 [{name}]: {e}")
    seed = load_seed_members().get(name, [])
    if seed:
        logger.warning(f"回退手工种子成分 [{name}]：{len(seed)} 只")
        return pd.DataFrame(seed, columns=["symbol", "name"])
    return pd.DataFrame(columns=["symbol", "name"])


def init_history(ds: DataSource, repo: Repository, start_date: str, end_date: str, limit: int | None = None) -> dict:
    """跑一遍历史初始化，返回统计。"""
    now, today = _now(), _today()
    stats = {"sectors": 0, "members": 0, "price_ok": 0, "price_fail": 0, "flow_ok": 0, "sector_flow": 0}
    members_all: dict[str, list[str]] = {}

    for sec in load_sectors():
        name, stype = sec["name"], sec["type"]
        logger.info(f"=== 板块 [{name}] ({stype}) ===")
        members = _get_members(ds, name, stype)
        if members.empty:
            logger.warning(f"板块 [{name}] 无成分，跳过")
            continue
        stats["sectors"] += 1

        repo.upsert(
            "dim_sector",
            [{"sector_code": name, "sector_name": name, "sector_type": stype, "updated_at": now}],
            ["sector_code"],
        )
        repo.upsert(
            "dim_sector_member",
            [{"sector_code": name, "symbol": s, "in_date": today, "out_date": None} for s in members["symbol"]],
            ["sector_code", "symbol", "in_date"],
        )
        repo.upsert(
            "dim_stock",
            [{"symbol": r.symbol, "name": r.name, "market": infer_market(r.symbol), "updated_at": now}
             for r in members.itertuples()],
            ["symbol"],
        )

        symbols = list(members["symbol"])
        if limit:
            symbols = symbols[:limit]
        members_all[name] = symbols
        stats["members"] += len(symbols)
        logger.info(f"成分股 {len(symbols)} 只，拉取日线…")

        for i, sym in enumerate(symbols, 1):
            try:
                prices = ds.daily_prices(sym, start_date, end_date)
                if not prices.empty:
                    rows = prices.to_dict("records")
                    for r in rows:
                        r.update({"symbol": sym, "src": ds.name, "fetched_at": now})
                    repo.upsert("fact_quote", rows, ["symbol", "trade_date"])
                    stats["price_ok"] += 1
                else:
                    stats["price_fail"] += 1
            except Exception as e:  # noqa: BLE001
                stats["price_fail"] += 1
                logger.warning(f"行情失败 {sym}: {e}")
            if i % 5 == 0:
                logger.info(f"  进度 {i}/{len(symbols)}")

    # ---- 个股资金流：腾讯全市场快照（一次调用） ----
    all_symbols = {s for lst in members_all.values() for s in lst}
    if all_symbols:
        try:
            spot = ds.spot()
            if not spot.empty:
                sub = spot[spot["symbol"].isin(all_symbols)]
                rows = [
                    {
                        "symbol": r.symbol,
                        "trade_date": today,
                        # 腾讯快照主力净流入为“万元”，统一换算为“元”
                        "main_net": None if getattr(r, "main_net", None) is None else float(r.main_net) * 1e4,
                        "close": getattr(r, "last", None),
                        "pct_chg": getattr(r, "pct_chg", None),
                        "src": f"{ds.name}:tx_spot",
                        "fetched_at": now,
                    }
                    for r in sub.itertuples()
                ]
                repo.upsert("fact_stock_flow", rows, ["symbol", "trade_date"])
                stats["flow_ok"] = len(rows)
                logger.info(f"个股资金流快照入库 {len(rows)} 行")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"个股资金流快照失败: {e}")

    # ---- 板块资金流：同花顺（行业/概念） ----
    for sec in load_sectors():
        name, stype = sec["name"], sec["type"]
        cate = "concept" if stype == "concept" else "industry"
        try:
            sf = ds.sector_fund_flow(cate=cate, period="即时")
            if not sf.empty and name in set(sf["行业"]):
                row = sf[sf["行业"] == name].iloc[0]
                net = row.get("净额")
                repo.upsert(
                    "fact_sector_flow",
                    [{
                        "sector_code": name,
                        "trade_date": today,
                        # 同花顺板块净额为“亿元”，统一换算为“元”
                        "main_net": None if net is None else float(net) * 1e8,
                        "src": f"{ds.name}:ths",
                        "fetched_at": now,
                    }],
                    ["sector_code", "trade_date"],
                )
                stats["sector_flow"] += 1
                logger.info(f"板块资金流 [{name}] 净额={row.get('净额')} 亿")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"板块资金流失败 {name}: {e}")

    repo.set_watermark("fact_quote", end_date)
    repo.set_watermark("fact_stock_flow", today)
    repo.set_watermark("fact_sector_flow", today)
    return stats
