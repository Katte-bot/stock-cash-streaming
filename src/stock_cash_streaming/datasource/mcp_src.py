"""MCP 网关数据源适配器（备援/校验）。

本适配器假设 `mcp_stock-data` 网关以 HTTP 方式暴露：POST {endpoint}/{tool}
JSON body 为工具参数，返回文本/JSON。若未配置 `MCP_ENDPOINT` 则不可用。

注意：当前环境未发现可直连端点；本类保留为架构预留，待端点明确后启用。
"""

import json

import pandas as pd
import requests

from ..config.settings import get_settings
from .base import DataSourceError


class McpSource:
    name = "mcp"

    def __init__(self):
        self.endpoint = get_settings().mcp_endpoint.rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.endpoint)

    def _call(self, tool: str, args: dict) -> dict:
        if not self.available:
            raise DataSourceError("MCP 端点未配置（MCP_ENDPOINT 为空）")
        resp = requests.post(f"{self.endpoint}/{tool}", json=args, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def board_members(self, board_name: str, board_type: str) -> pd.DataFrame:
        data = self._call("stock_board_cons", {"board_name": board_name, "board_type": board_type, "limit": 500})
        rows = data.get("rows", data if isinstance(data, list) else [])
        df = pd.DataFrame(rows)
        return pd.DataFrame({"symbol": df.get("代码"), "name": df.get("名称")}) if not df.empty else pd.DataFrame()

    def daily_prices(self, symbol: str, start_date: str, end_date: str, adjust: str = "qfq") -> pd.DataFrame:
        raise DataSourceError("McpAdapter.daily_prices 尚未实现（端点契约待定）")

    def stock_fund_flow(self, symbol: str) -> pd.DataFrame:
        raise DataSourceError("McpAdapter.stock_fund_flow 尚未实现（端点契约待定）")

    def sector_fund_flow_hist(self, sector_name: str) -> pd.DataFrame:
        raise DataSourceError("McpAdapter.sector_fund_flow_hist 尚未实现（端点契约待定）")


__all__ = ["McpSource"]
