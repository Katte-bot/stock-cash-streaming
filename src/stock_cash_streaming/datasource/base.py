"""数据源抽象接口。

所有适配器统一返回**规范化英文列名**，使上层与具体数据源解耦：
- board_members        -> symbol, name
- daily_prices         -> trade_date, open, high, low, close, volume, amount, pct_chg, turnover
- stock_fund_flow      -> trade_date, close, pct_chg, main_net, main_ratio,
                          xl_net, xl_ratio, l_net, l_ratio, m_net, m_ratio, s_net, s_ratio
- sector_fund_flow_hist-> trade_date, main_net, main_ratio,
                          xl_net, xl_ratio, l_net, l_ratio, m_net, m_ratio, s_net, s_ratio
"""

from typing import Protocol

import pandas as pd


class DataSourceError(RuntimeError):
    """数据源异常。"""


class DataSource(Protocol):
    name: str

    def board_members(self, board_name: str, board_type: str) -> pd.DataFrame: ...

    def daily_prices(self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame: ...

    def stock_fund_flow(self, symbol: str) -> pd.DataFrame: ...

    def sector_fund_flow_hist(self, sector_name: str) -> pd.DataFrame: ...
