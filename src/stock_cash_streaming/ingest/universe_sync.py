"""为指定股票（如 C1 用户池）补拉行情与资金流历史。

与 `ingest/history.py` 的区别：不依赖板块成分，直接按代码清单取数；
用于 C1/C2/C3 等自定义股票池的初次建档与增量补齐（幂等）。
"""

from datetime import datetime

from ..common.logging import logger
from ..datasource.registry import get_source
from ..datasource.sina_src import sina_stock_fund_flow
from ..storage.repository import Repository
from .incremental import _fill_pct_chg


def sync_symbols(repo: Repository, symbols: list[str], start_date: str = "20180101",
                 end_date: str | None = None, with_flow: bool = True,
                 sleep: float = 0.4) -> dict:
    """逐只补拉日线（腾讯→新浪→Baostock 回退）与新浪四档资金流。"""
    ds = get_source()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    end_date = end_date or datetime.now().strftime("%Y%m%d")
    stats = {"symbols": len(symbols), "price_ok": 0, "price_fail": 0,
             "flow_ok": 0, "flow_fail": 0}
    for i, sym in enumerate(symbols, 1):
        try:
            df = ds.daily_prices(sym, start_date, end_date)
            if df.empty:
                stats["price_fail"] += 1
                logger.warning(f"{sym} 无行情数据")
            else:
                _fill_pct_chg(repo, sym, df)
                rows = df.to_dict("records")
                for r in rows:
                    r.update({"symbol": sym, "src": f"{ds.name}:hfq", "fetched_at": now})
                repo.upsert("fact_quote", rows, ["symbol", "trade_date"])
                stats["price_ok"] += 1
        except Exception as e:  # noqa: BLE001
            stats["price_fail"] += 1
            logger.warning(f"{sym} 行情失败: {e}")

        if with_flow:
            try:
                flow = sina_stock_fund_flow(sym)
                if flow is not None and not flow.empty:
                    recs = flow.to_dict("records")
                    for r in recs:
                        r.update({"symbol": sym, "src": "sina:lscjfb", "fetched_at": now})
                    repo.upsert("fact_stock_flow", recs, ["symbol", "trade_date"])
                    stats["flow_ok"] += 1
                else:
                    stats["flow_fail"] += 1
            except Exception as e:  # noqa: BLE001
                stats["flow_fail"] += 1
                logger.warning(f"{sym} 资金流失败: {e}")

        if i % 5 == 0:
            logger.info(f"  自定义池补数进度 {i}/{len(symbols)}")
        if sleep:
            import time
            time.sleep(sleep)
    logger.info(f"自定义池补数完成: {stats}")
    return stats
