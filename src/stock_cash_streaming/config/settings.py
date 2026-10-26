"""全局配置：从系统环境变量（优先）与可选 `.env` 读取。

配置约定见 docs/software-architecture.md 附录B / §12.3：
- LLM 走系统环境变量 OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL。
- 其余通过同名环境变量覆盖。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 存储 ----
    db_path: str = "data/stock_cash_streaming.sqlite3"

    # ---- 服务 ----
    host: str = "0.0.0.0"
    port: int = 8000

    # ---- 目标板块（逗号分隔，支持中英文逗号） ----
    target_sectors: str = "电网设备,天然气"

    # ---- 数据源 ----
    data_source_primary: str = "akshare"  # akshare | mcp
    mcp_endpoint: str = ""  # 可选：MCP 网关 HTTP 端点（为空则禁用）
    request_sleep: float = 0.5  # 单次请求间隔（秒，简单限速）
    max_retries: int = 3  # 单次请求最大重试次数

    # ---- LLM（AI 日报）----
    openai_api_key: str = ""
    openai_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"

    # ---- 网页搜索（AI 日报消息面参考，不参与因子/信号/回测）----
    search_enabled: bool = True
    search_count: int = 5  # 每个板块检索条数（1~50）
    search_days: int = 7  # 只取最近 N 天的网页
    search_timeout: float = 20.0

    @property
    def sectors(self) -> list[str]:
        """解析目标板块列表。"""
        normalized = self.target_sectors.replace("，", ",")
        return [s.strip() for s in normalized.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
