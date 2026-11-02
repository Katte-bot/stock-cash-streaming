"""自然语言策略：白名单 DSL、指标计算、事件驱动组合回测与 LLM 解析。

流程：用户中文描述 → LLM 输出受控 JSON（白名单指标）→ `validate_spec` 归一化
→ `build_signal_matrix` 生成信号 → `simulate_rule_portfolio` 组合回测。

执行口径（与现有回测一致）：
- 信号在 K 线收盘计算，次一交易日收盘成交；
- 每只目标金额 = 初始资金 / top_n，整手（100 股）买入；
- 持有 hold_days 个交易日后按收盘卖出（跌停顺延）；
- 涨停不可买，停牌/新股/ST 不参与；成本复用 `_a_share_rates`。

说明：本模块只做客观历史统计，不构成投资建议。
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import numpy as np
import pandas as pd

from ..backtest.engine import _a_share_rates, _limit_threshold
from ..backtest.metrics import compute_metrics
from ..common.logging import logger
from ..config.settings import get_settings
from ..research.universe import eligibility_panel, universe_members
from ..storage.repository import Repository

LOT = 100
DEFAULT_TOP_N = 5
DEFAULT_CAPITAL = 100_000.0
COST_RATE = 0.0013
MAX_HOLD_DAYS = 60
MAX_CONDITIONS = 5

_OPS = {"<": "lt", ">": "gt", "<=": "le", ">=": "ge", "≤": "le", "≥": "ge"}
_DIRECTIONS = {"golden": "golden", "death": "death", "金叉": "golden", "死叉": "death"}
_SIDES = {"lower": "lower", "upper": "upper", "下轨": "lower", "上轨": "upper"}


class SpecError(Exception):
    """DSL 不合法。"""


class StrategyParseError(Exception):
    """自然语言解析失败（LLM 未配置/返回非法）。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _as_int(v, name: str, lo: int, hi: int, default: int | None = None) -> int:
    if v is None:
        if default is None:
            raise SpecError(f"{name} 缺失")
        return default
    try:
        iv = int(float(v))
    except (TypeError, ValueError) as e:
        raise SpecError(f"{name} 必须是整数") from e
    if not (lo <= iv <= hi):
        raise SpecError(f"{name} 需在 {lo}~{hi} 之间")
    return iv


def _as_float(v, name: str, lo: float, hi: float, default: float) -> float:
    if v is None:
        return default
    try:
        fv = float(v)
    except (TypeError, ValueError) as e:
        raise SpecError(f"{name} 必须是数字") from e
    if not (lo <= fv <= hi):
        raise SpecError(f"{name} 需在 {lo}~{hi} 之间")
    return fv


def _as_op(v) -> str:
    op = _OPS.get(str(v or "").strip())
    if not op:
        raise SpecError(f"op 只能是 <, >, <=, >=，收到 {v}")
    return op


def _as_enum(v, mapping: dict, name: str, default: str) -> str:
    key = str(v or default).strip()
    out = mapping.get(key)
    if not out:
        raise SpecError(f"{name} 只能是 {list(mapping)}，收到 {v}")
    return out


def _validate_condition(cond: dict) -> dict:
    if not isinstance(cond, dict):
        raise SpecError("条件必须是对象")
    ind = str(cond.get("indicator") or "").strip()
    params = cond.get("params") or {}
    if not isinstance(params, dict):
        raise SpecError(f"{ind} 的 params 必须是对象")

    if ind in ("consecutive_down", "consecutive_up"):
        return {"indicator": ind, "params": {"n": _as_int(params.get("n"), "n", 2, 120, 3)}}
    if ind == "macd_cross":
        fast = _as_int(params.get("fast"), "fast", 2, 60, 12)
        slow = _as_int(params.get("slow"), "slow", 3, 120, 26)
        if fast >= slow:
            raise SpecError("MACD 的 fast 必须小于 slow")
        return {"indicator": ind, "params": {
            "fast": fast, "slow": slow,
            "signal": _as_int(params.get("signal"), "signal", 2, 30, 9),
            "direction": _as_enum(params.get("direction"), _DIRECTIONS, "direction", "golden")}}
    if ind == "ma_cross":
        fast = _as_int(params.get("fast"), "fast", 2, 120, 5)
        slow = _as_int(params.get("slow"), "slow", 3, 250, 20)
        if fast >= slow:
            raise SpecError("均线的 fast 必须小于 slow")
        return {"indicator": ind, "params": {
            "fast": fast, "slow": slow,
            "direction": _as_enum(params.get("direction"), _DIRECTIONS, "direction", "golden")}}
    if ind == "rsi":
        return {"indicator": ind, "params": {
            "n": _as_int(params.get("n"), "n", 2, 60, 14),
            "op": _as_op(params.get("op") or "<"),
            "value": _as_float(params.get("value"), "value", 0, 100, 30)}}
    if ind == "return_pct":
        return {"indicator": ind, "params": {
            "n": _as_int(params.get("n"), "n", 1, 250, 5),
            "op": _as_op(params.get("op") or "<"),
            "value": _as_float(params.get("value"), "value", -100, 100, -10)}}
    if ind == "close_vs_ma":
        return {"indicator": ind, "params": {
            "n": _as_int(params.get("n"), "n", 2, 250, 20),
            "op": _as_op(params.get("op") or "<"),
            "value": _as_float(params.get("value"), "value", -100, 100, 0)}}
    if ind == "boll":
        return {"indicator": ind, "params": {
            "n": _as_int(params.get("n"), "n", 5, 120, 20),
            "k": _as_float(params.get("k"), "k", 1, 4, 2.0),
            "side": _as_enum(params.get("side"), _SIDES, "side", "lower")}}
    if ind == "volume_shrink":
        return {"indicator": ind, "params": {
            "n": _as_int(params.get("n"), "n", 2, 60, 5),
            "ratio": _as_float(params.get("ratio"), "ratio", 0.1, 1.0, 0.7)}}
    raise SpecError(f"暂不支持的指标：{ind or '(空)'}")


def validate_spec(raw: dict) -> dict:
    """校验并归一化 DSL。"""
    if not isinstance(raw, dict):
        raise SpecError("策略必须是 JSON 对象")
    timeframe = str(raw.get("timeframe") or "D").strip().upper()
    if timeframe not in ("D", "W"):
        raise SpecError("timeframe 只能是 D（日线）或 W（周线）")

    entry = raw.get("entry")
    if isinstance(entry, list):
        conds = entry
    elif isinstance(entry, dict):
        conds = entry.get("all")
    else:
        conds = None
    if not isinstance(conds, list) or not conds:
        raise SpecError("entry.all 不能为空")
    if len(conds) > MAX_CONDITIONS:
        raise SpecError(f"条件最多 {MAX_CONDITIONS} 个")

    hold = _as_int(raw.get("hold_days"), "hold_days", 1, MAX_HOLD_DAYS, 5)
    name = str(raw.get("name") or "未命名策略").strip()[:40]
    return {"name": name, "timeframe": timeframe,
            "entry": {"all": [_validate_condition(c) for c in conds]},
            "hold_days": hold}


# ---------- 指标计算（K 线级） ----------

def _compare(df: pd.DataFrame, op: str, value: float) -> pd.DataFrame:
    if op == "lt":
        return df < value
    if op == "gt":
        return df > value
    if op == "le":
        return df <= value
    return df >= value


def _indicator(close: pd.DataFrame, volume: pd.DataFrame, cond: dict) -> pd.DataFrame:
    ind, p = cond["indicator"], cond["params"]

    if ind in ("consecutive_down", "consecutive_up"):
        n = p["n"]
        flag = (close.diff() < 0) if ind == "consecutive_down" else (close.diff() > 0)
        return (flag.rolling(n).sum() == n).fillna(False)

    if ind == "macd_cross":
        dif = (close.ewm(span=p["fast"], adjust=False).mean()
               - close.ewm(span=p["slow"], adjust=False).mean())
        dea = dif.ewm(span=p["signal"], adjust=False).mean()
        if p["direction"] == "golden":
            cross = (dif > dea) & (dif.shift(1) <= dea.shift(1))
        else:
            cross = (dif < dea) & (dif.shift(1) >= dea.shift(1))
        return cross.fillna(False)

    if ind == "ma_cross":
        fast = close.rolling(p["fast"]).mean()
        slow = close.rolling(p["slow"]).mean()
        if p["direction"] == "golden":
            cross = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        else:
            cross = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        return cross.fillna(False)

    if ind == "rsi":
        delta = close.diff()
        up = delta.clip(lower=0).rolling(p["n"]).mean()
        down = (-delta.clip(upper=0)).rolling(p["n"]).mean()
        rs = up / down.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        return _compare(rsi, p["op"], p["value"]).fillna(False)

    if ind == "return_pct":
        ret = close.pct_change(p["n"]) * 100
        return _compare(ret, p["op"], p["value"]).fillna(False)

    if ind == "close_vs_ma":
        dev = (close / close.rolling(p["n"]).mean() - 1) * 100
        return _compare(dev, p["op"], p["value"]).fillna(False)

    if ind == "boll":
        ma = close.rolling(p["n"]).mean()
        sd = close.rolling(p["n"]).std()
        if p["side"] == "lower":
            return (close <= ma - p["k"] * sd).fillna(False)
        return (close >= ma + p["k"] * sd).fillna(False)

    if ind == "volume_shrink":
        ratio = volume.rolling(p["n"]).mean() / volume.rolling(20).mean()
        return (ratio < p["ratio"]).fillna(False)

    raise SpecError(f"暂不支持的指标：{ind}")


def resample_bars(close: pd.DataFrame, volume: pd.DataFrame, timeframe: str):
    """日线 → 目标周期。周线的 bar 标签取该周最后一个交易日。"""
    if timeframe == "D" or close.empty:
        return close, volume, list(close.index)
    dt = pd.to_datetime(pd.Index(close.index))
    period = dt.to_period("W-FRI")
    c = close.copy()
    c.index = period
    c = c.groupby(level=0).last()
    v = volume.copy()
    v.index = period
    v = v.groupby(level=0).sum(min_count=1)
    last = pd.Series(dt, index=period).groupby(level=0).max()
    bar_end = last.dt.strftime("%Y-%m-%d").tolist()
    c.index = bar_end
    v.index = bar_end
    return c, v, bar_end


def build_signal_matrix(close: pd.DataFrame, volume: pd.DataFrame, spec: dict) -> pd.DataFrame:
    """按条件组（all）在 K 线级生成信号矩阵。"""
    mask = pd.DataFrame(True, index=close.index, columns=close.columns)
    for cond in spec["entry"]["all"]:
        m = _indicator(close, volume, cond)
        mask &= m.reindex_like(mask).fillna(False).astype(bool)
    return mask


def to_execution_signals(signals: pd.DataFrame, trading_dates: list[str]) -> pd.DataFrame:
    """K 线信号 → 次一交易日执行。"""
    out = pd.DataFrame(False, index=trading_dates, columns=signals.columns)
    pos = {d: i for i, d in enumerate(trading_dates)}
    for bar_date in signals.index:
        i = pos.get(bar_date)
        if i is None or i + 1 >= len(trading_dates):
            continue
        nxt = trading_dates[i + 1]
        out.loc[nxt] = out.loc[nxt] | signals.loc[bar_date].reindex(out.columns).fillna(False)
    return out


# ---------- 组合回测 ----------

def simulate_rule_portfolio(close: pd.DataFrame, pct: pd.DataFrame,
                            signals: pd.DataFrame,
                            eligibility: pd.DataFrame | None = None,
                            capital: float = DEFAULT_CAPITAL,
                            top_n: int = DEFAULT_TOP_N,
                            hold_days: int = 5) -> dict:
    """事件驱动：到期卖出 → 信号买入 → 每日估值。"""
    if close is None or close.empty:
        return {"nav": [], "trades": [], "trade_stats": {}, "status": "waiting"}

    dates = list(close.index)
    symbols = list(close.columns)
    cash = float(capital)
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    nav_rows: list[dict] = []
    peak = float(capital)
    max_concurrent = 0
    slot_value = float(capital) / max(1, top_n)

    for i, d in enumerate(dates):
        px = close.loc[d]
        pct_d = pct.loc[d] if d in pct.index else pd.Series(dtype=float)

        # 1) 到期卖出
        for sym in list(positions):
            pos = positions[sym]
            if i - pos["entry_idx"] < hold_days:
                continue
            price = px.get(sym)
            if pd.isna(price) or float(price) <= 0:
                continue
            if float(pct_d.get(sym, 0) or 0) <= -_limit_threshold(sym, d, "board"):
                continue  # 跌停不可卖，顺延
            amount = pos["shares"] * float(price)
            _, sell_rate = _a_share_rates(d, COST_RATE)
            fee = amount * sell_rate
            cash += amount - fee
            pnl = (amount - fee) / pos["cost"] - 1
            trades.append({
                "symbol": sym, "entry_date": pos["entry_date"], "exit_date": d,
                "entry_price": round(pos["entry_price"], 4), "exit_price": round(float(price), 4),
                "shares": pos["shares"], "hold_days": i - pos["entry_idx"],
                "pnl": round(float(pnl), 6),
            })
            del positions[sym]

        # 2) 信号买入
        slots = top_n - len(positions)
        if slots > 0 and d in signals.index:
            row = signals.loc[d]
            elig = eligibility.loc[d] if (eligibility is not None and d in eligibility.index) else None
            cands: list[str] = []
            for sym in symbols:
                if sym in positions or not bool(row.get(sym, False)):
                    continue
                if elig is not None and not bool(elig.get(sym, False)):
                    continue
                price = px.get(sym)
                if pd.isna(price) or float(price) <= 0:
                    continue
                if float(pct_d.get(sym, 0) or 0) >= _limit_threshold(sym, d, "board"):
                    continue  # 涨停不可买
                cands.append(sym)
            for sym in sorted(cands)[:slots]:
                price = float(px[sym])
                lots = int(slot_value / (price * LOT)) * LOT
                buy_rate, _ = _a_share_rates(d, COST_RATE)
                while lots > 0 and lots * price * (1 + buy_rate) > cash:
                    lots -= LOT
                if lots <= 0:
                    continue
                amount = lots * price
                fee = amount * buy_rate
                cash -= amount + fee
                positions[sym] = {"shares": lots, "entry_idx": i, "entry_date": d,
                                  "entry_price": price, "cost": amount + fee}
                max_concurrent = max(max_concurrent, len(positions))

        # 3) 每日估值
        mv = sum(p["shares"] * float(px.get(s, 0) or 0) for s, p in positions.items())
        total = cash + mv
        peak = max(peak, total)
        nav_rows.append({"trade_date": d, "nav": round(total, 2), "cash": round(cash, 2),
                         "market_value": round(mv, 2),
                         "drawdown": round(total / peak - 1, 6)})

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    stats = {
        "trades": len(trades),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "avg_win": float(np.mean([t["pnl"] for t in wins])) if wins else 0.0,
        "avg_loss": float(np.mean([t["pnl"] for t in losses])) if losses else 0.0,
        "avg_hold_days": float(np.mean([t["hold_days"] for t in trades])) if trades else 0.0,
        "max_concurrent": int(max_concurrent),
    }
    return {"status": "ok" if nav_rows else "waiting",
            "nav": nav_rows, "trades": trades, "trade_stats": stats,
            "open_positions": len(positions), "cash": round(cash, 2),
            "total_value": nav_rows[-1]["nav"] if nav_rows else round(capital, 2)}


def _load_panels(repo: Repository, symbols: list[str], start: str | None):
    ph = ",".join("?" for _ in symbols)
    rows = repo.query(
        f"SELECT symbol, trade_date, close, pct_chg, volume FROM fact_quote "
        f"WHERE symbol IN ({ph}) ORDER BY trade_date", tuple(symbols))
    if not rows:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    close = df.pivot(index="trade_date", columns="symbol", values="close").sort_index().ffill()
    pct = df.pivot(index="trade_date", columns="symbol", values="pct_chg").sort_index().fillna(0.0)
    volume = df.pivot(index="trade_date", columns="symbol", values="volume").sort_index().fillna(0.0)
    if start:
        close, pct, volume = close[close.index >= start], pct[pct.index >= start], volume[volume.index >= start]
    return close, pct, volume


def _benchmark(close: pd.DataFrame) -> pd.Series:
    bases = {}
    for c in close.columns:
        s = close[c].dropna()
        if len(s):
            bases[c] = float(s.iloc[0])
    if not bases:
        return pd.Series(1.0, index=close.index)
    return close.divide(pd.Series(bases), axis=1).mean(axis=1, skipna=True).fillna(1.0)


def run_nl_backtest(repo: Repository, spec: dict, universe: str,
                    top_n: int = DEFAULT_TOP_N, start: str | None = None,
                    end: str | None = None,
                    capital: float = DEFAULT_CAPITAL) -> dict:
    """执行一次规则回测并返回结果（不落库）。"""
    spec = validate_spec(spec)
    members = universe_members(repo, universe)
    if not members:
        raise SpecError(f"股票池 {universe} 为空")
    symbols = list(members)
    close, pct, volume = _load_panels(repo, symbols, start)
    if close.empty:
        raise SpecError("无行情数据")
    if end:
        close, pct, volume = close[close.index <= end], pct[pct.index <= end], volume[volume.index <= end]
    if len(close) < 30:
        raise SpecError("回测区间过短（至少 30 个交易日）")

    bars_c, bars_v, _ = resample_bars(close, volume, spec["timeframe"])
    bar_signals = build_signal_matrix(bars_c, bars_v, spec)
    exec_signals = to_execution_signals(bar_signals, list(close.index))

    elig = eligibility_panel(repo, [s for s in symbols if s in close.columns],
                             dates=list(close.index))
    result = simulate_rule_portfolio(close, pct, exec_signals, elig,
                                     capital=capital, top_n=top_n,
                                     hold_days=spec["hold_days"])

    nav = pd.Series([x["nav"] for x in result["nav"]],
                    index=[x["trade_date"] for x in result["nav"]])
    bench = _benchmark(close).reindex(nav.index).ffill()
    bench_value = bench * float(capital)
    metrics = compute_metrics(nav, bench_value) if len(nav) > 2 else {}
    for row, b in zip(result["nav"], bench_value.tolist()):
        row["benchmark"] = round(float(b), 2)

    return {
        "spec": spec, "describe": describe_spec(spec), "universe": universe,
        "params": {"top_n": int(top_n), "start": str(close.index[0]),
                   "end": str(close.index[-1]), "hold_days": spec["hold_days"],
                   "timeframe": spec["timeframe"], "capital": float(capital)},
        "metrics": metrics, "trade_stats": result["trade_stats"],
        "nav": result["nav"], "trades": result["trades"],
        "open_positions": result.get("open_positions", 0),
    }


def describe_spec(spec: dict) -> list[str]:
    """把 DSL 渲染成中文规则列表。"""
    tf = "周线" if spec["timeframe"] == "W" else "日线"
    lines = [f"周期：{tf}", f"持仓：{spec['hold_days']} 个交易日"]
    conds = []
    for c in spec["entry"]["all"]:
        ind, p = c["indicator"], c["params"]
        if ind == "consecutive_down":
            conds.append(f"连续 {p['n']} 根阴线")
        elif ind == "consecutive_up":
            conds.append(f"连续 {p['n']} 根阳线")
        elif ind == "macd_cross":
            conds.append(f"MACD {'金叉' if p['direction'] == 'golden' else '死叉'}"
                         f"（{p['fast']}/{p['slow']}/{p['signal']}）")
        elif ind == "ma_cross":
            conds.append(f"MA{p['fast']} {'上穿' if p['direction'] == 'golden' else '下穿'} "
                         f"MA{p['slow']}")
        elif ind == "rsi":
            conds.append(f"RSI{p['n']} {p['op']} {p['value']:g}")
        elif ind == "return_pct":
            conds.append(f"近 {p['n']} 根涨跌幅 {p['op']} {p['value']:g}%")
        elif ind == "close_vs_ma":
            conds.append(f"收盘相对 MA{p['n']} 偏离 {p['op']} {p['value']:g}%")
        elif ind == "boll":
            conds.append(f"触及布林{'下轨' if p['side'] == 'lower' else '上轨'}"
                         f"（{p['n']}, {p['k']:g}σ）")
        elif ind == "volume_shrink":
            conds.append(f"{p['n']} 日均量 / 20 日均量 < {p['ratio']:g}")
    lines.append("买入条件（全部满足）：" + "；".join(conds))
    return lines


# ---------- LLM 解析 ----------

_SYSTEM_PROMPT = """你是 A 股量化策略解析器。把用户的中文策略描述转换成 JSON（只输出 JSON，不要解释、不要 Markdown）。

输出格式：
{
  "name": "中文策略名（≤20 字）",
  "timeframe": "D" 或 "W",
  "entry": {"all": [ {"indicator": "...", "params": {...}} ]},
  "hold_days": 整数（1~60，买入后持有的交易日数）
}

可用指标（只能使用这些，不能发明新指标）：
1. consecutive_down  {"n": 2~120}           连续 n 根阴线（收盘低于前收）
2. consecutive_up    {"n": 2~120}           连续 n 根阳线
3. macd_cross        {"fast":12,"slow":26,"signal":9,"direction":"golden"|"death"}
4. ma_cross          {"fast":5,"slow":20,"direction":"golden"|"death"}
5. rsi               {"n":14,"op":"<"|">"|"<="|">=","value":0~100}
6. return_pct        {"n":5,"op":"<"|">"|"<="|">=","value":-100~100}   近 n 根涨跌幅（%）
7. close_vs_ma       {"n":20,"op":"<"|">"|"<="|">=","value":-100~100}   收盘相对均线偏离（%）
8. boll              {"n":20,"k":2.0,"side":"lower"|"upper"}
9. volume_shrink     {"n":5,"ratio":0.1~1.0}

规则：
- “金叉”对应 direction=golden，“死叉”对应 death；
- 用户未给参数时使用上面的默认值；
- 持仓周期换算：1 周 = 5 个交易日，2 周 = 10，1 个月 = 20；
- 若描述包含多个买入条件，全部放入 entry.all（最多 5 个，表示同时满足）；
- 无法用上述指标表达时，选择最接近的指标，不要输出额外字段。
"""


def parse_nl_strategy(text: str) -> dict:
    """中文描述 → 归一化 DSL。"""
    text = (text or "").strip()
    if not text:
        raise SpecError("请先输入策略描述")
    cfg = get_settings()
    if not cfg.openai_api_key:
        raise StrategyParseError(
            "未配置 LLM（OPENAI_API_KEY），可在「手动编辑 JSON」中填写规则后回测")

    try:
        from openai import OpenAI

        client = OpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url)
        resp = client.chat.completions.create(
            model=cfg.llm_model,
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": text}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        raise StrategyParseError(f"LLM 调用失败：{e}") from e

    # 兼容模型偶尔包裹 ```json 代码块
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"LLM 返回非 JSON：{raw[:200]}")
        raise StrategyParseError("LLM 返回的内容不是合法 JSON，请换一种说法") from e
    try:
        return validate_spec(data)
    except SpecError as e:
        raise StrategyParseError(f"LLM 返回的规则不可用：{e}") from e


# ---------- 策略存取 ----------

def _new_id(repo: Repository) -> str:
    ids = [r["strategy_id"] for r in repo.query("SELECT strategy_id FROM dim_nl_strategy")]
    nums = [int(x[2:]) for x in ids if re.fullmatch(r"NL\d+", x or "")]
    return f"NL{max(nums, default=0) + 1}"


def summarize_result(result: dict, universe: str) -> dict:
    m = result.get("metrics") or {}
    ts = result.get("trade_stats") or {}
    p = result.get("params") or {}
    return {
        "universe": universe, "top_n": p.get("top_n"),
        "start": p.get("start"), "end": p.get("end"),
        "total_return": m.get("total_return"), "annual_return": m.get("annual_return"),
        "sharpe": m.get("sharpe"), "max_drawdown": m.get("max_drawdown"),
        "trades": ts.get("trades"), "win_rate": ts.get("win_rate"),
        "ran_at": _now(),
    }


def save_strategy(repo: Repository, spec: dict, nl_text: str, universe: str,
                  result: dict, name: str | None = None) -> dict:
    sid = _new_id(repo)
    now = _now()
    repo.upsert("dim_nl_strategy", [{
        "strategy_id": sid, "name": (name or spec.get("name") or "未命名策略")[:40],
        "nl_text": nl_text or "", "spec_json": json.dumps(spec, ensure_ascii=False),
        "universe_id": universe,
        "last_result": json.dumps(summarize_result(result, universe), ensure_ascii=False),
        "last_run_at": now, "created_at": now, "updated_at": now,
    }], ["strategy_id"])
    return get_strategy(repo, sid)


def update_strategy_result(repo: Repository, strategy_id: str, universe: str,
                           result: dict) -> dict:
    now = _now()
    repo.conn.execute(
        "UPDATE dim_nl_strategy SET universe_id=?, last_result=?, last_run_at=?, updated_at=? "
        "WHERE strategy_id=?",
        (universe, json.dumps(summarize_result(result, universe), ensure_ascii=False),
         now, now, strategy_id))
    repo.conn.commit()
    return get_strategy(repo, strategy_id)


def _row_to_dict(row) -> dict:
    out = dict(row)
    try:
        out["spec"] = json.loads(out.pop("spec_json") or "{}")
    except (TypeError, ValueError):
        out["spec"] = {}
    try:
        out["last_summary"] = json.loads(out.get("last_result") or "null")
    except (TypeError, ValueError):
        out["last_summary"] = None
    out.pop("last_result", None)
    try:
        out["describe"] = describe_spec(out["spec"])
    except (SpecError, KeyError, TypeError):
        out["describe"] = []
    return out


def list_strategies(repo: Repository) -> list[dict]:
    return [_row_to_dict(r) for r in repo.query(
        "SELECT * FROM dim_nl_strategy ORDER BY created_at DESC")]


def get_strategy(repo: Repository, strategy_id: str) -> dict | None:
    rows = repo.query("SELECT * FROM dim_nl_strategy WHERE strategy_id=?", (strategy_id,))
    return _row_to_dict(rows[0]) if rows else None


def delete_strategy(repo: Repository, strategy_id: str) -> bool:
    cur = repo.conn.execute("DELETE FROM dim_nl_strategy WHERE strategy_id=?", (strategy_id,))
    repo.conn.commit()
    return cur.rowcount > 0
