"""仓储：通用 upsert / 查询 / 数据水位。"""

import sqlite3
from datetime import datetime
from typing import Any, Iterable

from ..common.logging import logger


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Repository:
    """SQLite 仓储。所有写入均为幂等 upsert。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------- 通用写入 ----------

    def upsert(self, table: str, rows: Iterable[dict[str, Any]], conflict_cols: list[str]) -> int:
        """按唯一键幂等写入。返回写入行数。"""
        rows = list(rows)
        if not rows:
            return 0
        cols = list(rows[0].keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in conflict_cols)
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
        if updates:
            sql += f" ON CONFLICT({','.join(conflict_cols)}) DO UPDATE SET {updates}"
        else:
            sql += f" ON CONFLICT({','.join(conflict_cols)}) DO NOTHING"
        self.conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
        self.conn.commit()
        return len(rows)

    # ---------- 查询 ----------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def count(self, table: str, where: str = "", params: Iterable[Any] = ()) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(self.conn.execute(sql, tuple(params)).fetchone()["n"])

    # ---------- 数据水位 ----------

    def set_watermark(self, dataset: str, last_date: str | None, status: str = "ok") -> None:
        self.conn.execute(
            """INSERT INTO ingest_watermark (dataset, last_date, last_run_at, status)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(dataset) DO UPDATE SET
                   last_date=excluded.last_date,
                   last_run_at=excluded.last_run_at,
                   status=excluded.status""",
            (dataset, last_date, _now(), status),
        )
        self.conn.commit()

    def get_watermark(self, dataset: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM ingest_watermark WHERE dataset=?", (dataset,)
        ).fetchone()

    # ---------- 任务日志 ----------

    def log_task(self, task_id: str, name: str, status: str, started_at: str,
                 ended_at: str | None = None, message: str = "") -> None:
        self.conn.execute(
            """INSERT INTO task_log (task_id, name, status, started_at, ended_at, message)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET
                   status=excluded.status, ended_at=excluded.ended_at, message=excluded.message""",
            (task_id, name, status, started_at, ended_at, message),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def open_repository(db_path: str | None = None) -> Repository:
    from .db import connect, init_schema

    conn = connect(db_path)
    init_schema(conn)
    logger.debug("SQLite 就绪")
    return Repository(conn)
