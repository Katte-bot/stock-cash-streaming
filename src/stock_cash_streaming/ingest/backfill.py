"""资金流历史回补：按成分股逐只拉取新浪逐日四档资金流（带限速）。"""

import time
from datetime import datetime

from ..common.logging import logger
from ..datasource.sina_src import sina_stock_fund_flow
from ..storage.repository import Repository


def backfill_fund_flow(repo: Repository, sleep: float = 0.4, max_symbols: int | None = None) -> dict:
    rows = repo.query("SELECT DISTINCT symbol FROM dim_sector_member")
    symbols = [r["symbol"] for r in rows]
    if max_symbols:
        symbols = symbols[:max_symbols]

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")
    stats = {"symbols": len(symbols), "ok": 0, "fail": 0, "rows": 0}

    for i, sym in enumerate(symbols, 1):
        df = None
        for attempt in range(1, 3):
            try:
                df = sina_stock_fund_flow(sym)
                if df is not None and not df.empty:
                    break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"资金流回补失败 {sym} 第{attempt}次: {e}")
            time.sleep(1.0)
        if df is None or df.empty:
            stats["fail"] += 1
            logger.warning(f"[{i}/{len(symbols)}] {sym} 无数据")
            continue

        recs = df.to_dict("records")
        for r in recs:
            r.update({"symbol": sym, "src": "sina:lscjfb", "fetched_at": now})
        n = repo.upsert("fact_stock_flow", recs, ["symbol", "trade_date"])
        stats["ok"] += 1
        stats["rows"] += n
        logger.info(f"[{i}/{len(symbols)}] {sym}: {n} 行")
        time.sleep(sleep)

    repo.set_watermark("fact_stock_flow", today)
    logger.info(f"资金流回补完成: {stats}")
    return stats
