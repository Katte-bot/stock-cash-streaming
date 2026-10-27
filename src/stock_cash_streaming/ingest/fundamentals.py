"""时点财务/分红数据入库（Baostock + 新浪财务摘要）。

数据用途：
- 财务质量、现金流质量、估值、股息可持续性因子的原始指标；
- 所有记录带**公告日**（pub_date / announce_date），选股只允许使用 t 日之前已公告的数据。

来源与口径：
- 指标值：新浪 `stock_financial_abstract`（一次请求返回全部报告期）；
- 公告日：Baostock `query_profit_data` 的 pubDate（缺失时按法定披露截止日兜底）；
- 分红：Baostock `query_dividend_data`（含预案公告日/除权日）；
- 上市/退市日：Baostock `query_stock_basic`。

单位：金额为元；ROE/毛利率/净利率/资产负债率为百分数。原始值入库，不在此处做标准化。
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd

from ..common.logging import logger
from ..storage.repository import Repository
from .disclosure import fetch_dividends, fetch_ipo_date, fetch_report_pub_dates

# 新浪财务摘要中需要保留的指标：(选项, 指标) -> 入库名
METRIC_MAP = {
    ("常用指标", "归母净利润"): "net_profit",
    ("常用指标", "营业总收入"): "revenue",
    ("常用指标", "股东权益合计(净资产)"): "equity",
    ("常用指标", "经营现金流量净额"): "ocf",
    ("常用指标", "基本每股收益"): "eps",
    ("常用指标", "每股净资产"): "bps",
    ("常用指标", "净资产收益率(ROE)"): "roe",
    ("常用指标", "毛利率"): "gross_margin",
    ("常用指标", "销售净利率"): "net_margin",
    ("常用指标", "资产负债率"): "liability_to_asset",
    ("每股指标", "每股企业自由现金流量"): "fcf_ps",
    ("成长能力", "营业总收入增长率"): "revenue_growth",
    ("成长能力", "归属母公司净利润增长率"): "profit_growth",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _deadline(stat_date: str) -> str:
    """法定披露截止日兜底（严格不早于真实公告日，避免前视）。"""
    y, m = int(stat_date[:4]), int(stat_date[5:7])
    if m == 3:
        return f"{y}-04-30"
    if m == 6:
        return f"{y}-08-31"
    if m == 9:
        return f"{y}-10-31"
    return f"{y + 1}-04-30"


def _to_float(x) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "--", "-", "None", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------- 新浪财务摘要 ----------

def fetch_financial_abstract(symbol: str, start_year: int) -> pd.DataFrame:
    """返回列：stat_date, metric, value。"""
    import akshare as ak

    last_err = None
    df = None
    for attempt in range(3):
        try:
            df = ak.stock_financial_abstract(symbol=symbol)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0 * (attempt + 1))
    if df is None:
        raise RuntimeError(f"财务摘要请求失败: {last_err}")
    if df.empty or "选项" not in df.columns:
        return pd.DataFrame()
    periods = [c for c in df.columns if str(c).isdigit() and len(str(c)) == 8]
    periods = [c for c in periods if int(str(c)[:4]) >= start_year]
    if not periods:
        return pd.DataFrame()

    rows = []
    for (group, name), metric in METRIC_MAP.items():
        sub = df[(df["选项"] == group) & (df["指标"] == name)]
        if sub.empty:
            continue
        rec = sub.iloc[0]
        for p in periods:
            v = _to_float(rec.get(p))
            if v is not None:
                rows.append({"stat_date": f"{p[:4]}-{p[4:6]}-{p[6:]}", "metric": metric, "value": v})
    return pd.DataFrame(rows)


# ---------- 公告日 / 分红 / 基础信息（巨潮 + 新浪） ----------

def _fetch_pub_dates(symbol: str, start_year: int, end_year: int) -> dict[str, str]:
    """{stat_date: pub_date}，来自巨潮定期报告公告（取最早披露日）。"""
    return fetch_report_pub_dates(symbol, start_year, end_year)


def _fetch_dividends(symbol: str, start_year: int, end_year: int) -> list[dict]:
    """分红明细（新浪），字段与 fact_dividend 对齐。"""
    return fetch_dividends(symbol, start_year, end_year)


def _fetch_basic(symbol: str) -> dict | None:
    ipo = fetch_ipo_date(symbol)
    if not ipo:
        return None
    return {"symbol": symbol, "ipo_date": ipo, "status": "normal"}


# ---------- 修订版本 ----------

def _same_num(a, b, tol: float = 1e-6) -> bool:
    """数值比较（容忍浮点误差）。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)
    return abs(fa - fb) <= tol * max(1.0, abs(fa), abs(fb))


def record_financial_revisions(repo: Repository, symbol: str, fin: pd.DataFrame,
                               src: str) -> dict:
    """把一次抓取结果写入修订版本表，并把最新值同步到 `fact_financial`。

    版本规则：同一 (symbol, stat_date, metric) 首次写入 version=1；
    之后数值或公告日发生变化时追加 version+1，否则不写（幂等）。
    """
    if fin.empty:
        return {"new": 0, "revisions": 0, "unchanged": 0}
    cur = {(r["stat_date"], r["metric"]): r for r in repo.query(
        "SELECT stat_date, metric, version, value, pub_date "
        "FROM fact_financial_revision WHERE symbol=?", (symbol,))}
    rows: list[dict] = []
    new_n = rev_n = same_n = 0
    for r in fin.itertuples():
        cur_row = cur.get((r.stat_date, r.metric))
        value = None if pd.isna(r.value) else float(r.value)
        base = {"symbol": symbol, "stat_date": r.stat_date, "metric": r.metric,
                "value": value, "pub_date": r.pub_date, "src": src,
                "fetched_at": r.fetched_at}
        if cur_row is None:
            rows.append({**base, "version": 1})
            new_n += 1
        elif (not _same_num(cur_row["value"], value)
              or (cur_row["pub_date"] or "") != (r.pub_date or "")):
            rows.append({**base, "version": int(cur_row["version"]) + 1})
            rev_n += 1
        else:
            same_n += 1
    if rows:
        repo.upsert("fact_financial_revision", rows,
                    ["symbol", "stat_date", "metric", "version"])
    return {"new": new_n, "revisions": rev_n, "unchanged": same_n}


def backfill_financial_revisions(repo: Repository) -> int:
    """把 `fact_financial` 现有快照初始化为 version=1（幂等）。返回新增行数。"""
    before = repo.count("fact_financial_revision")
    repo.conn.execute(
        """INSERT INTO fact_financial_revision
           (symbol, stat_date, metric, version, value, pub_date, src, fetched_at)
           SELECT f.symbol, f.stat_date, f.metric, 1, f.value, f.pub_date, f.src, f.fetched_at
           FROM fact_financial f
           WHERE NOT EXISTS (
               SELECT 1 FROM fact_financial_revision r
               WHERE r.symbol=f.symbol AND r.stat_date=f.stat_date AND r.metric=f.metric)"""
    )
    repo.conn.commit()
    return repo.count("fact_financial_revision") - before


def revision_report(repo: Repository, limit: int = 20) -> dict:
    """修订版本概览：总行数、发生过多版本的报告期数、最近修订明细。"""
    total = repo.count("fact_financial_revision")
    multi = repo.query(
        """SELECT COUNT(*) AS n FROM (
             SELECT symbol, stat_date, metric, COUNT(*) AS c
             FROM fact_financial_revision
             GROUP BY symbol, stat_date, metric HAVING c > 1)"""
    )[0]["n"]
    recent = [dict(r) for r in repo.query(
        """SELECT symbol, stat_date, metric, version, value, pub_date, fetched_at
           FROM fact_financial_revision WHERE version > 1
           ORDER BY fetched_at DESC, symbol, stat_date LIMIT ?""", (limit,))]
    return {"rows": total, "keys_multi_version": int(multi or 0),
            "recent_revisions": recent}


# ---------- 编排 ----------

def sync_fundamentals(repo: Repository, symbols: list[str], start_year: int = 2017,
                      end_year: int | None = None, with_dividend: bool = True,
                      with_basic: bool = True) -> dict:
    """逐只同步财务指标、公告日、分红与上市/退市信息（幂等）。

    数据源：新浪财务摘要/分红/公司资料 + 巨潮资讯定期报告公告日。
    所有网络请求均带超时与重试，单只失败不会中断整体同步。
    """
    end_year = end_year or datetime.now().year
    stats = {"symbols": len(symbols), "financial": 0, "revisions": 0,
             "dividend": 0, "basic": 0, "fail": 0}
    for i, sym in enumerate(symbols, 1):
        now = _now()
        # 1) 财务指标（新浪）+ 公告日（巨潮）
        try:
            fin = fetch_financial_abstract(sym, start_year)
            if not fin.empty:
                pub = _fetch_pub_dates(sym, start_year, end_year)
                fin["pub_date"] = fin["stat_date"].map(
                    lambda d: pub.get(d) or _deadline(d))
                fin["symbol"] = sym
                fin["src"] = "sina:financial_abstract+cninfo:pubdate"
                fin["fetched_at"] = now
                # 同一指标同报告期可能多行（重复选项），去重
                fin = fin.drop_duplicates(["symbol", "stat_date", "metric"])
                rec = record_financial_revisions(repo, sym, fin, fin["src"].iloc[0])
                stats["revisions"] += rec["revisions"]
                stats["financial"] += repo.upsert(
                    "fact_financial",
                    fin[["symbol", "stat_date", "pub_date", "metric", "value", "src",
                         "fetched_at"]].to_dict("records"),
                    ["symbol", "stat_date", "metric"],
                )
            else:
                logger.warning(f"{sym} 财务摘要为空")
        except Exception as e:  # noqa: BLE001
            stats["fail"] += 1
            logger.warning(f"{sym} 财务同步失败: {e}")

        # 2) 分红
        if with_dividend:
            try:
                div = _fetch_dividends(sym, start_year, end_year)
                if div:
                    for r in div:
                        r.update({"src": "sina:dividend", "fetched_at": now})
                    stats["dividend"] += repo.upsert(
                        "fact_dividend", div, ["symbol", "announce_date", "ex_date"])
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{sym} 分红同步失败: {e}")

        # 3) 上市/退市
        if with_basic:
            try:
                basic = _fetch_basic(sym)
                if basic:
                    basic["updated_at"] = now
                    repo.upsert("dim_stock", [basic], ["symbol"])
                    stats["basic"] += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{sym} 上市日同步失败: {e}")

        if i % 5 == 0:
            logger.info(f"  基本面进度 {i}/{len(symbols)}")
        time.sleep(0.2)

    repo.set_watermark("fact_financial", datetime.now().strftime("%Y-%m-%d"))
    logger.info(f"基本面同步完成: {stats}")
    return stats


def rebuild_missing_pub_dates(repo: Repository) -> int:
    """为已有财务记录补齐缺失的 pub_date（巨潮公告日，失败则用法定截止日）。"""
    rows = repo.query("SELECT DISTINCT symbol, stat_date FROM fact_financial "
                      "WHERE pub_date IS NULL OR pub_date=''")
    if not rows:
        return 0
    n = 0
    by_symbol: dict[str, list[str]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r["stat_date"])
    for sym, stat_dates in by_symbol.items():
        years = sorted({int(d[:4]) for d in stat_dates})
        pub = _fetch_pub_dates(sym, min(years), max(years))
        for d in stat_dates:
            repo.conn.execute(
                "UPDATE fact_financial SET pub_date=? WHERE symbol=? AND stat_date=?",
                (pub.get(d) or _deadline(d), sym, d),
            )
            n += 1
    repo.conn.commit()
    return n
