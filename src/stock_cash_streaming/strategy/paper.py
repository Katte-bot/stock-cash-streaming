"""模拟持仓（纸面账户）：按目标权重月频调仓的成交流水、持仓与资产曲线。

规则（尽量贴近 A 股实际）：
- 初始资金默认 100,000 元；整手（100 股）成交；
- 调仓日历与回测/目标组合一致：信号日次一交易日收盘执行；
- 费用：佣金万 2.5（最低 5 元）+ 过户费万 0.1 + 卖出印花税
  （2023-08-28 前 0.1%，之后 0.05%）；
- 先卖后买；现金不足时等比缩量，买不起 1 手的标的跳过；
- 价格为后复权收盘价（收益连续、含分红），并按账户建立时固定的「后复权/真实收盘」
  比例折算到真实价位，保证 100 股整手可成交且历史流水稳定。

历史段来自 `run_backtest(return_weights=True)` 的逐日权重，实时段来自
`fact_target_portfolio` 的调仓快照；两者统一存为 `fact_paper_event` 后回放，
保证幂等与可重放。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

import numpy as np
import pandas as pd

from ..backtest.engine import run_backtest
from ..common.logging import logger
from ..research.pool_admin import portfolio_strategy_name
from ..research.universe import C0, eligibility_panel, universe_members
from ..storage.repository import Repository

DEFAULT_CAPITAL = 100_000.0
MIN_CAPITAL, MAX_CAPITAL = 10_000.0, 1_000_000.0
LOT = 100
COMMISSION_RATE = 0.00025
MIN_COMMISSION = 5.0
TRANSFER_RATE = 0.00001
STAMP_SWITCH = "2023-08-28"
STAMP_BEFORE, STAMP_AFTER = 0.001, 0.0005

# 所有股票池（内置 C0~C3 + 用户自建 U*）都启用模拟持仓
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 费用 ----------

def trade_fee(side: str, trade_date: str, amount: float) -> float:
    """单笔交易费用：佣金（最低 5 元）+ 过户费 + 卖出印花税。"""
    amount = float(amount)
    if amount <= 0:
        return 0.0
    fee = max(MIN_COMMISSION, amount * COMMISSION_RATE) + amount * TRANSFER_RATE
    if side == "sell":
        fee += amount * (STAMP_BEFORE if trade_date < STAMP_SWITCH else STAMP_AFTER)
    return round(fee, 2)


# ---------- 纯回放（不依赖数据库） ----------

def _greedy_lots(target_values: dict[str, float], prices: pd.Series,
                 budget: float) -> dict[str, int]:
    """按目标金额贪心补整手，避免小资金因高价股买不起而大量空仓。

    每轮给「目标金额 - 已分配金额」缺口最大的标的加 1 手，直到资金不足或达标；
    总投入不超过 budget，因此不会产生负现金。
    """
    lots = {s: 0 for s in target_values}
    remaining = float(budget)
    rate = COMMISSION_RATE + TRANSFER_RATE
    while True:
        best, best_gap = None, 1e-6
        for s, tv in target_values.items():
            p = float(prices[s])
            if pd.isna(p) or p <= 0:
                continue
            cost = p * LOT * (1 + rate)
            gap = tv - lots[s] * p * LOT
            if gap > best_gap and cost <= remaining:
                best, best_gap = s, gap
        if best is None:
            return lots
        lots[best] += 1
        remaining -= float(prices[best]) * LOT * (1 + rate)


def simulate_ledger(prices: pd.DataFrame, events: list[tuple[str, dict[str, float]]],
                    capital: float = DEFAULT_CAPITAL,
                    benchmark: pd.Series | None = None) -> dict:
    """按 (执行日, 目标权重) 逐事件回放，返回成交/净值/持仓。

    prices: index=trade_date，columns=symbol，已 ffill 的后复权收盘价。
    """
    empty = {"status": "waiting", "trades": [], "nav": [], "positions": [],
             "cash": float(capital), "total_value": float(capital), "skipped": 0,
             "rebalances": 0, "start_date": None, "last_date": None}
    if prices is None or prices.empty or not events:
        return empty

    dates = list(prices.index)
    symbols = list(prices.columns)
    cash = float(capital)
    shares = {s: 0 for s in symbols}
    cost_total = {s: 0.0 for s in symbols}
    trades: list[dict] = []
    nav: list[dict] = []
    seq_by_date: dict[str, int] = {}
    skipped = 0
    rebalances = 0
    peak = float(capital)
    started = False
    pending = {d: w for d, w in sorted(events, key=lambda x: x[0])}

    def add_trade(d: str, s: str, side: str, price: float, qty: int,
                  amount: float, fee: float, reason: str) -> None:
        seq_by_date[d] = seq_by_date.get(d, 0) + 1
        trades.append({"trade_date": d, "seq": seq_by_date[d], "symbol": s,
                       "side": side, "price": round(price, 4), "shares": int(qty),
                       "amount": round(amount, 2), "fee": round(fee, 2),
                       "reason": reason})

    for d in dates:
        px = prices.loc[d]
        weights = pending.get(d)
        if weights is not None:
            total_before = cash + sum(
                shares[s] * float(px[s]) for s in symbols if not pd.isna(px[s]))
            # 目标金额 → 整手（贪心补足，尽量贴近目标仓位）
            target_values = {}
            for s, w in weights.items():
                if s not in px.index or pd.isna(px[s]) or float(w) <= 1e-9:
                    continue
                target_values[s] = total_before * float(w)
            target_lots = _greedy_lots(target_values, px, total_before)
            target = {s: 0 for s in symbols}
            for s, lots in target_lots.items():
                target[s] = int(lots) * LOT

            # 1) 卖出（释放现金）
            for s in symbols:
                if shares[s] <= target[s]:
                    continue
                price = float(px[s])
                if pd.isna(price):
                    continue
                qty = (shares[s] - target[s]) // LOT * LOT
                if qty <= 0:
                    continue
                amount = qty * price
                fee = trade_fee("sell", d, amount)
                cash += amount - fee
                old = shares[s]
                shares[s] -= qty
                cost_total[s] *= (shares[s] / old) if old else 0.0
                add_trade(d, s, "sell", price, qty, amount, fee,
                          "initial" if not started else "rebalance")

            # 2) 买入（按可用现金等比缩量）
            buys = [(s, target[s] - shares[s], float(px[s])) for s in symbols
                    if target[s] > shares[s] and not pd.isna(px[s])]
            required = sum(q * p * (1 + COMMISSION_RATE + TRANSFER_RATE)
                           for _, q, p in buys)
            scale = 1.0 if required <= cash or required <= 0 else cash / required
            for s, qty, price in buys:
                q = int(qty * scale) // LOT * LOT
                if q <= 0:
                    skipped += 1
                    continue
                amount = q * price
                fee = trade_fee("buy", d, amount)
                if amount + fee > cash:
                    q = int((cash / (price * (1 + COMMISSION_RATE + TRANSFER_RATE))) // LOT) * LOT
                    if q <= 0:
                        skipped += 1
                        continue
                    amount = q * price
                    fee = trade_fee("buy", d, amount)
                    if amount + fee > cash:
                        skipped += 1
                        continue
                cash -= amount + fee
                shares[s] += q
                cost_total[s] += amount + fee
                add_trade(d, s, "buy", price, q, amount, fee,
                          "initial" if not started else "rebalance")

            rebalances += 1
            started = True

        if not started:
            continue

        mv = sum(shares[s] * float(px[s]) for s in symbols if not pd.isna(px[s]))
        total = cash + mv
        peak = max(peak, total)
        b = 1.0
        if benchmark is not None and d in benchmark.index and not pd.isna(benchmark.loc[d]):
            b = float(benchmark.loc[d])
        nav.append({"trade_date": d, "total_value": round(total, 2),
                    "cash": round(cash, 2), "market_value": round(mv, 2),
                    "ret": round(total / capital - 1, 6),
                    "benchmark": round(b, 6),
                    "drawdown": round(total / peak - 1, 6)})

    if not nav:
        return empty

    last_px = prices.iloc[-1]
    total_value = float(nav[-1]["total_value"])
    positions = []
    for s in symbols:
        if shares[s] <= 0:
            continue
        price = float(last_px[s]) if not pd.isna(last_px[s]) else 0.0
        mv = shares[s] * price
        positions.append({
            "symbol": s, "shares": int(shares[s]),
            "avg_cost": round(cost_total[s] / shares[s], 4),
            "last_price": round(price, 4), "market_value": round(mv, 2),
            "weight": round(mv / total_value, 6) if total_value else 0.0,
            "pnl": round(mv - cost_total[s], 2),
        })
    positions.sort(key=lambda x: -x["market_value"])

    return {"status": "ok", "trades": trades, "nav": nav, "positions": positions,
            "cash": round(cash, 2), "total_value": round(total_value, 2),
            "skipped": skipped, "rebalances": rebalances,
            "start_date": nav[0]["trade_date"], "last_date": nav[-1]["trade_date"]}


def _events_from_weights(weights: pd.DataFrame | None, source: str = "backtest") -> list[dict]:
    """逐日权重 → 调仓事件。权重在 d 日持有，即 d-1 收盘执行。"""
    if weights is None or weights.empty:
        return []
    dates = list(weights.index)
    out: list[dict] = []
    prev = None
    for i, d in enumerate(dates):
        row = weights.loc[d].fillna(0.0)
        if prev is None:
            if float(np.abs(row.values).sum()) > 1e-9:
                out.append({"exec_date": d, "source": source,
                            "weights": {s: float(v) for s, v in row.items() if v > 1e-9}})
        elif not np.allclose(row.values, prev.values, atol=1e-8):
            # 新权重在 d 日生效 → 对应 d 的前一交易日收盘成交
            out.append({"exec_date": dates[i - 1], "source": source,
                        "weights": {s: float(v) for s, v in row.items() if v > 1e-9}})
        prev = row
    return out


def _events_from_snapshots(repo: Repository, universe_id: str,
                           after_date: str | None) -> list[dict]:
    """目标组合调仓快照 → 事件（信号日次一交易日收盘执行）。"""
    strategy = portfolio_strategy_name(universe_id)
    rows = repo.query(
        "SELECT trade_date, symbol, weight FROM fact_target_portfolio "
        "WHERE strategy=? AND is_rebalance=1 ORDER BY trade_date, symbol", (strategy,))
    if not rows:
        return []
    snaps: dict[str, dict[str, float]] = {}
    for r in rows:
        snaps.setdefault(r["trade_date"], {})[r["symbol"]] = float(r["weight"] or 0.0)

    dates = [r["d"] for r in repo.query(
        "SELECT DISTINCT trade_date AS d FROM fact_quote ORDER BY trade_date")]
    pos = {d: i for i, d in enumerate(dates)}
    out: list[dict] = []
    for sig, w in sorted(snaps.items()):
        i = pos.get(sig)
        if i is None or i + 1 >= len(dates):
            continue
        exec_date = dates[i + 1]
        if after_date and exec_date <= after_date:
            continue
        out.append({"exec_date": exec_date, "source": "live",
                    "weights": {s: x for s, x in w.items() if x > 1e-9}})
    return out


# ---------- 数据读写 ----------

def _price_scale(repo: Repository, symbols: list[str]) -> dict[str, float]:
    """后复权价 → 最新真实价位的折算比例。

    `fact_quote.close` 是后复权价，部分标的被成倍放大（如 600406：1120 vs 23.98），
    直接按 100 股整手会“买不起”。用最新一天的「后复权 / 新浪真实收盘」比例
    统一折算，收益序列不变，但股数与金额落到真实价位。
    """
    if not symbols:
        return {}
    ph = ",".join("?" for _ in symbols)
    q = {r["symbol"]: float(r["close"]) for r in repo.query(
        "SELECT symbol, close FROM fact_quote "
        f"WHERE trade_date=(SELECT MAX(trade_date) FROM fact_quote) AND symbol IN ({ph})",
        tuple(symbols))}
    f = {r["symbol"]: float(r["close"]) for r in repo.query(
        "SELECT symbol, close FROM fact_stock_flow "
        "WHERE trade_date=(SELECT MAX(trade_date) FROM fact_stock_flow) "
        f"AND symbol IN ({ph})", tuple(symbols))}
    return {s: q[s] / f[s] for s in q
            if f.get(s) and f[s] > 0 and q[s] > 0}


def _load_prices(repo: Repository, symbols: list[str], start: str,
                 scale: dict[str, float] | None = None) -> pd.DataFrame:
    """取后复权收盘价，并按固定比例折算到真实价位（收益连续、含分红）。"""
    if not symbols:
        return pd.DataFrame()
    ph = ",".join("?" for _ in symbols)
    rows = repo.query(
        f"SELECT symbol, trade_date, close FROM fact_quote "
        f"WHERE trade_date>=? AND symbol IN ({ph}) ORDER BY trade_date",
        tuple([start] + symbols))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    prices = df.pivot(index="trade_date", columns="symbol",
                      values="close").sort_index().ffill()
    scale = scale or {}
    for s in prices.columns:
        k = float(scale.get(s) or 1.0)
        if k > 0:
            prices[s] = prices[s] / k
    return prices


def _benchmark(prices: pd.DataFrame) -> pd.Series | None:
    """该池等权买入持有（起点归一）。"""
    if prices is None or prices.empty:
        return None
    bases = {}
    for c in prices.columns:
        s = prices[c].dropna()
        if len(s):
            bases[c] = float(s.iloc[0])
    if not bases:
        return None
    norm = prices.divide(pd.Series(bases), axis=1)
    return norm.mean(axis=1, skipna=True)


def _delete_universe(repo: Repository, universe_id: str, include_events: bool = True) -> None:
    tables = ["fact_paper_account", "fact_paper_position",
              "fact_paper_trade", "fact_paper_nav"]
    if include_events:
        tables.append("fact_paper_event")
    for t in tables:
        repo.conn.execute(f"DELETE FROM {t} WHERE universe_id=?", (universe_id,))
    repo.conn.commit()


def _write_ledger(repo: Repository, universe_id: str, capital: float, ledger: dict,
                  scale: dict[str, float] | None = None) -> None:
    now = _now()
    _delete_universe(repo, universe_id, include_events=False)
    repo.upsert("fact_paper_account", [{
        "universe_id": universe_id, "initial_capital": float(capital),
        "cash": ledger["cash"], "start_date": ledger["start_date"],
        "last_date": ledger["last_date"], "total_value": ledger["total_value"],
        "rebalances": ledger["rebalances"], "trades_count": len(ledger["trades"]),
        "price_scale": json.dumps(scale or {}, ensure_ascii=False),
        "created_at": now, "updated_at": now,
    }], ["universe_id"])

    if ledger["positions"]:
        repo.upsert("fact_paper_position", [{
            "universe_id": universe_id, "symbol": p["symbol"], "name": p.get("name", ""),
            "shares": p["shares"], "avg_cost": p["avg_cost"],
            "last_price": p["last_price"], "market_value": p["market_value"],
            "weight": p["weight"], "pnl": p["pnl"], "updated_at": now,
        } for p in ledger["positions"]], ["universe_id", "symbol"])

    if ledger["nav"]:
        repo.upsert("fact_paper_nav", [{
            "universe_id": universe_id, "trade_date": n["trade_date"],
            "total_value": n["total_value"], "cash": n["cash"],
            "market_value": n["market_value"], "ret": n["ret"],
            "benchmark": n["benchmark"], "drawdown": n["drawdown"],
            "created_at": now,
        } for n in ledger["nav"]], ["universe_id", "trade_date"])

    if ledger["trades"]:
        repo.upsert("fact_paper_trade", [{
            "universe_id": universe_id, "trade_date": t["trade_date"], "seq": t["seq"],
            "symbol": t["symbol"], "name": t.get("name", ""), "side": t["side"],
            "price": t["price"], "shares": t["shares"], "amount": t["amount"],
            "fee": t["fee"], "reason": t["reason"], "created_at": now,
        } for t in ledger["trades"]], ["universe_id", "trade_date", "seq"])


def _names(repo: Repository, symbols: list[str]) -> dict[str, str]:
    if not symbols:
        return {}
    ph = ",".join("?" for _ in symbols)
    return {r["symbol"]: r["name"] for r in repo.query(
        f"SELECT symbol, name FROM dim_stock WHERE symbol IN ({ph})", tuple(symbols))}


def _replay_and_write(repo: Repository, universe_id: str, capital: float,
                      scale: dict[str, float] | None = None) -> dict:
    rows = repo.query(
        "SELECT exec_date, symbol, weight FROM fact_paper_event "
        "WHERE universe_id=? ORDER BY exec_date", (universe_id,))
    if not rows:
        return {"status": "waiting", "universe": universe_id, "message": "暂无调仓事件"}
    grouped: dict[str, dict[str, float]] = {}
    for r in rows:
        grouped.setdefault(r["exec_date"], {})[r["symbol"]] = float(r["weight"])
    events = sorted(grouped.items())

    symbols = sorted({s for _, w in events for s in w})
    # 价格折算比例：首次建立后固定，保证历史流水稳定；新出现的标的再补算
    acc = get_account(repo, universe_id)
    if scale is None:
        scale = {}
        if acc and acc.get("price_scale"):
            try:
                scale = {k: float(v) for k, v in json.loads(acc["price_scale"]).items()}
            except (TypeError, ValueError):
                scale = {}
    missing = [s for s in symbols if s not in scale]
    if missing:
        scale.update(_price_scale(repo, missing))

    prices = _load_prices(repo, symbols, events[0][0], scale)
    if prices.empty:
        return {"status": "waiting", "universe": universe_id, "message": "等待行情数据"}

    ledger = simulate_ledger(prices, events, capital, benchmark=_benchmark(prices))
    if ledger["status"] != "ok":
        return {"status": "waiting", "universe": universe_id, "message": "等待更多数据"}

    names = _names(repo, symbols)
    for t in ledger["trades"]:
        t["name"] = names.get(t["symbol"], "")
    for p in ledger["positions"]:
        p["name"] = names.get(p["symbol"], "")

    _write_ledger(repo, universe_id, capital, ledger, scale)
    logger.info(f"模拟持仓 {universe_id} 已更新：{ledger['trades'].__len__()} 笔成交，"
                f"总资产 {ledger['total_value']:,.2f}")
    return {"status": "ok", "universe": universe_id,
            "start_date": ledger["start_date"], "last_date": ledger["last_date"],
            "trades": len(ledger["trades"]), "rebalances": ledger["rebalances"],
            "total_value": ledger["total_value"], "skipped": ledger["skipped"]}


def get_account(repo: Repository, universe_id: str = C0) -> dict | None:
    rows = repo.query("SELECT * FROM fact_paper_account WHERE universe_id=?", (universe_id,))
    return dict(rows[0]) if rows else None


def _rebuild(repo: Repository, universe_id: str, capital: float) -> dict:
    members = universe_members(repo, universe_id)
    if not members:
        return {"status": "empty", "universe": universe_id,
                "message": f"股票池 {universe_id} 为空"}
    symbols = list(members)
    try:
        res = run_backtest(
            repo, top_k=12, rebalance="monthly", hold_buffer=1.0,
            strategy="defensive", weighting="vol_inv", vol_target=0.25,
            universe=symbols, eligibility=eligibility_panel(repo, symbols),
            cost_model="a_share", limit_model="board",
            persist=False, return_weights=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{universe_id} 模拟持仓回测失败: {e}")
        return {"status": "failed", "universe": universe_id, "message": str(e)}
    events = _events_from_weights(res.get("weights"), "backtest")
    last = events[-1]["exec_date"] if events else None
    events += _events_from_snapshots(repo, universe_id, last)
    if not events:
        return {"status": "waiting", "universe": universe_id, "message": "暂无调仓事件"}

    _delete_universe(repo, universe_id)
    now = _now()
    repo.upsert("fact_paper_event", [{
        "universe_id": universe_id, "exec_date": e["exec_date"], "symbol": s,
        "weight": w, "source": e["source"], "created_at": now,
    } for e in events for s, w in e["weights"].items()],
        ["universe_id", "exec_date", "symbol"])

    return _replay_and_write(repo, universe_id, capital, _price_scale(repo, symbols))


def rebuild_paper_account(repo: Repository, universe_id: str = C0,
                          capital: float = DEFAULT_CAPITAL,
                          acquire_lock: bool = True) -> dict:
    """历史回放 + 实时事件重建账户（会跑一次该池回测，约 10–30 秒）。"""
    if acquire_lock:
        from ..pipeline.pool_jobs import release_job_lock, try_acquire_job_lock

        with _LOCK:
            handle = try_acquire_job_lock()
            if handle is None:
                return {"status": "busy", "universe": universe_id,
                        "message": "已有任务运行中，请稍后重试"}
            try:
                return _rebuild(repo, universe_id, float(capital))
            finally:
                release_job_lock(handle)
    return _rebuild(repo, universe_id, float(capital))


def refresh_paper_account(repo: Repository, universe_id: str = C0) -> dict:
    """每日增量：追加新的调仓快照事件后全量重放（不跑回测，约 <1 秒）。"""
    acc = get_account(repo, universe_id)
    if not acc:
        # 每日流水线已持有跨进程锁，这里不再抢锁
        return rebuild_paper_account(repo, universe_id, DEFAULT_CAPITAL, acquire_lock=False)

    last = repo.query(
        "SELECT MAX(exec_date) AS d FROM fact_paper_event WHERE universe_id=?",
        (universe_id,))[0]["d"]
    new_events = _events_from_snapshots(repo, universe_id, last)
    if new_events:
        now = _now()
        repo.upsert("fact_paper_event", [{
            "universe_id": universe_id, "exec_date": e["exec_date"], "symbol": s,
            "weight": w, "source": e["source"], "created_at": now,
        } for e in new_events for s, w in e["weights"].items()],
            ["universe_id", "exec_date", "symbol"])
    return _replay_and_write(repo, universe_id, float(acc["initial_capital"]))


def refresh_all_paper_accounts(repo: Repository) -> dict:
    """每日流水线调用：为所有股票池刷新/建立模拟持仓。

    单个池失败（数据不足、回测异常）只记录结果，不影响其它池。
    """
    ids = [r["universe_id"] for r in repo.query(
        "SELECT universe_id FROM dim_universe ORDER BY universe_id")]
    out: dict = {}
    for uid in ids:
        try:
            out[uid] = refresh_paper_account(repo, uid)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"模拟持仓 {uid} 刷新失败: {e}")
            out[uid] = {"status": "failed", "universe": uid, "error": str(e)}
    return out


def reset_paper_account(repo: Repository, universe_id: str = C0,
                        capital: float = DEFAULT_CAPITAL) -> dict:
    """重置账户：清空该池全部模拟数据后按新资金重建。"""
    capital = float(capital)
    if not (MIN_CAPITAL <= capital <= MAX_CAPITAL):
        return {"status": "invalid", "universe": universe_id,
                "message": f"初始资金需在 {MIN_CAPITAL:,.0f} ~ {MAX_CAPITAL:,.0f} 之间"}
    return rebuild_paper_account(repo, universe_id, capital, acquire_lock=True)


def paper_summary(repo: Repository, universe_id: str = C0,
                  trade_limit: int = 100) -> dict:
    acc = get_account(repo, universe_id)
    enabled = True  # 所有股票池都启用模拟持仓
    if not acc:
        return {"status": "not_initialized", "universe": universe_id,
                "enabled": enabled, "capital": DEFAULT_CAPITAL}

    positions = [dict(r) for r in repo.query(
        "SELECT * FROM fact_paper_position WHERE universe_id=? ORDER BY weight DESC",
        (universe_id,))]
    limit = max(0, min(int(trade_limit), 500))
    trades_sql = ("SELECT * FROM fact_paper_trade WHERE universe_id=? "
                  "ORDER BY trade_date DESC, seq DESC")
    trades = [dict(r) for r in repo.query(
        trades_sql + (" LIMIT ?" if limit else ""),
        tuple([universe_id] + ([limit] if limit else [])))]
    nav = [dict(r) for r in repo.query(
        "SELECT trade_date, total_value, cash, market_value, ret, benchmark, drawdown "
        "FROM fact_paper_nav WHERE universe_id=? ORDER BY trade_date", (universe_id,))]

    capital = float(acc["initial_capital"] or DEFAULT_CAPITAL)
    total = float(acc["total_value"] or capital)
    total_return = total / capital - 1 if capital else 0.0
    days = len(nav)
    annual = ((1 + total_return) ** (252 / days) - 1) if days >= 2 and total_return > -1 else None
    max_dd = min((n["drawdown"] or 0.0) for n in nav) if nav else 0.0

    return {
        "status": "ok", "universe": universe_id, "enabled": enabled,
        "account": {
            "initial_capital": capital, "cash": acc["cash"],
            "total_value": total, "start_date": acc["start_date"],
            "last_date": acc["last_date"], "rebalances": acc["rebalances"],
            "trades_count": acc["trades_count"],
            "exposure": (total - float(acc["cash"] or 0)) / total if total else 0.0,
        },
        "metrics": {"total_return": total_return, "annual_return": annual,
                    "max_drawdown": max_dd, "days": days},
        "positions": positions,
        "trades": trades,
        "nav": nav,
    }
