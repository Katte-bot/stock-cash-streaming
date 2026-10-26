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
