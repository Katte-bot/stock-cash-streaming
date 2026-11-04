"""SQLite 连接与建表。"""

import sqlite3
from pathlib import Path

from ..config.settings import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """建立连接（自动创建父目录，开启 WAL）。"""
    path = Path(db_path or get_settings().db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """执行 DDL（幂等），并补齐旧库缺失的列。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(dim_stock)")}
    for col in ("ipo_date", "out_date"):
        if col not in cols:
            conn.execute(f"ALTER TABLE dim_stock ADD COLUMN {col} TEXT")

    ucols = {r["name"] for r in conn.execute("PRAGMA table_info(dim_universe)")}
    for col, ddl in (
        ("is_custom", "INTEGER NOT NULL DEFAULT 0"),
        ("daily_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("members_updated_at", "TEXT"),
        ("strategy_id", "TEXT NOT NULL DEFAULT 'defensive'"),
    ):
        if col not in ucols:
            conn.execute(f"ALTER TABLE dim_universe ADD COLUMN {col} {ddl}")

    # C0 是全局流水线维护的内置池，落一行便于统一展示与读写状态。
    conn.execute(
        """INSERT INTO dim_universe
               (universe_id, name, description, updated_at, is_custom, daily_enabled)
           VALUES ('C0', 'C0 当前板块成分', '电网设备 + 天然气',
                   datetime('now','localtime'), 0, 1)
           ON CONFLICT(universe_id) DO NOTHING"""
    )

    # A0：全 A 研究池（不参与每日池任务，仅用于研究回测）
    conn.execute(
        """INSERT INTO dim_universe
               (universe_id, name, description, updated_at, is_custom, daily_enabled, strategy_id)
           VALUES ('A0', 'A0 全 A 研究池', '全 A 可交易股票，按流动性动态过滤',
                   datetime('now','localtime'), 0, 0, 'value_quality')
           ON CONFLICT(universe_id) DO NOTHING"""
    )

    # 回测记录补充指标快照列（旧库）
    bcols = {r["name"] for r in conn.execute("PRAGMA table_info(backtest_run)")}
    if bcols and "metrics_json" not in bcols:
        conn.execute("ALTER TABLE backtest_run ADD COLUMN metrics_json TEXT")

    # 一次性迁移（user_version=2）：所有股票池默认纳入每日池任务，
    # 保证“数据 + 因子 + 回测 + 组合/跟踪”每天都跟随最新行情刷新。
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        conn.execute("UPDATE dim_universe SET daily_enabled=1 WHERE universe_id <> 'A0'")
        conn.execute("PRAGMA user_version=2")
    # A0 是研究池，永不参与每日池任务（修复历史库里可能被误开的情况）
    conn.execute("UPDATE dim_universe SET daily_enabled=0 WHERE universe_id='A0'")

    # 模拟持仓：账户表增加折算比例列（旧库补列）
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(fact_paper_account)")}
    if pcols and "price_scale" not in pcols:
        conn.execute("ALTER TABLE fact_paper_account ADD COLUMN price_scale TEXT")
