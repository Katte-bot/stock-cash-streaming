"""数据源注册与选择。"""

from ..common.logging import logger
from ..config.settings import get_settings
from .akshare_src import AkShareSource
from .base import DataSource


def get_source(name: str | None = None) -> DataSource:
    """按配置返回数据源实例（默认 akshare）。"""
    name = (name or get_settings().data_source_primary).lower()
    if name == "akshare":
        return AkShareSource()
    if name == "mcp":
        from .mcp_src import McpSource

        return McpSource()
    raise ValueError(f"未知数据源: {name}")


def describe() -> str:
    return f"primary={get_settings().data_source_primary}"


__all__ = ["get_source", "describe", "DataSource", "AkShareSource", "logger"]
