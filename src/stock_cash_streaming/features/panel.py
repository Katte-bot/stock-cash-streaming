"""统一行情/可交易性面板。

一次加载，多策略复用：
- 行情宽表（close/ret/pct/amount/turnover/volume）；
- 可交易性掩码（ST/退市/停牌/上市天数/流动性）；
- 行业映射（申万一级，缺失回退 THS 板块）。

严格口径：面板只包含回测区间内的数据；所有滚动因子在面板上计算，
不使用区间之外的未来数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from ..common.logging import logger
from ..storage.repository import Repository
from ..ingest.industry import industry_map

DEFAULT_MIN_LISTED_DAYS = 60
A0_MIN_LISTED_DAYS = 250
A0_MIN_AMOUNT_20D = 2_000_000.0
SUSPEND_DAYS = 20


@dataclass
class Panel:
    close: pd.DataFrame
    ret: pd.DataFrame
    pct: pd.DataFrame
    amount: pd.DataFrame
    turnover: pd.DataFrame
    volume: pd.DataFrame
    eligibility: pd.DataFrame
    open: pd.DataFrame | None = None
    high: pd.DataFrame | None = None
    low: pd.DataFrame | None = None
    sector_map: dict[str, str] = field(default_factory=dict)
    st_symbols: set[str] = field(default_factory=set)
    universe: str | None = None

    @property
    def dates(self) -> list[str]:
        return list(self.close.index)

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)

    def slice(self, start: str | None = None, end: str | None = None) -> "Panel":
        idx = self.close.index
        if start:
            idx = idx[idx >= start]
        if end:
            idx = idx[idx <= end]
        return Panel(
            close=self.close.loc[idx], ret=self.ret.loc[idx], pct=self.pct.loc[idx],
            amount=self.amount.loc[idx], turnover=self.turnover.loc[idx],
            volume=self.volume.loc[idx], eligibility=self.eligibility.loc[idx],
            open=None if self.open is None else self.open.loc[idx],
            high=None if self.high is None else self.high.loc[idx],
            low=None if self.low is None else self.low.loc[idx],
            sector_map=self.sector_map, st_symbols=self.st_symbols, universe=self.universe,
        )

    def sector_return(self) -> pd.DataFrame:
        """行业等权日收益（用行业映射分组）。"""
        groups: dict[str, list[str]] = {}
        for sym in self.ret.columns:
            groups.setdefault(self.sector_map.get(sym, "UNKNOWN"), []).append(sym)
        out = pd.DataFrame(index=self.ret.index)
        for name, syms in groups.items():
            out[name] = self.ret[syms].mean(axis=1, skipna=True)
        return out


def _symbols_for(repo: Repository, universe: str | None) -> list[str]:
    if universe and universe != "A0":
        from ..research.universe import universe_members

        members = universe_members(repo, universe)
        return list(members) if members else []
    rows = repo.query("SELECT symbol FROM dim_stock ORDER BY symbol")
    return [r["symbol"] for r in rows]


QUOTE_FIELDS = ("close", "pct_chg", "amount", "turnover", "volume", "open", "high", "low")


def _load_quote_panels(repo: Repository, symbols: list[str], start: str | None,
                       end: str | None) -> dict[str, pd.DataFrame]:
    """分块读取行情并就地透视成宽表。

    全 A（约 5200 只 × 2100 日 = 1100 万行）如果一次性读成长表，
    DataFrame 的字符串列会吃掉数 GB 内存；这里按 600 只一段读取、立即透视，
    峰值只与单段行数成正比。
    """
    if not symbols:
        return {}
    parts: dict[str, list[pd.DataFrame]] = {c: [] for c in QUOTE_FIELDS}
    chunk = 600  # 兼顾 SQLite 变量上限与单段内存
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        ph = ",".join("?" for _ in part)
        sql = (f"SELECT symbol, trade_date, {', '.join(QUOTE_FIELDS)} "
               f"FROM fact_quote WHERE symbol IN ({ph})")
        params: list = list(part)
        if start:
            sql += " AND trade_date >= ?"
            params.append(start)
        if end:
            sql += " AND trade_date <= ?"
            params.append(end)
        df = pd.read_sql(sql, repo.conn, params=params)
        if df.empty:
            continue
        for c in QUOTE_FIELDS:
            parts[c].append(df.pivot(index="trade_date", columns="symbol", values=c))
        del df
    return {c: pd.concat(v, axis=1).sort_index().astype("float32")
            for c, v in parts.items() if v}


def _eligibility(repo: Repository, close: pd.DataFrame, st_symbols: set[str],
                 min_listed_days: int) -> pd.DataFrame:
    traded = close.notna()
    idx = close.index
    mask = traded.copy()
    pos = pd.Series(range(len(idx)), index=idx)

    ipo_map = {r["symbol"]: r["ipo_date"] for r in repo.query(
        "SELECT symbol, ipo_date FROM dim_stock")}
    out_map = {r["symbol"]: r["out_date"] for r in repo.query(
        "SELECT symbol, out_date FROM dim_stock")}

    for sym in close.columns:
        if sym in st_symbols:
            mask[sym] = False
            continue
        ipo = ipo_map.get(sym)
        if ipo and ipo > idx[0]:
            after = pos.index[pos.index >= ipo]
            base = after[0] if len(after) else None
        elif ipo:
            base = idx[0]
        else:
            s = traded[sym]
            base = s[s].index[0] if s.any() else None
        if base is None:
            mask[sym] = False
            continue
        listed = pos - pos[base]
        mask[sym] = mask[sym] & (listed >= min_listed_days)
        out_date = out_map.get(sym)
        if out_date:
            mask.loc[mask.index >= out_date, sym] = False

    # 连续停牌 >= 20 个交易日：滚动窗口全缺失
    missing = (~traded).astype("int8")
    susp = missing.rolling(SUSPEND_DAYS, min_periods=SUSPEND_DAYS).sum() >= SUSPEND_DAYS
    mask &= ~susp
    return mask.fillna(False).astype(bool)


def load_panel(repo: Repository, universe: str | None = None,
               start: str | None = None, end: str | None = None,
               min_listed_days: int | None = None,
               liquidity_floor: float | None = None,
               symbols: list[str] | None = None) -> Panel:
    """加载统一面板。A0 默认上市 ≥250 日、20 日均额 ≥2000 万。"""
    if min_listed_days is None:
        min_listed_days = A0_MIN_LISTED_DAYS if universe == "A0" else DEFAULT_MIN_LISTED_DAYS
    if liquidity_floor is None:
        liquidity_floor = A0_MIN_AMOUNT_20D if universe == "A0" else 0.0

    syms = symbols or _symbols_for(repo, universe)
    wide = _load_quote_panels(repo, syms, start, end)
    if not wide:
        empty = pd.DataFrame()
        return Panel(empty, empty, empty, empty, empty, empty, empty, universe=universe)

    close = wide["close"]
    pct, amount, turnover = wide["pct_chg"], wide["amount"], wide["turnover"]
    volume, op, high, low = wide["volume"], wide["open"], wide["high"], wide["low"]

    # 数据源收盘后分批发布日线：末日覆盖不足（低于近 20 日覆盖中位数的一半）时剔除，
    # 避免半天的行情进入研究面板与因子缓存（注意用未 ffill 的原始收盘价统计覆盖）。
    cov = close.notna().sum(axis=1)
    if len(cov) > 21:
        med = float(cov.iloc[-21:-1].median())
        drop: list[str] = []
        for d, n in cov.iloc[::-1].items():
            if med and n < med * 0.5:
                drop.append(str(d))
            else:
                break
        if drop:
            logger.warning(
                f"剔除行情不完整的交易日 {', '.join(reversed(drop))}"
                f"（最新覆盖 {int(cov.iloc[-1])}/{int(med)} 只）")
            keep = close.index.difference(pd.Index(drop))
            close, pct, amount, turnover, volume, op, high, low = (
                x.loc[keep] for x in (close, pct, amount, turnover, volume, op, high, low))
    close = close.ffill()

    ret = close.pct_change().replace([np.inf, -np.inf], np.nan).astype("float32")

    # 数据质量：A 股单日涨跌幅上限为 20%（新股首日无前收盘，收益率为 NaN），
    # 出现 |日收益| > 50% 说明复权基准混用等价格断点，整只剔除，避免污染回测与因子。
    bad = (ret.abs() > 0.5).any(axis=0)
    if bad.any():
        keep = list(ret.columns[~bad])
        logger.warning(
            f"剔除价格异常标的 {int(bad.sum())} 只（日收益 > 50%）："
            f"{', '.join(list(ret.columns[bad])[:8])}…")
        close, pct = close[keep], pct[keep]
        amount, turnover, volume = amount[keep], turnover[keep], volume[keep]
        op, high, low, ret = op[keep], high[keep], low[keep], ret[keep]
    st_symbols = {r["symbol"] for r in repo.query("SELECT symbol, name FROM dim_stock")
                  if "ST" in (r["name"] or "") or "退" in (r["name"] or "")}
    eligibility = _eligibility(repo, close, st_symbols, min_listed_days)

    if liquidity_floor > 0:
        amt20 = amount.rolling(20, min_periods=10).mean()
        eligibility &= (amt20 >= liquidity_floor).fillna(False)

    panel = Panel(
        close=close, ret=ret, pct=pct, amount=amount, turnover=turnover,
        volume=volume, eligibility=eligibility, open=op, high=high, low=low,
        sector_map=industry_map(repo), st_symbols=st_symbols, universe=universe,
    )
    logger.info(
        f"面板加载：{panel.close.shape[0]} 日 × {panel.close.shape[1]} 只 "
        f"({close.index[0]} ~ {close.index[-1]})"
    )
    return panel


def coverage_summary(panel: Panel) -> dict:
    """面板覆盖度（供研究页提示）。"""
    if panel.close.empty:
        return {"dates": 0, "symbols": 0, "eligible_last": 0}
    last = panel.eligibility.iloc[-1]
    return {
        "dates": int(panel.close.shape[0]),
        "symbols": int(panel.close.shape[1]),
        "eligible_last": int(last.sum()),
        "start": str(panel.close.index[0]),
        "end": str(panel.close.index[-1]),
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
