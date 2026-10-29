"""池级任务：数据补齐 → 财务 → 因子 → 回测 → 目标组合 → 跟踪 → 开启每日调度。

设计约定：
- 幂等：每一步先判断“是否已是最新/已回测/已开启”，能跳过就跳过并写明原因。
- 单写者：`data/pool_job.lock` 跨进程锁保证页面手动任务与 scheduler 每日任务不并发写 SQLite。
- 可审计：过程写入 task_log（task_id = pool_{pool_id}_{YYYYMMDD}）。
- C0 的组合/跟踪由全局每日流水线负责，池任务对其只做数据/因子/回测。
"""

from __future__ import annotations

import fcntl
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from ..backtest.engine import run_backtest
from ..common.logging import logger
from ..config.settings import get_settings
from ..features.engine import FACTOR_VERSION, compute_all
from ..features.fundamentals import compute_fundamental_factors
from ..ingest.fundamentals import sync_fundamentals
from ..ingest.universe_sync import sync_symbols
from ..research.pool_admin import (
    PoolError,
    PoolNotFound,
    backtest_strategy_name,
    get_pool,
    portfolio_strategy_name,
    set_daily_enabled,
)
from ..research.universe import C0, eligibility_panel, is_etf
from ..storage.repository import Repository, open_repository
from ..strategy.portfolio import build_target_portfolio
from ..strategy.tracking import simulate_tracking

_JOB_LOCK = threading.Lock()
_RUNNING_POOL: str | None = None
_TIMEOUT_SECONDS = 2 * 3600  # running 超过 2 小时视为中断


# ---------- 跨进程锁 ----------

def _lock_path() -> Path:
    return Path(get_settings().db_path).parent / "pool_job.lock"


def try_acquire_job_lock():
    """非阻塞获取跨进程锁；成功返回文件句柄，失败（他人持有）返回 None。"""
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def release_job_lock(handle) -> None:
    if not handle:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# ---------- 状态 ----------

def _task_id(pool_id: str) -> str:
    return f"pool_{pool_id}_{datetime.now().strftime('%Y%m%d')}"


def _write_task(repo: Repository, task_id: str, pool_id: str, status: str,
                started_at: str, actions: list[dict], trigger: str,
                error: str | None = None) -> None:
    payload = {"actions": actions, "trigger": trigger}
    if error:
        payload["error"] = error
    if status != "running":
        payload["ended_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    repo.log_task(task_id, f"pool_job:{pool_id}", status, started_at,
                  None if status == "running" else payload["ended_at"],
                  json.dumps(payload, ensure_ascii=False, default=str))


def get_pool_job(repo: Repository, pool_id: str) -> dict:
    """读取今日池任务状态；无记录返回 idle。"""
    task_id = _task_id(pool_id)
    rows = repo.query("SELECT * FROM task_log WHERE task_id=?", (task_id,))
    if not rows:
        return {"pool_id": pool_id, "task_id": task_id, "status": "idle",
                "actions": [], "message": "", "trigger": None}
    row = dict(rows[0])
    try:
        payload = json.loads(row["message"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    status = row["status"] or "unknown"
    actions = payload.get("actions", [])
    # 进程重启/线程中断后 running 会永远挂着，超时按失败处理
    if status == "running" and row["started_at"]:
        try:
            started = datetime.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            started = datetime.now()
        if (datetime.now() - started).total_seconds() > _TIMEOUT_SECONDS:
            status = "failed"
            actions = list(actions) + [
                {"step": "system", "status": "failed", "message": "任务中断，可重新启动"}]
    return {
        "pool_id": pool_id,
        "task_id": task_id,
        "status": status,
        "started_at": row["started_at"],
        "ended_at": payload.get("ended_at") or row["ended_at"],
        "trigger": payload.get("trigger"),
        "error": payload.get("error"),
        "actions": actions,
        "message": payload.get("error") or "",
    }


# ---------- 状态判定 ----------

def _symbol_dates(repo: Repository, symbols: list[str]) -> dict[str, str | None]:
    if not symbols:
        return {}
    ph = ",".join("?" for _ in symbols)
    return {r["symbol"]: r["d"] for r in repo.query(
        f"SELECT symbol, MAX(trade_date) AS d FROM fact_quote "
        f"WHERE symbol IN ({ph}) GROUP BY symbol", tuple(symbols))}


def backtest_state(repo: Repository, pool_id: str, latest: str | None,
                   members_updated_at: str | None) -> tuple[str, dict | None]:
    """missing | stale | latest。成员变更时间晚于回测创建时间时视为过期。"""
    rows = repo.query(
        "SELECT run_id, start_date, end_date, created_at FROM backtest_run "
        "WHERE strategy=? ORDER BY created_at DESC LIMIT 1",
        (backtest_strategy_name(pool_id),))
    if not rows:
        return "missing", None
    run = dict(rows[0])
    stale = bool(latest and (run["end_date"] or "") < latest)
    if not stale and members_updated_at:
        stale = (run["created_at"] or "") < members_updated_at
    return ("stale" if stale else "latest"), run


def _factors_ready(repo: Repository, symbols: list[str], trade_date: str | None) -> bool:
    """该池在 trade_date 的 v2 因子是否已覆盖全部成员（用 vol_20 作为判据）。"""
    if not symbols or not trade_date:
        return False
    ph = ",".join("?" for _ in symbols)
    n = repo.query(
        f"SELECT COUNT(DISTINCT symbol) AS n FROM fact_factor "
        f"WHERE version=? AND factor='vol_20' AND trade_date=? AND symbol IN ({ph})",
        tuple([FACTOR_VERSION, trade_date] + symbols))[0]["n"]
    return int(n) >= len(symbols)


# ---------- 主任务 ----------

def run_pool_job(repo: Repository, pool_id: str, force: bool = False,
                 trigger: str = "manual", lock_held: bool = False) -> dict:
    """执行一次池任务；lock_held=True 表示调用方已持有跨进程锁。"""
    started = datetime.now()
    started_str = started.strftime("%Y-%m-%d %H:%M:%S")
    task_id = _task_id(pool_id)
    actions: list[dict] = []
    result: dict = {"pool_id": pool_id, "task_id": task_id, "trigger": trigger,
                    "status": "running", "actions": actions}

    def step(name: str, status: str, message: str) -> None:
        actions.append({"step": name, "status": status, "message": message})

    def finish(status: str, error: str | None = None) -> dict:
        result["status"] = status
        if error:
            result["error"] = error
        _write_task(repo, task_id, pool_id, status, started_str, actions, trigger, error)
        logger.info(f"池任务 {pool_id} 结束：{status} / "
                    + "；".join(f"{a['step']}={a['status']}" for a in actions))
        return result

    handle = None
    if not lock_held:
        handle = try_acquire_job_lock()
        if handle is None:
            step("lock", "busy", "已有池任务运行中")
            return finish("busy")

    try:
        _write_task(repo, task_id, pool_id, "running", started_str, actions, trigger)

        # 1) 加载池
        try:
            pool = get_pool(repo, pool_id)
        except PoolNotFound as e:
            step("pool", "failed", str(e))
            return finish("failed", str(e))
        symbols = [m["symbol"] for m in pool["members"]]
        if not symbols:
            step("pool", "failed", "股票池为空")
            return finish("failed", "股票池为空")
        step("pool", "ok", f"{pool['name']}：{len(symbols)} 只")

        # 2) 行情 / 资金流
        latest = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")[0]["d"]
        dates = _symbol_dates(repo, symbols)
        missing = [s for s in symbols if not dates.get(s)]
        stale = [s for s in symbols if dates.get(s) and latest and dates[s] < latest]
        try:
            if missing:
                st = sync_symbols(repo, missing, start_date="20180101", with_flow=True)
                step("data", "ok",
                     f"补全 {len(missing)} 只历史行情（成功 {st['price_ok']}，失败 {st['price_fail']}）")
            if stale:
                ph = ",".join("?" for _ in stale)
                have_flow = {r["symbol"] for r in repo.query(
                    f"SELECT DISTINCT symbol FROM fact_stock_flow WHERE symbol IN ({ph})",
                    tuple(stale))}
                start = (datetime.strptime(latest, "%Y-%m-%d")
                         - timedelta(days=15)).strftime("%Y%m%d")
                priced = [s for s in stale if s in have_flow]
                noflow = [s for s in stale if s not in have_flow]
                ok = 0
                if priced:
                    ok += sync_symbols(repo, priced, start_date=start, with_flow=False)["price_ok"]
                if noflow:
                    st = sync_symbols(repo, noflow, start_date=start, with_flow=True)
                    ok += st["price_ok"]
                step("data", "ok", f"增量补拉 {len(stale)} 只（成功 {ok}）")
            if not missing and not stale:
                step("data", "skipped", f"已是 {latest}")
        except Exception as e:  # noqa: BLE001
            step("data", "failed", str(e))
            return finish("failed", str(e))

        latest_data = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")[0]["d"]

        # 3) 财务 / 上市日（ETF 无财务）
        non_etf = [s for s in symbols if not is_etf(s)]
        try:
            need: list[str] = []
            if non_etf:
                ph = ",".join("?" for _ in non_etf)
                fin = {r["symbol"] for r in repo.query(
                    f"SELECT DISTINCT symbol FROM fact_financial WHERE symbol IN ({ph})",
                    tuple(non_etf))}
                ipo = {r["symbol"]: r["ipo_date"] for r in repo.query(
                    f"SELECT symbol, ipo_date FROM dim_stock WHERE symbol IN ({ph})",
                    tuple(non_etf))}
                need = [s for s in non_etf if s not in fin or not ipo.get(s)]
            if need:
                st = sync_fundamentals(repo, need, start_year=2017)
                step("fundamentals", "ok",
                     f"补全 {len(need)} 只（成功 {st['basic']}，失败 {st['fail']}）")
            else:
                step("fundamentals", "skipped", "财务/上市日已齐全")
        except Exception as e:  # noqa: BLE001
            step("fundamentals", "failed", str(e))
            return finish("failed", str(e))

        # 4) 因子
        try:
            data_updated = bool(missing or stale)
            if not force and not data_updated and _factors_ready(repo, symbols, latest_data):
                step("factors", "skipped", f"{latest_data} 因子已齐全")
            else:
                stats = compute_all(repo, symbols=symbols)
                msg = f"v2 {len(stats)} 个因子"
                if non_etf:
                    try:
                        fstats = compute_fundamental_factors(repo, symbols=non_etf)
                        msg += f"，v3 {len(fstats)} 个"
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"{pool_id} 基本面因子失败: {e}")
                        msg += "，v3 失败"
                step("factors", "ok", msg)
        except Exception as e:  # noqa: BLE001
            step("factors", "failed", str(e))
            return finish("failed", str(e))

        # 5) 回测
        state, run = backtest_state(repo, pool_id, latest_data, pool["members_updated_at"])
        try:
            if state == "latest" and not force:
                step("backtest", "skipped", f"已回测至 {run['end_date']}")
            else:
                res = run_backtest(
                    repo, top_k=12, rebalance="monthly", hold_buffer=1.0,
                    strategy="defensive", weighting="vol_inv", vol_target=0.25,
                    universe=symbols, eligibility=eligibility_panel(repo, symbols),
                    cost_model="a_share", limit_model="board",
                    universe_tag=pool_id, persist=True,
                )
                m = res["metrics"]
                step("backtest", "ok",
                     f"年化 {m['annual_return']:.2%}，夏普 {m['sharpe']:.2f}，"
                     f"{res['start']}~{res['end']}")
        except Exception as e:  # noqa: BLE001
            step("backtest", "failed", str(e))
            return finish("failed", str(e))

        # 6) 目标组合（C0 由全局流水线负责）
        portfolio_name = portfolio_strategy_name(pool_id)
        if pool_id == C0:
            step("portfolio", "skipped", "C0 由全局每日流水线维护")
        else:
            try:
                exist = repo.query(
                    "SELECT 1 FROM fact_target_run WHERE trade_date=? AND strategy=?",
                    (latest_data, portfolio_name))
                if exist and not force:
                    step("portfolio", "skipped", f"{latest_data} 已生成")
                else:
                    pf = build_target_portfolio(repo, trade_date=latest_data, persist=True,
                                                universe_id=pool_id, strategy="defensive")
                    if pf.get("error"):
                        step("portfolio", "failed", pf["error"])
                    else:
                        step("portfolio", "ok",
                             f"{len(pf.get('positions', []))} 只，仓位 "
                             f"{float(pf.get('exposure') or 0):.0%}")
            except Exception as e:  # noqa: BLE001
                step("portfolio", "failed", str(e))

        # 7) 模拟跟踪（C0 由全局流水线负责）
        if pool_id == C0:
            step("tracking", "skipped", "C0 由全局每日流水线维护")
        else:
            try:
                row = repo.query(
                    "SELECT MAX(trade_date) AS d FROM fact_tracking_nav WHERE strategy=?",
                    (portfolio_name,))
                last_nav = row[0]["d"] if row else None
                if last_nav and latest_data and last_nav >= latest_data and not force:
                    step("tracking", "skipped", f"净值已到 {last_nav}")
                else:
                    tr = simulate_tracking(repo, strategy=portfolio_name, persist=True,
                                           universe_id=pool_id)
                    msg = tr.get("message") or f"{tr.get('status')}，{tr.get('days', 0)} 个交易日"
                    step("tracking", "ok" if tr.get("status") == "running" else "skipped", msg)
            except Exception as e:  # noqa: BLE001
                step("tracking", "failed", str(e))

        # 8) 开启每日调度
        if pool_id == C0:
            step("schedule", "skipped", "全局每日流水线")
        elif pool["daily_enabled"]:
            step("schedule", "skipped", "已开启")
        else:
            try:
                set_daily_enabled(repo, pool_id, True)
                step("schedule", "ok", "已开启每日调度")
            except PoolError as e:
                step("schedule", "failed", str(e))

        failed = [a for a in actions if a["status"] == "failed"]
        if failed:
            return finish("failed", "；".join(f"{a['step']}: {a['message']}" for a in failed))
        # pool 步骤只是加载信息，不参与“是否全部跳过”的判断
        work = [a for a in actions if a["step"] != "pool"]
        if work and all(a["status"] == "skipped" for a in work):
            return finish("skipped")
        return finish("ok")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"池任务 {pool_id} 异常: {e}")
        step("system", "failed", str(e))
        return finish("failed", str(e))
    finally:
        release_job_lock(handle)


def start_pool_job(pool_id: str) -> dict:
    """API 入口：抢锁后起后台线程执行，立即返回 running/busy。"""
    global _RUNNING_POOL
    with _JOB_LOCK:
        if _RUNNING_POOL:
            return {"status": "busy", "message": f"池任务运行中：{_RUNNING_POOL}"}
        handle = try_acquire_job_lock()
        if handle is None:
            return {"status": "busy", "message": "已有池任务运行中"}
        _RUNNING_POOL = pool_id

    repo = open_repository()
    try:
        # 先落 running 行，保证前端第一次轮询就能看到状态
        _write_task(repo, _task_id(pool_id), pool_id, "running",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), [], "manual")
    finally:
        repo.close()

    def _runner() -> None:
        global _RUNNING_POOL
        runner_repo = open_repository()
        try:
            run_pool_job(runner_repo, pool_id, trigger="manual", lock_held=True)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"池任务 {pool_id} 线程异常: {e}")
        finally:
            runner_repo.close()
            release_job_lock(handle)
            with _JOB_LOCK:
                _RUNNING_POOL = None

    threading.Thread(target=_runner, daemon=True, name=f"pool-job-{pool_id}").start()
    return {"status": "running", "pool_id": pool_id, "task_id": _task_id(pool_id)}


def run_enabled_pool_jobs(repo: Repository) -> dict:
    """每日流水线调用：逐个执行已开启调度的非 C0 池（调用方已持锁）。"""
    rows = repo.query(
        "SELECT universe_id FROM dim_universe WHERE daily_enabled=1 AND universe_id<>? "
        "ORDER BY universe_id", (C0,))
    out: dict = {}
    for r in rows:
        pid = r["universe_id"]
        try:
            res = run_pool_job(repo, pid, trigger="scheduler", lock_held=True)
            out[pid] = {"status": res["status"],
                        "actions": [f"{a['step']}:{a['status']}" for a in res["actions"]]}
        except Exception as e:  # noqa: BLE001
            logger.exception(f"池任务 {pid} 失败: {e}")
            out[pid] = {"status": "failed", "error": str(e)}
    return out
