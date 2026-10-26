"""股票池（C0/C1/...）与时点可交易性过滤。

股票池定义：
- C0：当前板块成分（dim_sector_member），即电网设备 + 天然气
- C1：用户指定股票（config/universe_c1.csv），由 `cli universe --id C1 --sync` 刷新入库
- C2/C3：后续按行业池扩展，同样通过 CSV 录入

用户要求的剔除规则（对所有股票池生效）：
1. ST/*ST/退市整理：名称含 ST/退，或已到退市日；
2. 长期停牌：截至 t 连续 max_suspend_days 个交易日无行情；
3. 上市不足 min_listed_days 个交易日。

所有规则只使用 t 及之前的数据，避免前视偏差；历史 ST 状态受数据源限制（见
docs/a_share_defensive_strategy_enhancement_plan.md 第 10.2 节）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from ..common.logging import logger
from ..storage.repository import Repository

C0 = "C0"
C1 = "C1"
DEFAULT_MAX_SUSPEND_DAYS = 60
DEFAULT_MIN_LISTED_DAYS = 60

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
UNIVERSES_PATH = CONFIG_DIR / "universes.yaml"


def c1_csv_path() -> Path:
    return CONFIG_DIR / "universe_c1.csv"


# ---------- C1 CSV 录入 ----------

def read_c1_csv(path: Path | None = None) -> pd.DataFrame:
    """读取并校验 C1 清单。允许列：symbol/代码, name/名称, sector_code/板块。"""
    p = path or c1_csv_path()
    if not p.exists():
        return pd.DataFrame(columns=["symbol", "name", "sector_code"])
    df = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
    df = df.rename(columns={
        "代码": "symbol", "股票代码": "symbol",
        "名称": "name", "股票名称": "name",
        "板块": "sector_code", "行业": "sector_code",
    })
    if "symbol" not in df.columns:
        raise ValueError(f"{p} 缺少 symbol/代码 列")
    df["symbol"] = df["symbol"].fillna("").str.strip().str.zfill(6)
    df = df[df["symbol"].str.fullmatch(r"\d{6}")]
    if "name" not in df.columns:
        df["name"] = ""
    if "sector_code" not in df.columns:
        df["sector_code"] = "C1"
    df["name"] = df["name"].fillna("").str.strip()
    df["sector_code"] = df["sector_code"].fillna("C1").str.strip().replace("", "C1")
    df = df.drop_duplicates("symbol")
    bad = df[~df["symbol"].str.match(r"^([036]\d{5}|[248]\d{5}|9\d{5}|[15]\d{5})$")]
    if not bad.empty:
        logger.warning(f"C1 清单存在非 A 股/ETF 代码: {', '.join(bad['symbol'])}")
    etf = df[df["symbol"].map(is_etf)]
    if not etf.empty:
        logger.info(f"C1 含 ETF/LOF {len(etf)} 只（无财务数据，基本面因子留空）: "
                    + ", ".join(etf["symbol"]))
    return df[["symbol", "name", "sector_code"]].reset_index(drop=True)


def is_etf(symbol: str) -> bool:
    """ETF/LOF 代码：深市 15/16/18 开头，沪市 5 开头。"""
    return symbol.startswith(("15", "16", "18", "5"))


def refresh_universe(repo: Repository, universe_id: str, df: pd.DataFrame,
                     name: str | None = None, description: str = "",
                     source: str = "csv") -> int:
    """用给定清单整体替换某个股票池（幂等）。返回成员数。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    repo.upsert("dim_universe", [{
        "universe_id": universe_id, "name": name or universe_id,
        "description": description, "updated_at": now,
    }], ["universe_id"])
    # 整体替换：先删后插，保证清单删掉的股票会从池中移除
    repo.conn.execute("DELETE FROM dim_universe_member WHERE universe_id=?", (universe_id,))
    if not df.empty:
        repo.upsert("dim_universe_member", [{
            "universe_id": universe_id, "symbol": r.symbol,
            "sector_code": r.sector_code, "source": source, "updated_at": now,
        } for r in df.itertuples()], ["universe_id", "symbol"])
    repo.conn.commit()
    logger.info(f"股票池 {universe_id} 已刷新：{len(df)} 只")
    return len(df)


def refresh_c1(repo: Repository) -> int:
    """从 config/universe_c1.csv 刷新 C1，并补齐 dim_stock 基础信息。"""
    df = read_c1_csv()
    if df.empty:
        logger.warning(f"C1 清单为空：请在 {c1_csv_path()} 中填入股票代码后重试")
    if not df.empty:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        repo.upsert("dim_stock", [{
            "symbol": r.symbol, "name": r.name or None,
            "market": infer_market(r.symbol), "updated_at": now,
        } for r in df.itertuples()], ["symbol"])
    return refresh_universe(repo, C1, df, name="C1 用户指定池",
                            description="由 config/universe_c1.csv 录入", source="csv")


def refresh_industry_universe(repo: Repository, universe_id: str) -> dict:
    """按 config/universes.yaml 拉取 C2/C3 行业成分并刷新股票池。

    剔除：名称含 ST/退、B 股（900/200 开头）。
    """
    from ..datasource.sina_src import sina_board_members

    cfg = yaml.safe_load(UNIVERSES_PATH.read_text(encoding="utf-8"))
    udef = (cfg.get("universes") or {}).get(universe_id)
    if not udef:
        raise ValueError(f"config/universes.yaml 未定义 {universe_id}")

    frames: list[pd.DataFrame] = []
    stats: dict[str, dict] = {}
    for src in udef.get("sources", []):
        name, label = src["name"], src["sina_label"]
        try:
            df = sina_board_members(label)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"行业成分失败 [{name}/{label}]: {e}")
            stats[name] = {"raw": 0, "kept": 0, "error": str(e)}
            continue
        raw = len(df)
        if src.get("name_filter"):
            df = df[df["name"].astype(str).str.contains(src["name_filter"], regex=True)]
        df = df[~df["name"].astype(str).str.contains("ST|退", regex=True)]
        df = df[~df["symbol"].astype(str).str.startswith(("900", "200"))]
        stats[name] = {"raw": raw, "kept": len(df)}
        if not df.empty:
            frames.append(df.assign(sector_code=name))
        logger.info(f"  {name}: 原始 {raw}，保留 {len(df)}")

    if not frames:
        raise RuntimeError(f"{universe_id} 未获取到任何成分")
    members = pd.concat(frames, ignore_index=True).drop_duplicates("symbol", keep="first")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    repo.upsert("dim_stock", [{
        "symbol": r.symbol, "name": r.name, "market": infer_market(r.symbol),
        "industry": r.sector_code, "updated_at": now,
    } for r in members.itertuples()], ["symbol"])
    refresh_universe(repo, universe_id, members[["symbol", "name", "sector_code"]],
                     name=udef.get("name", universe_id),
                     description=udef.get("description", ""), source="sina")
    stats["total"] = len(members)
    logger.info(f"{universe_id} 行业池已刷新：{len(members)} 只")
    return stats


# ---------- 股票池解析 ----------

def universe_members(repo: Repository, universe_id: str = C0) -> dict[str, str]:
    """返回 {symbol: sector_code}。

    用户决策：不做历史 ST 时点还原，当前名称含 ST/退 的标的直接从所有股票池剔除
    （C0 的原始板块成分表保持不动，这里在解析时过滤）。
    """
    if universe_id == C0:
        rows = repo.query(
            """SELECT m.symbol, m.sector_code, s.name FROM dim_sector_member m
               LEFT JOIN dim_stock s ON s.symbol = m.symbol
               WHERE m.out_date IS NULL OR m.out_date=''
               ORDER BY m.sector_code, m.symbol"""
        )
    else:
        rows = repo.query(
            """SELECT m.symbol, m.sector_code, s.name FROM dim_universe_member m
               LEFT JOIN dim_stock s ON s.symbol = m.symbol
               WHERE m.universe_id=? ORDER BY m.symbol""",
            (universe_id,),
        )
    out: dict[str, str] = {}
    for r in rows:
        name = r["name"] or ""
        if "ST" in name or "退" in name:
            continue
        out.setdefault(r["symbol"], r["sector_code"] or universe_id)
    return out


def universe_symbols(repo: Repository, universe_id: str = C0) -> list[str]:
    return list(universe_members(repo, universe_id).keys())


def prune_universe(repo: Repository, universe_id: str, max_stale_days: int = 365) -> list[dict]:
    """剔除无行情或长期无行情的成员（退市/长期停牌），并标记 dim_stock。

    仅用于 C1/C2/C3；C0 由板块成分派生，不在此处删除。
    """
    if universe_id == C0:
        return []
    members = universe_members(repo, universe_id)
    if not members:
        return []
    ph = ",".join("?" for _ in members)
    rows = {r["symbol"]: r["d"] for r in repo.query(
        f"SELECT symbol, MAX(trade_date) AS d FROM fact_quote WHERE symbol IN ({ph}) GROUP BY symbol",
        tuple(members))}
    latest = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")[0]["d"]
    cutoff = (datetime.strptime(latest, "%Y-%m-%d") - pd.Timedelta(days=max_stale_days)).strftime("%Y-%m-%d")
    removed: list[dict] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for sym in members:
        last = rows.get(sym)
        if last and last >= cutoff:
            continue
        removed.append({"symbol": sym, "last_trade": last})
        repo.conn.execute("DELETE FROM dim_universe_member WHERE universe_id=? AND symbol=?",
                          (universe_id, sym))
        repo.conn.execute("UPDATE dim_stock SET status='delisted', updated_at=? WHERE symbol=?",
                          (now, sym))
    repo.conn.commit()
    if removed:
        logger.info(f"{universe_id} 剔除 {len(removed)} 只无行情/退市标的")
    return removed


def list_universes(repo: Repository) -> list[dict]:
    rows = repo.query("SELECT universe_id, name, description, updated_at FROM dim_universe ORDER BY universe_id")
    out = [dict(r) for r in rows]
    if not any(x["universe_id"] == C0 for x in out):
        out.insert(0, {"universe_id": C0, "name": "C0 当前板块成分",
                       "description": "电网设备 + 天然气", "updated_at": None})
    for x in out:
        x["members"] = len(universe_members(repo, x["universe_id"]))
    return out


# ---------- 可用性过滤 ----------

def _quote_panel(repo: Repository, symbols: list[str], start: str, end: str) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    ph = ",".join("?" for _ in symbols)
    rows = repo.query(
        f"SELECT symbol, trade_date FROM fact_quote "
        f"WHERE trade_date BETWEEN ? AND ? AND symbol IN ({ph})",
        tuple([start, end] + symbols),
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    return df.assign(traded=True).pivot_table(
        index="trade_date", columns="symbol", values="traded", aggfunc="first"
    ).reindex(columns=symbols).fillna(False).astype(bool)


def _consecutive_missing(traded: pd.Series) -> pd.Series:
    """截至当日，连续未成交的交易日数（当日成交则为 0）。"""
    gap = (~traded).astype(int)
    block = (gap != gap.shift()).cumsum()
    return gap * (gap.groupby(block).cumcount() + 1)


def eligibility_panel(repo: Repository, symbols: list[str], dates: list[str] | None = None,
                      max_suspend_days: int = DEFAULT_MAX_SUSPEND_DAYS,
                      min_listed_days: int = DEFAULT_MIN_LISTED_DAYS,
                      exclude_st: bool = True) -> pd.DataFrame:
    """返回 dates × symbols 的 bool 掩码：True 表示当日可进入股票池。

    规则：当日有行情、非 ST/退市、连续停牌 < max_suspend_days、上市 >= min_listed_days。
    """
    if not symbols:
        return pd.DataFrame()
    if dates is None:
        rows = repo.query("SELECT DISTINCT trade_date AS d FROM fact_quote ORDER BY d")
        dates = [r["d"] for r in rows]
    if not dates:
        return pd.DataFrame()

    traded = _quote_panel(repo, symbols, dates[0], dates[-1])
    if traded.empty:
        return pd.DataFrame(False, index=dates, columns=symbols)
    traded = traded.reindex(dates).fillna(False).astype(bool)

    mask = traded.copy()
    # 上市不足 min_listed_days：优先用 IPO 日期，缺失时退化为首个行情日
    pos = pd.Series(range(len(dates)), index=dates)
    ipo_map = {r["symbol"]: r["ipo_date"] for r in repo.query("SELECT symbol, ipo_date FROM dim_stock")}
    for sym in symbols:
        ipo = ipo_map.get(sym)
        if ipo:
            if ipo <= dates[0]:
                continue  # 回测开始前已上市，不设限制
            after = pos.index[pos.index >= ipo]
            base = after[0] if len(after) else None
        else:
            traded_days = traded[sym]
            base = traded_days[traded_days].index[0] if traded_days.any() else None
        if base is None:
            mask[sym] = False
            continue
        listed = pos - pos[base]
        mask[sym] = mask[sym] & (listed >= min_listed_days)
        gap = _consecutive_missing(traded[sym])
        mask[sym] = mask[sym] & (gap < max_suspend_days)

    # 停牌阈值对所有标的生效（含 ipo_date 在回测开始前的标的）
    for sym in symbols:
        gap = _consecutive_missing(traded[sym])
        if int(gap.max() if len(gap) else 0) >= max_suspend_days:
            mask[sym] = mask[sym] & (gap < max_suspend_days)

    if exclude_st:
        rows = repo.query("SELECT symbol, name, out_date FROM dim_stock")
        for r in rows:
            sym = r["symbol"]
            if sym not in mask.columns:
                continue
            name = r["name"] or ""
            if "ST" in name or "退" in name:
                mask[sym] = False
            out_date = r["out_date"] or ""
            if out_date:
                mask.loc[mask.index >= out_date, sym] = False

    return mask


def suspension_report(repo: Repository, symbols: list[str], as_of: str | None = None,
                      max_suspend_days: int = DEFAULT_MAX_SUSPEND_DAYS) -> list[dict]:
    """列出截至 as_of 已连续停牌 >= 阈值的标的（只读）。"""
    rows = repo.query("SELECT DISTINCT trade_date AS d FROM fact_quote ORDER BY d")
    dates = [r["d"] for r in rows]
    if not dates:
        return []
    as_of = as_of or dates[-1]
    traded = _quote_panel(repo, symbols, dates[0], as_of).reindex(dates).fillna(False).astype(bool)
    out = []
    for sym in symbols:
        if sym not in traded.columns:
            continue
        gap = int(_consecutive_missing(traded[sym]).iloc[-1])
        if gap >= max_suspend_days:
            last = traded[sym][traded[sym]].index.max() if traded[sym].any() else None
            out.append({"symbol": sym, "gap_days": gap, "last_trade": last})
    return out


def infer_market(symbol: str) -> str:
    from ..datasource.akshare_src import infer_market as _infer
    return _infer(symbol)
