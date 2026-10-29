"""自定义股票池管理：代码规范化、名称解析、池/成员 CRUD 与状态。

设计约定：
- 内置池 C0/C1/C2/C3 的成员与名称只读；自定义池（U1、U2…）可编辑。
- 自定义池成员来源为 `source='manual'`；名称优先取 `dim_stock`，缺失时用腾讯快照补齐。
- 不在此模块执行行情/因子/回测，重活由 `pipeline/pool_jobs.py` 负责。
"""

from __future__ import annotations

import re
from datetime import datetime

import pandas as pd

from ..backtest.metrics import compute_metrics
from ..common.logging import logger
from ..datasource.akshare_src import infer_market
from ..datasource.registry import get_source
from ..storage.repository import Repository
from ..strategy.signal import compute_signals
from .universe import C0, is_etf, universe_members, universe_symbols

BASE_STRATEGY_NAME = "defensive_top12_monthly"
_SYMBOL_RE = re.compile(r"\d{1,6}")


class PoolError(Exception):
    """股票池业务异常基类。"""


class PoolNotFound(PoolError):
    pass


class PoolReadonly(PoolError):
    pass


class PoolValidationError(PoolError):
    pass


def backtest_strategy_name(pool_id: str) -> str:
    """回测落库策略名：C0 也带 _C0 后缀（与 /api/overview 一致）。"""
    return f"{BASE_STRATEGY_NAME}_{pool_id}"


def portfolio_strategy_name(pool_id: str) -> str:
    """目标组合/跟踪策略名：C0 保持生产策略名，其余追加池后缀。"""
    return BASE_STRATEGY_NAME if pool_id == C0 else f"{BASE_STRATEGY_NAME}_{pool_id}"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def split_codes(raw: str | list[str] | tuple[str, ...]) -> list[str]:
    """把用户输入拆成代码 token（保留非法 token 供校验提示）。"""
    if isinstance(raw, str):
        parts = re.split(r"[\s,，;；、]+", raw.strip())
    else:
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.extend(re.split(r"[\s,，;；、]+", item.strip()))
    return [p for p in (x.strip() for x in parts) if p]


def normalize_codes(raw: str | list[str] | tuple[str, ...]) -> list[str]:
    """返回去重后的 6 位代码；非法 token 直接忽略。"""
    out: list[str] = []
    for token in split_codes(raw):
        if not _SYMBOL_RE.fullmatch(token):
            continue
        code = token.zfill(6)
        if code not in out:
            out.append(code)
    return out


def _clean_name(name: str | None, field: str = "名称") -> str:
    value = (name or "").strip()
    if not value:
        raise PoolValidationError(f"{field}不能为空")
    if len(value) > 40:
        raise PoolValidationError(f"{field}最长 40 个字符")
    return value


def _ensure_name_unique(repo: Repository, name: str, exclude_id: str | None = None) -> None:
    rows = repo.query("SELECT universe_id FROM dim_universe WHERE name=?", (name,))
    if any(r["universe_id"] != exclude_id for r in rows):
        raise PoolValidationError(f"股票池名称已存在：{name}")


def _spot_names() -> dict[str, str]:
    """腾讯全市场快照的 code->name；失败返回空字典。"""
    try:
        df = get_source().spot()
        if df is None or df.empty or "symbol" not in df.columns:
            return {}
        out: dict[str, str] = {}
        for r in df.itertuples():
            name = getattr(r, "name", None)
            if isinstance(name, str) and name.strip():
                out[str(r.symbol).zfill(6)] = name.strip()
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"股票名称快照失败：{e}")
        return {}


def resolve_stock_names(repo: Repository, codes: list[str]) -> list[dict]:
    """解析代码对应的名称，不落库。

    返回项：symbol/name/market/is_etf/resolved/valid。
    非法代码（非 1~6 位数字）会被标记 valid=False。
    """
    items: list[dict] = []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        token = str(raw).strip()
        if not _SYMBOL_RE.fullmatch(token):
            items.append({"symbol": token, "name": "", "market": "", "is_etf": False,
                          "resolved": False, "valid": False})
            continue
        code = token.zfill(6)
        if code in seen:
            continue
        seen.add(code)
        normalized.append(code)

    known: dict[str, dict] = {}
    if normalized:
        ph = ",".join("?" for _ in normalized)
        known = {r["symbol"]: dict(r) for r in repo.query(
            f"SELECT symbol, name, market, industry FROM dim_stock WHERE symbol IN ({ph})",
            tuple(normalized))}
    missing = [s for s in normalized if not (known.get(s, {}).get("name") or "").strip()]
    spot = _spot_names() if missing else {}
    for code in normalized:
        meta = known.get(code, {})
        name = (meta.get("name") or "").strip() or spot.get(code, "").strip()
        items.append({
            "symbol": code,
            "name": name,
            "market": meta.get("market") or infer_market(code),
            "is_etf": is_etf(code),
            "resolved": bool(name),
            "valid": True,
        })
    return items


def _prepare_members(repo: Repository, members: list[dict]) -> list[dict]:
    """校验并规范化待写入成员；名称缺失时尝试从 dim_stock/快照补。"""
    normalized: list[dict] = []
    seen: set[str] = set()
    unknown: list[dict] = []
    for item in members:
        raw = str(item.get("symbol", "")).strip()
        if not _SYMBOL_RE.fullmatch(raw):
            raise PoolValidationError(f"股票代码格式不正确：{raw or '(空)'}")
        code = raw.zfill(6)
        if code in seen:
            continue
        seen.add(code)
        normalized.append({"symbol": code, "name": (item.get("name") or "").strip()})
        if not normalized[-1]["name"]:
            unknown.append(normalized[-1])

    if unknown:
        resolved = {x["symbol"]: x["name"] for x in resolve_stock_names(
            repo, [x["symbol"] for x in unknown])}
        for item in unknown:
            item["name"] = resolved.get(item["symbol"], "")
    for item in normalized:
        item["name"] = _clean_name(item["name"], f"{item['symbol']} 名称")
        if "ST" in item["name"] or "退" in item["name"]:
            raise PoolValidationError(
                f"{item['symbol']}（{item['name']}）属于 ST/退市股票，已按系统规则排除")

    codes = [x["symbol"] for x in normalized]
    ph = ",".join("?" for _ in codes)
    meta = {r["symbol"]: dict(r) for r in repo.query(
        f"SELECT symbol, industry FROM dim_stock WHERE symbol IN ({ph})", tuple(codes))}
    return [{
        "symbol": x["symbol"],
        "name": x["name"],
        "market": infer_market(x["symbol"]),
        "sector_code": (meta.get(x["symbol"], {}).get("industry") or "自定义"),
    } for x in normalized]


def _upsert_stock_members(repo: Repository, pool_id: str, members: list[dict],
                          source: str = "manual") -> None:
    now = _now()
    repo.upsert("dim_stock", [{
        "symbol": m["symbol"], "name": m["name"], "market": m["market"],
        "updated_at": now,
    } for m in members], ["symbol"])
    repo.upsert("dim_universe_member", [{
        "universe_id": pool_id, "symbol": m["symbol"],
        "sector_code": m["sector_code"], "source": source, "updated_at": now,
    } for m in members], ["universe_id", "symbol"])


def _new_pool_id(repo: Repository) -> str:
    ids = [r["universe_id"] for r in repo.query("SELECT universe_id FROM dim_universe")]
    nums = [int(x[1:]) for x in ids if re.fullmatch(r"U\d+", x)]
    return f"U{max(nums, default=0) + 1}"


def _universe_row(repo: Repository, pool_id: str) -> dict:
    rows = repo.query("SELECT * FROM dim_universe WHERE universe_id=?", (pool_id,))
    if not rows:
        raise PoolNotFound(f"股票池不存在：{pool_id}")
    return dict(rows[0])


def get_pool(repo: Repository, pool_id: str) -> dict:
    row = _universe_row(repo, pool_id)
    members = universe_members(repo, pool_id)
    if pool_id == C0:
        ph = ",".join("?" for _ in members) or "''"
        names = {r["symbol"]: r["name"] for r in repo.query(
            f"SELECT symbol, name FROM dim_stock WHERE symbol IN ({ph})", tuple(members))}
        member_rows = [{"symbol": s, "name": names.get(s, ""), "sector_code": sec,
                        "source": "sector", "updated_at": None}
                       for s, sec in members.items()]
    else:
        member_rows = [dict(r) for r in repo.query(
            """SELECT m.symbol, IFNULL(s.name,'') AS name, m.sector_code,
                      m.source, m.updated_at
               FROM dim_universe_member m
               LEFT JOIN dim_stock s ON s.symbol=m.symbol
               WHERE m.universe_id=? ORDER BY m.symbol""", (pool_id,))]
    return {
        "id": pool_id,
        "name": row["name"] or pool_id,
        "description": row["description"] or "",
        "is_custom": bool(row["is_custom"]),
        "readonly": not bool(row["is_custom"]),
        "daily_enabled": bool(row["daily_enabled"]),
        "members_updated_at": row["members_updated_at"],
        "members_count": len(member_rows),
        "members": member_rows,
    }


def list_pools(repo: Repository) -> list[dict]:
    """池列表 + 数据/回测/调度/今日任务状态。"""
    latest = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")[0]["d"]
    rows = [dict(r) for r in repo.query(
        """SELECT * FROM dim_universe
           ORDER BY CASE WHEN universe_id='C0' THEN 0 ELSE 1 END, universe_id""")]
    today = datetime.now().strftime("%Y%m%d")
    out: list[dict] = []
    for row in rows:
        pid = row["universe_id"]
        members = universe_members(repo, pid)
        dates: dict[str, str | None] = {}
        if members:
            ph = ",".join("?" for _ in members)
            dates = {r["symbol"]: r["d"] for r in repo.query(
                f"SELECT symbol, MAX(trade_date) AS d FROM fact_quote "
                f"WHERE symbol IN ({ph}) GROUP BY symbol", tuple(members))}
        missing_count = sum(1 for s in members if not dates.get(s))
        stale_count = sum(1 for s in members
                          if dates.get(s) and latest and dates[s] < latest)
        if not members or missing_count == len(members):
            data_state = "missing"
        elif missing_count or stale_count:
            data_state = "stale"
        else:
            data_state = "latest"
        data_as_of = max((d for d in dates.values() if d), default=None)

        bt_rows = repo.query(
            "SELECT run_id, strategy, end_date, created_at FROM backtest_run "
            "WHERE strategy=? ORDER BY created_at DESC LIMIT 1",
            (backtest_strategy_name(pid),))
        bt = dict(bt_rows[0]) if bt_rows else None
        if not bt:
            backtest_state = "missing"
        elif (latest and bt["end_date"] < latest) or (
                row["members_updated_at"]
                and (bt["created_at"] or "") < row["members_updated_at"]):
            backtest_state = "stale"
        else:
            backtest_state = "latest"

        # 最新回测的年化/夏普（总览页卡片用）
        annual = sharpe = None
        if bt:
            series = repo.query(
                "SELECT trade_date, nav, benchmark FROM backtest_result "
                "WHERE run_id=? ORDER BY trade_date", (bt["run_id"],))
            if len(series) > 2:
                nav = pd.Series([r["nav"] for r in series],
                                index=[r["trade_date"] for r in series])
                bench = pd.Series([r["benchmark"] for r in series],
                                  index=[r["trade_date"] for r in series])
                try:
                    m = compute_metrics(nav, bench)
                    annual, sharpe = m.get("annual_return"), m.get("sharpe")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"{pid} 回测指标计算失败: {e}")

        # 今日信号数（单日截面打分，成本很低）
        try:
            signals_count = len(compute_signals(repo, universe=pid))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{pid} 信号计算失败: {e}")
            signals_count = None

        job_rows = repo.query(
            "SELECT status, started_at, ended_at FROM task_log WHERE task_id=?",
            (f"pool_{pid}_{today}",))
        job = dict(job_rows[0]) if job_rows else None
        schedule_enabled = True if pid == C0 else bool(row["daily_enabled"])
        out.append({
            "id": pid,
            "name": row["name"] or pid,
            "description": row["description"] or "",
            "is_custom": bool(row["is_custom"]),
            "readonly": not bool(row["is_custom"]),
            "members_count": len(members),
            "data_state": data_state,
            "data_as_of": data_as_of,
            "stale_count": stale_count,
            "missing_count": missing_count,
            "backtest_state": backtest_state,
            "backtest_end": bt["end_date"] if bt else None,
            "backtest_created_at": bt["created_at"] if bt else None,
            "backtest_annual": annual,
            "backtest_sharpe": sharpe,
            "signals_count": signals_count,
            "schedule_enabled": schedule_enabled,
            "schedule_note": "全局每日流水线" if pid == C0 else None,
            "job": job,
            "members_updated_at": row["members_updated_at"],
        })
    return out


def create_pool(repo: Repository, name: str, members: list[dict],
                description: str = "") -> dict:
    pool_name = _clean_name(name, "股票池名称")
    _ensure_name_unique(repo, pool_name)
    prepared = _prepare_members(repo, members)
    if not prepared:
        raise PoolValidationError("股票池至少需要一只股票")
    pool_id = _new_pool_id(repo)
    now = _now()
    repo.upsert("dim_universe", [{
        "universe_id": pool_id, "name": pool_name,
        "description": (description or "").strip(), "updated_at": now,
        "is_custom": 1, "daily_enabled": 0, "members_updated_at": now,
    }], ["universe_id"])
    _upsert_stock_members(repo, pool_id, prepared)
    logger.info(f"自定义股票池 {pool_id} 已创建：{pool_name}（{len(prepared)} 只）")
    return get_pool(repo, pool_id)


def rename_pool(repo: Repository, pool_id: str, name: str) -> dict:
    row = _universe_row(repo, pool_id)
    if not row["is_custom"]:
        raise PoolReadonly("内置股票池名称只读")
    pool_name = _clean_name(name, "股票池名称")
    _ensure_name_unique(repo, pool_name, exclude_id=pool_id)
    repo.conn.execute(
        "UPDATE dim_universe SET name=?, updated_at=? WHERE universe_id=?",
        (pool_name, _now(), pool_id))
    repo.conn.commit()
    return get_pool(repo, pool_id)


def add_members(repo: Repository, pool_id: str, members: list[dict]) -> dict:
    row = _universe_row(repo, pool_id)
    if not row["is_custom"]:
        raise PoolReadonly("内置股票池成员只读")
    prepared = _prepare_members(repo, members)
    existing = set(universe_members(repo, pool_id))
    added = [m for m in prepared if m["symbol"] not in existing]
    skipped = [m["symbol"] for m in prepared if m["symbol"] in existing]
    if added:
        _upsert_stock_members(repo, pool_id, added)
        repo.conn.execute(
            "UPDATE dim_universe SET members_updated_at=?, updated_at=? WHERE universe_id=?",
            (_now(), _now(), pool_id))
        repo.conn.commit()
    return {"added": added, "skipped": skipped}


def remove_member(repo: Repository, pool_id: str, symbol: str) -> dict:
    row = _universe_row(repo, pool_id)
    if not row["is_custom"]:
        raise PoolReadonly("内置股票池成员只读")
    code = str(symbol).strip().zfill(6)
    repo.conn.execute(
        "DELETE FROM dim_universe_member WHERE universe_id=? AND symbol=?",
        (pool_id, code))
    repo.conn.execute(
        "UPDATE dim_universe SET members_updated_at=?, updated_at=? WHERE universe_id=?",
        (_now(), _now(), pool_id))
    repo.conn.commit()
    return {"removed": code}


def set_daily_enabled(repo: Repository, pool_id: str, enabled: bool) -> dict:
    row = _universe_row(repo, pool_id)
    if pool_id == C0 and not enabled:
        raise PoolValidationError("C0 由全局每日流水线维护，不能关闭")
    repo.conn.execute(
        "UPDATE dim_universe SET daily_enabled=?, updated_at=? WHERE universe_id=?",
        (1 if enabled else 0, _now(), pool_id))
    repo.conn.commit()
    return {"id": pool_id, "daily_enabled": bool(enabled)}
