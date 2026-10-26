"""盘中实时快照与板块内实时评分。"""

from datetime import datetime

import pandas as pd

from ..common.logging import logger
from ..datasource.base import DataSource
from ..ingest.universe import load_seed_members


def intraday_snapshot(ds: DataSource) -> dict:
    """拉取腾讯全市场快照，筛出目标板块成分股并做板块内实时排名。"""
    seeds = load_seed_members()
    sym2sector = {s: sec for sec, items in seeds.items() for s, _ in items}

    try:
        spot = ds.spot()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"盘中快照失败: {e}")
        return {"ts": datetime.now().strftime("%H:%M:%S"), "rows": [], "error": str(e)}

    sub = spot[spot["symbol"].isin(sym2sector)].copy()
    sub["sector"] = sub["symbol"].map(sym2sector)
    rows = []
    for sec, g in sub.groupby("sector"):
        g = g.copy()
        g["rank_pct"] = g["main_net"].rank(ascending=False, pct=True)
        for r in g.itertuples():
            rows.append({
                "sector": sec,
                "symbol": r.symbol,
                "name": r.name,
                "last": None if pd.isna(r.last) else float(r.last),
                "pct_chg": None if pd.isna(r.pct_chg) else float(r.pct_chg),
                "main_net": None if pd.isna(r.main_net) else float(r.main_net) * 1e4,  # 万元→元
                "rank_pct": None if pd.isna(r.rank_pct) else round(float(r.rank_pct), 3),
            })
    rows.sort(key=lambda x: (x["sector"], -(x["main_net"] or 0)))
    return {"ts": datetime.now().strftime("%H:%M:%S"), "rows": rows}
