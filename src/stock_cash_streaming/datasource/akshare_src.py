"""AkShare 数据源适配器（主源，免 Key）。

说明：AkShare 通过公网访问东财/新浪等站点，运行环境需可访问这些站点。
若网络受限，请改用代理或 McpAdapter（见 registry）。
"""

import socket
import time
from functools import lru_cache

import akshare as ak
import pandas as pd

from ..common.logging import logger
from ..config.settings import get_settings
from .base import DataSourceError

# 兜底超时，避免无超时的请求长时间挂起
socket.setdefaulttimeout(20)


def _retry(fn, what: str):
    settings = get_settings()
    last_err = None
    for i in range(settings.max_retries):
        try:
            df = fn()
            time.sleep(settings.request_sleep)  # 简单限速
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(f"AkShare 拉取失败({what}) 第{i + 1}/{settings.max_retries}次: {type(e).__name__}: {e}")
            time.sleep(1.0 * (i + 1))
    raise DataSourceError(f"AkShare 拉取失败({what}): {last_err}")


def infer_market(symbol: str) -> str:
    """由 6 位代码推断交易所：sh/sz/bj。

    深市 ETF/LOF/基金（15/16/18 开头）必须归 sz，否则腾讯/新浪接口取不到数据。
    """
    if symbol.startswith(("6", "9")):
        return "sh"
    if symbol.startswith(("0", "3", "2", "1")):
        return "sz"
    if symbol.startswith(("4", "8")):
        return "bj"
    return "sh"


def _strip_code(x) -> str:
    return str(x).zfill(6)


def _baostock_daily(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Baostock 日线（第三备源，后复权）。"""
    import baostock as bs

    prefix = {"sh": "sh.", "sz": "sz.", "bj": "bj."}.get(infer_market(symbol), "sh.")

    def fmt(d: str) -> str:
        return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 and "-" not in d else d

    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            f"{prefix}{symbol}",
            "date,open,high,low,close,volume,amount,turn,pctChg",
            start_date=fmt(start_date),
            end_date=fmt(end_date),
            frequency="d",
            adjustflag="1",  # 1=后复权（与 hfq 一致）
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=rs.fields)
        for c in ("open", "high", "low", "close", "volume", "amount", "turn", "pctChg"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
    finally:
        bs.logout()


@lru_cache(maxsize=8)
def tx_index_daily(symbol: str = "sh000300") -> pd.DataFrame:
    """腾讯指数日线（用于市场择时），返回 trade_date(str), close。"""
    df = ak.stock_zh_index_daily_tx(symbol=symbol).rename(columns={"date": "trade_date"})
    df["trade_date"] = df["trade_date"].astype(str)
    return df


@lru_cache(maxsize=8)
def baostock_index(symbol: str = "sh.000300", start_date: str = "2018-01-01",
                   end_date: str = "2026-12-31") -> pd.DataFrame:
    """Baostock 指数日线（不复权），返回 trade_date, close。"""
    import baostock as bs

    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            symbol, "date,close", start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="3",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=rs.fields).rename(columns={"date": "trade_date"})
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        return df
    finally:
        bs.logout()


class AkShareSource:
    name = "akshare"

    # ---------- 板块成分 ----------

    def board_members(self, board_name: str, board_type: str) -> pd.DataFrame:
        """板块成分：新浪映射优先（本环境可达），失败回退东财。"""
        from .sina_src import sector_label, sina_board_members

        label = sector_label(board_name)
        if label:
            try:
                df = sina_board_members(label)
                if not df.empty:
                    return df
            except Exception as e:  # noqa: BLE001
                logger.warning(f"新浪成分失败 [{board_name}/{label}]: {e}")

        try:
            if board_type == "concept":
                raw = _retry(lambda: ak.stock_board_concept_cons_em(symbol=board_name), f"concept_cons:{board_name}")
            else:
                raw = _retry(lambda: ak.stock_board_industry_cons_em(symbol=board_name), f"industry_cons:{board_name}")
            code_col = "代码" if "代码" in raw.columns else "code"
            name_col = "名称" if "名称" in raw.columns else "name"
            df = pd.DataFrame({"symbol": raw[code_col].map(_strip_code), "name": raw[name_col].astype(str)})
            return df.drop_duplicates("symbol").reset_index(drop=True)
        except Exception as e:  # noqa: BLE001
            raise DataSourceError(f"板块成分获取失败 [{board_name}]: {e}") from e

    # ---------- 个股行情 ----------

    def daily_prices(self, symbol: str, start_date: str, end_date: str, adjust: str = "hfq") -> pd.DataFrame:
        """日线行情：腾讯 → 新浪 → Baostock 三级回退。

        默认使用**后复权(hfq)**：相比前复权，能避免大额分红导致的负价问题，且收益连续。
        """
        tx_symbol = f"{infer_market(symbol)}{symbol}"
        try:
            raw = _retry(
                lambda: ak.stock_zh_a_hist_tx(
                    symbol=tx_symbol, start_date=start_date, end_date=end_date, adjust=adjust
                ),
                f"tx_hist:{symbol}",
            )
        except Exception:  # noqa: BLE001
            try:
                raw = _retry(
                    lambda: ak.stock_zh_a_daily(
                        symbol=tx_symbol, start_date=start_date, end_date=end_date, adjust=adjust
                    ),
                    f"sina_hist:{symbol}",
                )
            except Exception:  # noqa: BLE001
                raw = _baostock_daily(symbol, start_date, end_date)
        if raw is None or raw.empty:
            return pd.DataFrame()
        raw = raw.rename(columns={"date": "trade_date", "turn": "turnover", "pctChg": "pct_chg"})
        raw = raw.sort_values("trade_date")
        if "pct_chg" not in raw.columns or raw["pct_chg"].isna().all():
            raw["pct_chg"] = (raw["close"].pct_change() * 100).round(4)
        df = pd.DataFrame(
            {
                "trade_date": raw["trade_date"].astype(str),
                "open": raw["open"],
                "high": raw["high"],
                "low": raw["low"],
                "close": raw["close"],
                "volume": raw.get("volume"),
                "amount": raw.get("amount"),
                "pct_chg": raw["pct_chg"],
                "turnover": raw.get("turnover"),
            }
        )
        # 过滤异常价格（前复权偶发负价/零价）
        df = df[df["close"] > 0]
        return df.reset_index(drop=True)

    # ---------- 个股资金流 ----------

    def stock_fund_flow(self, symbol: str) -> pd.DataFrame:
        """个股资金流：新浪逐日四档历史优先（本环境可达），失败回退东财。"""
        from .sina_src import sina_stock_fund_flow

        try:
            df = sina_stock_fund_flow(symbol)
            if not df.empty:
                return df
        except Exception as e:  # noqa: BLE001
            logger.warning(f"新浪资金流失败 {symbol}: {e}")

        raw = _retry(
            lambda: ak.stock_individual_fund_flow(stock=symbol, market=infer_market(symbol)),
            f"stock_flow:{symbol}",
        )
        if raw is None or raw.empty:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "trade_date": raw["日期"].astype(str),
                "close": raw.get("收盘价"),
                "pct_chg": raw.get("涨跌幅"),
                "main_net": raw.get("主力净流入-净额"),
                "main_ratio": raw.get("主力净流入-净占比"),
                "xl_net": raw.get("超大单净流入-净额"),
                "xl_ratio": raw.get("超大单净流入-净占比"),
                "l_net": raw.get("大单净流入-净额"),
                "l_ratio": raw.get("大单净流入-净占比"),
                "m_net": raw.get("中单净流入-净额"),
                "m_ratio": raw.get("中单净流入-净占比"),
                "s_net": raw.get("小单净流入-净额"),
                "s_ratio": raw.get("小单净流入-净占比"),
            }
        ).reset_index(drop=True)

    # ---------- 全市场实时快照（腾讯源，含主力净流入） ----------

    def spot(self) -> pd.DataFrame:
        """全 A 实时快照（腾讯 `stock_zh_a_spot_tx`），含主力净流入。

        返回规范化列：symbol,name,last,pct_chg,turnover,vol_ratio,
        main_net,main_in,main_out,float_mv,total_mv,pe_ttm
        """
        raw = _retry(lambda: ak.stock_zh_a_spot_tx(), "tx_spot")
        if raw is None or raw.empty:
            return pd.DataFrame()
        df = pd.DataFrame(
            {
                "symbol": raw["code"].astype(str).str[-6:],
                "name": raw.get("name"),
                "last": raw.get("zxj"),
                "pct_chg": raw.get("zdf"),
                "turnover": raw.get("hsl"),
                "vol_ratio": raw.get("lb"),
                "main_net": raw.get("zljlr"),
                "main_in": raw.get("zllr"),
                "main_out": raw.get("zllc"),
                "float_mv": raw.get("ltsz"),
                "total_mv": raw.get("zsz"),
                "pe_ttm": raw.get("pe_ttm"),
            }
        )
        return df.reset_index(drop=True)

    # ---------- 板块资金流（同花顺源） ----------

    def sector_fund_flow(self, cate: str = "industry", period: str = "即时") -> pd.DataFrame:
        """板块资金流快照（同花顺）。

        cate: industry(行业) / concept(概念)；period: 即时 / 3日排行 / 5日排行 / 10日排行 / 20日排行。
        返回原始列：序号,行业,行业指数,行业-涨跌幅,流入资金,流出资金,净额,公司家数,领涨股,...
        """
        fn = ak.stock_fund_flow_concept if cate == "concept" else ak.stock_fund_flow_industry
        raw = _retry(lambda: fn(symbol=period), f"ths_sector_flow:{cate}:{period}")
        if raw is None or raw.empty:
            return pd.DataFrame()
        return raw.reset_index(drop=True)
