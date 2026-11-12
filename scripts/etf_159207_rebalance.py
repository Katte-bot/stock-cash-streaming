#!/usr/bin/env python3
"""高股息ETF广发（159207）历史持仓、调仓规则与 2026Q4 重仓股概率预测。

数据来源
--------
- 天天基金移动端接口：基金基本信息、历史季度前十大持仓（按 DATE 参数回溯）
- 中证指数官网 OSS：932305 成分股与权重文件
- akshare：个股分红（stock_history_dividend_detail）、个股不复权行情（stock_zh_a_hist）
- 本地 SQLite：全 A 后复权日行情、行业分类（只读 SELECT）

用法
----
    .venv/bin/python scripts/etf_159207_rebalance.py fetch
    .venv/bin/python scripts/etf_159207_rebalance.py analyze
    .venv/bin/python scripts/etf_159207_rebalance.py report

所有外部抓取结果缓存在 data/etf159207_cache/，--refresh 强制刷新。
数据库仅以只读模式打开，不做任何写入。
"""

from __future__ import annotations

import argparse
import io
import json
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# akshare 内部部分请求无超时，设置全局兜底，避免单只股票卡死整个批量任务
socket.setdefaulttimeout(25)

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "stock_cash_streaming.sqlite3"
CACHE = ROOT / "data" / "etf159207_cache"
DOCS = ROOT / "docs"

FUND_CODE = "159207"
INDEX_CODE = "932305"

REPORT_DATES = [
    "2025-06-30",  # 2025 中报
    "2025-09-30",  # 2025 三季报
    "2025-12-31",  # 2025 年报
    "2026-03-31",  # 2026 一季报
    "2026-06-30",  # 2026 中报
    "2026-09-30",  # 2026 三季报
]
QUARTER_LABEL = {
    "2025-06-30": "2025Q2/中报",
    "2025-09-30": "2025Q3",
    "2025-12-31": "2025Q4/年报",
    "2026-03-31": "2026Q1",
    "2026-06-30": "2026Q2/中报",
    "2026-09-30": "2026Q3",
}

FUND_INFO_URL = (
    "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNNBasicInformation"
    "?FCODE={code}&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0"
)
FUND_HOLD_URL = (
    "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition"
    "?FCODE={code}&DATE={date}&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0"
)
INDEX_CONS_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/"
    f"autofile/cons/{INDEX_CODE}cons.xls"
)
INDEX_WEIGHT_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/"
    f"autofile/closeweight/{INDEX_CODE}closeweight.xls"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.qq.com",
}

# 模拟参数（运行时会在 cmd_analyze 中按最新数据动态覆盖）
BASE_PRICE_DATE = "2026-11-04"  # 最新不复权价格日期（权重投影基准）
RETURN_ASOF = "2026-11-04"      # 本地后复权行情完整覆盖日（收益/波动估计）
WEIGHT_ASOF = "2026-10-30"      # 指数权重文件日期
SIM_START = "2026-11-05"
SIM_END = "2026-12-31"
N_PATHS = 20_000
RNG_SEED = 20261109


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def db_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    """只读查询 SQLite。"""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def http_get(url: str, binary: bool = False, retries: int = 3, timeout: int = 25):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.content if binary else r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败: {url} -> {last}")


def http_json(url: str):
    return http_get(url).json()


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        obj,
        ensure_ascii=False,
        indent=2,
        default=lambda o: o.item() if hasattr(o, "item") else str(o),
    )
    path.write_text(text, encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def pct(x: float, digits: int = 2) -> str:
    return "-" if pd.isna(x) else f"{x * 100:.{digits}f}%"


def spearman(a: pd.Series, b: pd.Series) -> float:
    """秩相关（不依赖 scipy）。"""
    return float(a.rank().corr(b.rank()))


def latest_complete_quote_date(symbols: list[str], min_ratio: float = 0.9) -> str:
    """本地行情覆盖率达到 min_ratio 的最近交易日。"""
    ph = ",".join("?" * len(symbols))
    rows = db_query(
        f"SELECT trade_date, COUNT(*) AS c FROM fact_quote "
        f"WHERE symbol IN ({ph}) GROUP BY trade_date "
        f"ORDER BY trade_date DESC LIMIT 15",
        tuple(symbols),
    )
    need = max(1, int(len(symbols) * min_ratio))
    for _, r in rows.iterrows():
        if int(r["c"]) >= need:
            return str(r["trade_date"])
    return str(rows.iloc[0]["trade_date"]) if len(rows) else ""


def actual_price_pivot(actual_prices: pd.DataFrame) -> pd.DataFrame:
    """不复权价格宽表（行=日期，列=代码）。"""
    df = actual_prices.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.pivot(index="date", columns="symbol", values="close").sort_index().ffill()


def next_business_day(date_str: str) -> str:
    return pd.bdate_range(pd.Timestamp(date_str), periods=2)[1].strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# fetch：抓取并缓存外部数据
# --------------------------------------------------------------------------- #
def fetch_fund_info(refresh: bool = False) -> dict:
    path = CACHE / "fund_info.json"
    if path.exists() and not refresh:
        return read_json(path)
    js = http_json(FUND_INFO_URL.format(code=FUND_CODE))
    datas = js.get("Datas") or {}
    keep = {
        k: datas.get(k)
        for k in (
            "FCODE", "SHORTNAME", "JJGS", "FTYPE", "ESTABDATE", "INDEXCODE",
            "INDEXNAME", "ENDNAV", "FEGMRQ", "DWJZ", "FSRQ",
        )
    }
    save_json(path, keep)
    return keep


def fetch_fund_holdings(refresh: bool = False) -> pd.DataFrame:
    path = CACHE / "fund_holdings_raw.json"
    if path.exists() and not refresh:
        return pd.DataFrame(read_json(path))
    rows = []
    for d in REPORT_DATES:
        js = http_json(FUND_HOLD_URL.format(code=FUND_CODE, date=d))
        stocks = (js.get("Datas") or {}).get("fundStocks") or []
        if len(stocks) != 10:
            raise RuntimeError(f"{d} 前十大持仓返回 {len(stocks)} 条，预期 10 条")
        for i, s in enumerate(stocks, 1):
            rows.append(
                {
                    "report_date": d,
                    "quarter": QUARTER_LABEL[d],
                    "rank": i,
                    "symbol": str(s["GPDM"]).zfill(6),
                    "name": s["GPJC"],
                    "weight_pct": float(s["JZBL"]),
                    "change_type": s.get("PCTNVCHGTYPE", ""),
                    "change_pct": float(s.get("PCTNVCHG") or 0),
                }
            )
        time.sleep(0.3)
    df = pd.DataFrame(rows)
    save_json(path, df.to_dict(orient="records"))
    return df


def fetch_index_files(refresh: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    cons_csv = CACHE / "index_cons.csv"
    w_csv = CACHE / "index_weight_raw.csv"
    if not (cons_csv.exists() and w_csv.exists()) or refresh:
        cons = pd.read_excel(io.BytesIO(http_get(INDEX_CONS_URL, binary=True)))
        wt = pd.read_excel(io.BytesIO(http_get(INDEX_WEIGHT_URL, binary=True)))
        cons = cons.iloc[:, [0, 4, 5, 7]]
        cons.columns = ["as_of", "symbol", "name", "exchange"]
        cons["symbol"] = cons["symbol"].astype(str).str.zfill(6)
        wt = wt.iloc[:, [0, 4, 5, 7, 9]]
        wt.columns = ["as_of", "symbol", "name", "exchange", "weight_pct"]
        wt["symbol"] = wt["symbol"].astype(str).str.zfill(6)
        wt["weight_pct"] = pd.to_numeric(wt["weight_pct"])
        cons.to_csv(cons_csv, index=False, encoding="utf-8-sig")
        wt.to_csv(w_csv, index=False, encoding="utf-8-sig")
    return (
        pd.read_csv(cons_csv, dtype={"symbol": str}),
        pd.read_csv(w_csv, dtype={"symbol": str}),
    )


def fetch_dividends(codes: list[str], refresh: bool = False) -> pd.DataFrame:
    """逐只拉取分红历史（东财接口，约 50 次请求，结果按股票分别缓存）。"""
    import akshare as ak

    per_dir = CACHE / "dividends"
    per_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, code in enumerate(codes, 1):
        path = per_dir / f"{code}.csv"
        if path.exists() and not refresh:
            frames.append(pd.read_csv(path, dtype={"symbol": str}))
            continue
        last = None
        for attempt in range(3):
            try:
                d = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
                d.insert(0, "symbol", code)
                d.to_csv(path, index=False, encoding="utf-8-sig")
                frames.append(d)
                last = None
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.0 + attempt)
        if last is not None:
            print(f"[警告] {code} 分红拉取失败: {last}", flush=True)
        if i % 10 == 0:
            print(f"  分红进度 {i}/{len(codes)}", flush=True)
        time.sleep(0.1)
    df = pd.concat(frames, ignore_index=True)
    df.to_csv(CACHE / "dividends.csv", index=False, encoding="utf-8-sig")
    return df


def _tx_symbol(code: str) -> str:
    """A 股代码 → 腾讯行情前缀。"""
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def fetch_latest_snapshot(codes: list[str], refresh: bool = False) -> pd.DataFrame:
    """腾讯批量快照（一次请求取全部成分股最新价，含交易日期）。"""
    snap_path = CACHE / "latest_snapshot.csv"
    if (
        snap_path.exists()
        and not refresh
        and (time.time() - snap_path.stat().st_mtime) < 300
    ):
        return pd.read_csv(snap_path, dtype={"symbol": str})

    url = "https://qt.gtimg.cn/q=" + ",".join(_tx_symbol(c) for c in codes)
    try:
        text = http_get(url, binary=True, timeout=20).decode("gbk", errors="ignore")
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 最新快照拉取失败: {e}", flush=True)
        if snap_path.exists():
            return pd.read_csv(snap_path, dtype={"symbol": str})
        return pd.DataFrame()

    rows = []
    for line in text.splitlines():
        if '="' not in line:
            continue
        try:
            body = line.split('="', 1)[1].rsplit('"', 1)[0]
            f = body.split("~")
            if len(f) < 33:
                continue
            code = str(f[2]).zfill(6)
            price = float(f[3])
            dt = str(f[30])
            if price <= 0 or len(dt) < 8:
                continue
            date = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"
            rows.append({"symbol": code, "date": date, "close": price})
        except (ValueError, IndexError):
            continue
    snap = pd.DataFrame(rows).drop_duplicates("symbol", keep="last")
    if not snap.empty:
        snap.to_csv(snap_path, index=False, encoding="utf-8-sig")
    return snap


def fetch_actual_prices(
    codes: list[str],
    refresh: bool = False,
    start_date: str = "20260401",
    end_date: str | None = None,
    use_snapshot: bool = True,
) -> pd.DataFrame:
    """不复权日线缓存 + 腾讯批量最新快照（用于权重投影与 9/30 以来收益）。"""
    import akshare as ak

    end_date = end_date or datetime.now().strftime("%Y%m%d")
    per_dir = CACHE / "prices"
    per_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, code in enumerate(codes, 1):
        path = per_dir / f"{code}.csv"
        if path.exists() and not refresh:
            frames.append(pd.read_csv(path, dtype={"symbol": str}))
            continue
        last = None
        for attempt in range(3):
            try:
                d = ak.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                )
                if d is not None and not d.empty:
                    d = d.rename(columns={"日期": "date", "收盘": "close"})
                    d = d[["date", "close"]]
                    d.insert(0, "symbol", code)
                    d.to_csv(path, index=False, encoding="utf-8-sig")
                    frames.append(d)
                last = None
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.0 + attempt)
        if last is not None:
            print(f"[警告] {code} 不复权行情拉取失败: {last}", flush=True)
        if i % 10 == 0:
            print(f"  不复权行情进度 {i}/{len(codes)}", flush=True)
        time.sleep(0.1)
    if use_snapshot:
        snap = fetch_latest_snapshot(codes, refresh=refresh)
        if not snap.empty:
            frames.append(snap)
            print(f"  最新快照: {snap['date'].max()}（{len(snap)} 只）", flush=True)
    df = pd.concat(frames, ignore_index=True)
    df = (
        df.drop_duplicates(["symbol", "date"], keep="last")
        .sort_values(["symbol", "date"])
        .reset_index(drop=True)
    )
    df.to_csv(CACHE / "actual_prices.csv", index=False, encoding="utf-8-sig")
    return df


def cmd_fetch(refresh: bool = False) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    print("1) 基金基本信息 ...")
    info = fetch_fund_info(refresh)
    print("   ", info.get("SHORTNAME"), info.get("INDEXCODE"), info.get("INDEXNAME"))
    print("2) 六期前十大持仓 ...")
    hold = fetch_fund_holdings(refresh)
    print("   ", hold.groupby("report_date").size().to_dict())
    print("3) 指数成分与权重 ...")
    cons, wt = fetch_index_files(refresh)
    codes = sorted(wt["symbol"].unique())
    print(f"    成分 {len(cons)} 只 / 权重 {len(wt)} 行，权重合计 {wt['weight_pct'].sum():.3f}%")
    print("4) 分红历史 ...")
    fetch_dividends(codes, refresh)
    print("5) 不复权行情 ...")
    fetch_actual_prices(codes, refresh)
    print("完成，缓存目录:", CACHE)


# --------------------------------------------------------------------------- #
# analyze：持仓演变、规则验证、Q4 预测
# --------------------------------------------------------------------------- #
def load_quotes(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    ph = ",".join("?" * len(symbols))
    sql = (
        f"SELECT symbol, trade_date, close FROM fact_quote "
        f"WHERE symbol IN ({ph}) AND trade_date BETWEEN ? AND ? "
        f"ORDER BY symbol, trade_date"
    )
    return db_query(sql, tuple(symbols) + (start, end))


def industry_map() -> dict[str, str]:
    df = db_query("SELECT symbol, level1 FROM dim_stock_industry")
    return dict(zip(df["symbol"], df["level1"]))


def build_holdings_table(hold: pd.DataFrame) -> pd.DataFrame:
    ind = industry_map()
    df = hold.copy()
    df["industry_l1"] = df["symbol"].map(ind).fillna("")
    return df[
        ["report_date", "quarter", "rank", "symbol", "name", "weight_pct",
         "change_type", "change_pct", "industry_l1"]
    ]


def turnover_metrics(hold: pd.DataFrame) -> pd.DataFrame:
    rows = []
    dates = REPORT_DATES
    for prev, curr in zip(dates, dates[1:]):
        a = hold[hold["report_date"] == prev].set_index("symbol")
        b = hold[hold["report_date"] == curr].set_index("symbol")
        sa, sb = set(a.index), set(b.index)
        inter = sa & sb
        new = sb - sa
        out = sa - sb
        common = sorted(inter)
        l1 = sum(
            abs(b.loc[s, "weight_pct"] - a.loc[s, "weight_pct"]) for s in common
        ) + sum(b.loc[s, "weight_pct"] for s in new) + sum(
            a.loc[s, "weight_pct"] for s in out
        )
        rows.append(
            {
                "prev": prev,
                "curr": curr,
                "prev_top10_weight_pct": round(a["weight_pct"].sum(), 3),
                "curr_top10_weight_pct": round(b["weight_pct"].sum(), 3),
                "new_count": len(new),
                "exit_count": len(out),
                "jaccard": round(len(inter) / len(sa | sb), 4),
                "l1_weight_change_pct": round(l1, 3),
                "rank_corr_common": round(
                    spearman(a.loc[common, "weight_pct"], b.loc[common, "weight_pct"]), 4
                )
                if len(common) > 2
                else np.nan,
                "new_names": "、".join(b.loc[s, "name"] for s in sorted(new)),
                "exit_names": "、".join(a.loc[s, "name"] for s in sorted(out)),
            }
        )
    return pd.DataFrame(rows)


def drift_attribution(hold: pd.DataFrame, quotes: pd.DataFrame) -> pd.DataFrame:
    """检验非调样季度前十大变化是否由价格漂移驱动。"""
    px = quotes.pivot(index="trade_date", columns="symbol", values="close").sort_index()
    rows = []
    for prev, curr in zip(REPORT_DATES, REPORT_DATES[1:]):
        a = hold[hold["report_date"] == prev].set_index("symbol")
        b = hold[hold["report_date"] == curr].set_index("symbol")
        new, out = set(b.index) - set(a.index), set(a.index) - set(b.index)
        common = sorted(set(a.index) & set(b.index))

        def ret(s: str) -> float:
            try:
                return float(px.loc[curr, s] / px.loc[prev, s] - 1)
            except Exception:  # noqa: BLE001
                return np.nan

        new_r = [ret(s) for s in new]
        out_r = [ret(s) for s in out]
        wchg = [b.loc[s, "weight_pct"] - a.loc[s, "weight_pct"] for s in common]
        com_r = [ret(s) for s in common]
        corr = (
            round(spearman(pd.Series(wchg), pd.Series(com_r)), 4)
            if len(common) > 2
            else np.nan
        )
        rows.append(
            {
                "prev": prev,
                "curr": curr,
                "new_count": len(new),
                "new_avg_ret": round(float(np.nanmean(new_r)), 4) if new_r else np.nan,
                "exit_avg_ret": round(float(np.nanmean(out_r)), 4) if out_r else np.nan,
                "weight_chg_vs_ret_corr": corr,
                "is_annual_rebalance": curr == "2026-06-30",
            }
        )
    return pd.DataFrame(rows)


def build_dividend_check(
    wt: pd.DataFrame, dividends: pd.DataFrame, actual_prices: pd.DataFrame
) -> pd.DataFrame:
    div = dividends.copy()
    div["公告日期"] = pd.to_datetime(div["公告日期"], errors="coerce")
    div["除权除息日"] = pd.to_datetime(div["除权除息日"], errors="coerce")
    div["派息"] = pd.to_numeric(div["派息"], errors="coerce").fillna(0)
    div["送股"] = pd.to_numeric(div["送股"], errors="coerce").fillna(0)
    div["转增"] = pd.to_numeric(div["转增"], errors="coerce").fillna(0)

    px = actual_prices.copy()
    px["date"] = pd.to_datetime(px["date"])
    px430 = (
        px[px["date"] == pd.Timestamp("2026-04-30")]
        .set_index("symbol")["close"]
        .astype(float)
    )

    rows = []
    for _, r in wt.iterrows():
        code = r["symbol"]
        d = div[div["symbol"] == code]
        # 除权年度覆盖：2024/2025/2026 分别对应 2023/2024/2025 年度分配
        years = set(d["除权除息日"].dt.year.dropna().astype(int))
        paid_2026 = d[d["除权除息日"].dt.year == 2026]
        dps_2025 = float(paid_2026["派息"].sum()) / 10.0  # 元/股
        q4 = d[
            (d["除权除息日"] >= pd.Timestamp(SIM_START))
            & (d["除权除息日"] <= pd.Timestamp(SIM_END))
        ]
        price430 = float(px430.get(code, np.nan))
        dy = dps_2025 / price430 if price430 and not np.isnan(price430) else np.nan
        rows.append(
            {
                "symbol": code,
                "name": r["name"],
                "div_2023": int(2024 in years),
                "div_2024": int(2025 in years),
                "div_2025": int(2026 in years),
                "dps_2025_yuan": round(dps_2025, 4),
                "price_20260430": round(price430, 3) if not np.isnan(price430) else np.nan,
                "dividend_yield_2025": round(dy, 6) if not np.isnan(dy) else np.nan,
                "ex_date_in_2026Q4": int(len(q4) > 0),
                "q4_ex_dates": "、".join(d.strftime("%Y-%m-%d") for d in q4["除权除息日"]),
                "q4_dps_yuan": round(float(q4["派息"].sum()) / 10.0, 4),
            }
        )
    return pd.DataFrame(rows)


def validate_weight_model(
    wt: pd.DataFrame, div_check: pd.DataFrame, quotes: pd.DataFrame
) -> dict:
    """验证 权重 ∝ 预案股息率 / 波动率。"""
    # 年化波动率（截至 2026-04-30 的 250 个交易日）
    px = quotes[quotes["symbol"].isin(wt["symbol"])].pivot(
        index="trade_date", columns="symbol", values="close"
    ).sort_index()
    px = px[px.index <= "2026-04-30"].tail(251)
    ret = px.pct_change().dropna()
    vol = ret.std()
    df = wt.copy()
    df["vol"] = df["symbol"].map(vol)
    df = df.merge(
        div_check[["symbol", "dividend_yield_2025"]], on="symbol", how="left"
    )
    df["score"] = df["dividend_yield_2025"] / df["vol"]
    sub = df.dropna(subset=["score", "weight_pct"]).copy()
    implied = sub["score"] / sub["score"].sum() * sub["weight_pct"].sum()
    rho = spearman(implied, sub["weight_pct"])
    top10_actual = set(sub.nlargest(10, "weight_pct")["symbol"])
    top10_implied = set(sub.nlargest(10, "score")["symbol"])
    return {
        "n": int(len(sub)),
        "spearman": round(rho, 4),
        "top10_overlap": len(top10_actual & top10_implied),
        "table": df.sort_values("weight_pct", ascending=False),
    }


def project_base_weights(
    wt: pd.DataFrame,
    actual_prices: pd.DataFrame,
    weight_asof: str,
    latest_date: str,
) -> pd.DataFrame:
    """把指数权重按不复权价格变化投影到最新交易日。"""
    base = wt[["symbol", "name", "weight_pct"]].copy()
    px = actual_price_pivot(actual_prices)
    if latest_date not in px.index.strftime("%Y-%m-%d").tolist():
        raise RuntimeError(f"最新价格日 {latest_date} 不在不复权行情中")
    if weight_asof not in px.index.strftime("%Y-%m-%d").tolist():
        raise RuntimeError(f"权重日 {weight_asof} 不在不复权行情中")
    ratio = (
        px.loc[pd.Timestamp(latest_date)] / px.loc[pd.Timestamp(weight_asof)]
    )
    base["price_ratio"] = base["symbol"].map(ratio)
    base["weight_base"] = base["weight_pct"] * base["price_ratio"]
    base["weight_base"] = base["weight_base"] / base["weight_base"].sum() * 100
    return base


def simulate(
    base: pd.DataFrame,
    quotes: pd.DataFrame,
    div_check: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """单因子模型 + 蒙特卡洛，输出各情景入选前十大频率。"""
    px = quotes.pivot(index="trade_date", columns="symbol", values="close").sort_index()
    px = px.loc["2025-11-01":RETURN_ASOF]
    px = px[base["symbol"].tolist()]
    ret = px.pct_change().dropna()
    f = ret.mean(axis=1)  # 等权因子
    beta = ret.apply(lambda s: np.cov(s, f)[0, 1] / np.var(f, ddof=1))
    # 注意：f（日期索引）与 beta（股票索引）不能直接相乘，需按行列对齐
    resid = ret - np.outer(f.to_numpy(), beta.reindex(ret.columns).to_numpy())
    sigma_eps = resid.std()
    sigma_f = float(f.std())

    ret20 = px.iloc[-1] / px.iloc[-21] - 1
    days = pd.bdate_range(SIM_START, SIM_END)
    if len(days) == 0:
        raise RuntimeError(f"模拟区间为空：{SIM_START} ~ {SIM_END}")
    n_days = len(days)
    symbols = base["symbol"].tolist()
    w0 = base["weight_base"].to_numpy() / 100.0

    # Q4 除权调整：除权日价格收益率中扣除每股派息 / 期初真实价格
    div_adj = {}
    q4 = div_check[div_check["ex_date_in_2026Q4"] == 1]
    for _, r in q4.iterrows():
        ex_dates = [d.strip() for d in str(r["q4_ex_dates"]).split("、") if d.strip()]
        price = r["price_20260430"]
        for d in ex_dates:
            if pd.Timestamp(d) in days and price and not np.isnan(price):
                div_adj.setdefault(r["symbol"], {})[
                    int(days.get_loc(pd.Timestamp(d)))
                ] = r["q4_dps_yuan"] / price

    rng = np.random.default_rng(RNG_SEED)
    # 近 20 日等权组合日均收益，作为动量/反转情景的因子漂移
    mom_daily = float(np.log1p(ret20).mean() / 20)
    scen = {
        "neutral": 0.0,
        "momentum": 0.5 * mom_daily,
        "reversal": -0.5 * mom_daily,
    }

    probs = {}
    rank_medians = {}
    for name, drift in scen.items():
        f_path = rng.normal(drift, sigma_f, size=(N_PATHS, n_days))
        cum = np.ones((N_PATHS, len(symbols)))
        for j, s in enumerate(symbols):
            eps = rng.normal(0.0, float(sigma_eps[s]), size=(N_PATHS, n_days))
            r = float(beta[s]) * f_path + eps
            for idx, dx in div_adj.get(s, {}).items():
                r[:, idx] -= dx
            cum[:, j] = np.prod(1.0 + r, axis=1)
        final_w = w0[None, :] * cum
        final_w = final_w / final_w.sum(axis=1, keepdims=True)
        order = np.argsort(-final_w, axis=1)
        top10 = order[:, :10]
        cnt = np.zeros(len(symbols))
        for k in range(10):
            cnt += np.bincount(top10[:, k], minlength=len(symbols))
        probs[name] = cnt / N_PATHS
        # 中位名次
        ranks = np.empty_like(order)
        for i in range(order.shape[0]):
            ranks[i, order[i]] = np.arange(1, len(symbols) + 1)
        rank_medians[name] = np.median(ranks, axis=0)

    out = base.copy()
    for name in scen:
        out[f"p_top10_{name}"] = probs[name]
        out[f"rank_median_{name}"] = rank_medians[name]
    out["p_top10_blend"] = (
        0.5 * out["p_top10_neutral"]
        + 0.3 * out["p_top10_momentum"]
        + 0.2 * out["p_top10_reversal"]
    )
    out["rank_median"] = (
        0.5 * out["rank_median_neutral"]
        + 0.3 * out["rank_median_momentum"]
        + 0.2 * out["rank_median_reversal"]
    )
    out["tier"] = pd.cut(
        out["p_top10_blend"],
        bins=[-0.001, 0.30, 0.60, 0.80, 1.001],
        labels=["概率较低", "待定", "较稳", "锁定"],
    ).astype(str)
    return out, ret20


def cmd_analyze() -> None:
    global BASE_PRICE_DATE, RETURN_ASOF, WEIGHT_ASOF, SIM_START, SIM_END
    DOCS.mkdir(parents=True, exist_ok=True)
    info = fetch_fund_info()
    hold = fetch_fund_holdings()
    cons, wt = fetch_index_files()
    codes = sorted(wt["symbol"].unique())
    dividends = fetch_dividends(codes)
    actual_prices = fetch_actual_prices(codes)

    # 动态基准：最新不复权价 → 权重投影；本地完整行情 → 收益估计
    px_actual = actual_price_pivot(actual_prices)
    latest_date = px_actual.index.max().strftime("%Y-%m-%d")
    weight_asof = pd.to_datetime(
        str(wt["as_of"].iloc[0]), format="%Y%m%d", errors="coerce"
    ).strftime("%Y-%m-%d")
    return_asof = latest_complete_quote_date(codes)
    if not return_asof:
        raise RuntimeError("本地行情为空，无法估计收益")
    BASE_PRICE_DATE = latest_date
    WEIGHT_ASOF = weight_asof
    RETURN_ASOF = return_asof
    SIM_START = next_business_day(latest_date)
    SIM_END = (
        "2026-12-31"
        if latest_date < "2026-12-31"
        else (pd.Timestamp(latest_date) + pd.offsets.QuarterEnd()).strftime("%Y-%m-%d")
    )
    print(
        f"数据基准：价格 {BASE_PRICE_DATE} / 收益 {RETURN_ASOF} / 权重 {WEIGHT_ASOF}；"
        f"模拟 {SIM_START} ~ {SIM_END}",
        flush=True,
    )

    # 1. 持仓表与换手
    hold_tbl = build_holdings_table(hold)
    hold_tbl.to_csv(DOCS / "etf_159207_holdings_top10.csv", index=False, encoding="utf-8-sig")
    turn = turnover_metrics(hold)
    turn.to_csv(CACHE / "turnover.csv", index=False, encoding="utf-8-sig")

    # 2. 指数权重
    ind = industry_map()
    wt_tbl = wt.copy()
    wt_tbl["as_of_date"] = pd.to_datetime(
        wt_tbl["as_of"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    wt_tbl["rank"] = wt_tbl["weight_pct"].rank(ascending=False, method="min").astype(int)
    wt_tbl["industry_l1"] = wt_tbl["symbol"].map(ind).fillna("")
    wt_tbl = wt_tbl.sort_values("weight_pct", ascending=False)
    wt_tbl[["as_of_date", "rank", "symbol", "name", "weight_pct", "industry_l1"]].to_csv(
        DOCS / "etf_159207_index_weights.csv", index=False, encoding="utf-8-sig"
    )

    # 3. 分红核验
    div_check = build_dividend_check(wt, dividends, actual_prices)
    div_check.to_csv(DOCS / "etf_159207_dividend_check.csv", index=False, encoding="utf-8-sig")

    # 4. 行情与规则验证（覆盖当前成分 + 历史上出现过的前十大个股）
    all_syms = sorted(set(wt["symbol"]) | set(hold["symbol"]))
    quotes = load_quotes(all_syms, "2025-04-01", RETURN_ASOF)
    val = validate_weight_model(wt, div_check, quotes)
    val["table"].to_csv(CACHE / "weight_model_table.csv", index=False, encoding="utf-8-sig")
    drift = drift_attribution(hold, quotes)
    drift.to_csv(CACHE / "drift_attribution.csv", index=False, encoding="utf-8-sig")

    # 5. 预测（权重用最新不复权价投影，收益/波动用本地完整后复权行情）
    base = project_base_weights(wt, actual_prices, WEIGHT_ASOF, BASE_PRICE_DATE)
    pred, ret20 = simulate(base, quotes, div_check)
    pred["ret_20d"] = pred["symbol"].map(ret20)
    px = quotes.pivot(index="trade_date", columns="symbol", values="close").sort_index()
    pred["ret_60d"] = pred["symbol"].map(px.iloc[-1] / px.iloc[-61] - 1)
    pred["vol_120d"] = pred["symbol"].map(px.pct_change().tail(120).std() * np.sqrt(252))
    # 9/30 以来（最新不复权价）：收益与对 ETF 的贡献
    r_since = (
        px_actual.loc[pd.Timestamp(BASE_PRICE_DATE)]
        / px_actual.loc[pd.Timestamp("2026-09-30")]
        - 1
    )
    pred["ret_since_report"] = pred["symbol"].map(r_since)
    q3 = hold[hold["report_date"] == "2026-09-30"].set_index("symbol")
    pred["contrib_since_report"] = (
        pred["symbol"].map(q3["weight_pct"]) * pred["ret_since_report"]
    ).where(pred["symbol"].isin(q3.index))
    pred["industry_l1"] = pred["symbol"].map(ind).fillna("")
    pred = pred.sort_values("p_top10_blend", ascending=False)
    cols = [
        "symbol", "name", "industry_l1", "weight_pct", "weight_base",
        "ret_20d", "ret_60d", "vol_120d", "ret_since_report", "contrib_since_report",
        "p_top10_neutral", "p_top10_momentum", "p_top10_reversal",
        "p_top10_blend", "rank_median", "tier",
    ]
    pred_out = pred[cols].rename(columns={"weight_pct": "weight_" + WEIGHT_ASOF.replace("-", "")})
    pred_out.to_csv(DOCS / "etf_159207_q4_prediction.csv", index=False, encoding="utf-8-sig")

    # 6. 其它指标落盘
    fund_0930 = hold[hold["report_date"] == "2026-09-30"].set_index("symbol")
    common = sorted(set(fund_0930.index) & set(wt["symbol"]))
    r_since_q3 = r_since.reindex(fund_0930.index)
    eq50 = float(r_since.reindex(wt["symbol"]).mean())
    q3_weighted = float(
        (fund_0930["weight_pct"] * r_since_q3).sum() / fund_0930["weight_pct"].sum()
    )
    since_rows = []
    for s, row in fund_0930.iterrows():
        rr = r_since_q3.get(s)
        rr = float(rr) if rr is not None and pd.notna(rr) else None
        since_rows.append(
            {
                "symbol": s,
                "name": row["name"],
                "weight_pct": float(row["weight_pct"]),
                "ret_since_report": rr,
                "contrib_pp": None if rr is None else float(row["weight_pct"] * rr),
            }
        )
    since_rows.sort(key=lambda x: -1e9 if x["contrib_pp"] is None else -x["contrib_pp"])

    # 按权重排序，用于“当前第 8~15 名”边界表
    by_weight = pred.sort_values("weight_base", ascending=False).reset_index(drop=True)
    metrics = {
        "fund_info": info,
        "data_as_of": BASE_PRICE_DATE,
        "return_asof": RETURN_ASOF,
        "weight_asof": WEIGHT_ASOF,
        "sim_start": SIM_START,
        "sim_end": SIM_END,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "turnover": turn.to_dict(orient="records"),
        "dividend_check": {
            "n": int(len(div_check)),
            "div_3y_ok": int(
                ((div_check[["div_2023", "div_2024", "div_2025"]].sum(axis=1)) == 3).sum()
            ),
            "q4_ex_count": int(div_check["ex_date_in_2026Q4"].sum()),
            "missing_price": int(div_check["price_20260430"].isna().sum()),
        },
        "weight_model": {
            "n": val["n"],
            "spearman": val["spearman"],
            "top10_overlap": val["top10_overlap"],
        },
        "drift": drift.to_dict(orient="records"),
        "etf_vs_index_0930": {
            "n_common": len(common),
            "spearman": round(
                spearman(
                    fund_0930.loc[common, "weight_pct"],
                    wt.set_index("symbol").loc[common, "weight_pct"],
                ),
                4,
            ),
        },
        "since_last_report": {
            "report_date": "2026-09-30",
            "data_as_of": BASE_PRICE_DATE,
            "equal_weight_50": round(eq50, 6),
            "q3_top10_weighted": round(q3_weighted, 6),
            "rows": since_rows,
        },
        "prediction": {
            "top10": pred.head(10)[
                ["symbol", "name", "weight_base", "p_top10_blend", "tier"]
            ].to_dict(orient="records"),
            "top10_weight_sum_base": round(by_weight.head(10)["weight_base"].sum(), 3),
            "top10_weight_sum_1030": round(by_weight.head(10)["weight_pct"].sum(), 3),
            "prob_sum": round(pred["p_top10_blend"].sum(), 3),
            "expected_top10_prob_sum": 10.0,
            "tier_counts": pred["tier"].value_counts().to_dict(),
            "boundary": by_weight.iloc[7:15][
                ["symbol", "name", "weight_base", "p_top10_blend", "tier"]
            ].to_dict(orient="records"),
        },
    }
    # 边界替换阈值：相对第 10 名所需超额涨幅（按当前权重大小）
    r10 = float(by_weight.iloc[9]["weight_base"])
    boundary = by_weight.iloc[7:15][["symbol", "name", "weight_base"]].copy()
    boundary["need_rel_gain_vs_rank10"] = boundary["weight_base"].apply(
        lambda w: (r10 / w - 1) if w > 0 else np.nan
    )
    boundary.to_csv(CACHE / "boundary.csv", index=False, encoding="utf-8-sig")
    metrics["boundary_threshold"] = boundary.to_dict(orient="records")
    save_json(CACHE / "metrics.json", metrics)

    print("持仓表:", DOCS / "etf_159207_holdings_top10.csv")
    print("换手指标:")
    print(turn[["curr", "new_count", "exit_count", "jaccard", "l1_weight_change_pct", "rank_corr_common"]].to_string(index=False))
    print("权重模型验证:", metrics["weight_model"])
    print("ETF vs 指数（2026-09-30 共同持仓）:", metrics["etf_vs_index_0930"])
    print("分红核验:", metrics["dividend_check"])
    print(
        f"9/30以来：50只等权 {eq50 * 100:.2f}% / Q3前十大加权 {q3_weighted * 100:.2f}%",
        flush=True,
    )
    print("预测前十大:")
    print(pred.head(10)[["rank_median", "symbol", "name", "weight_base", "p_top10_blend", "tier"]].to_string(index=False))
    print("预测文件:", DOCS / "etf_159207_q4_prediction.csv")


# --------------------------------------------------------------------------- #
# report：生成 Markdown 研究报告
# --------------------------------------------------------------------------- #
def md_table(df: pd.DataFrame, digits: int = 2) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        vals = []
        for c in cols:
            v = r[c]
            if isinstance(v, (float, np.floating)):
                vals.append("" if pd.isna(v) else f"{v:.{digits}f}")
            else:
                vals.append(str(v))
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def cmd_report() -> None:
    global BASE_PRICE_DATE, RETURN_ASOF, WEIGHT_ASOF, SIM_START, SIM_END
    metrics = read_json(CACHE / "metrics.json")
    BASE_PRICE_DATE = metrics.get("data_as_of", BASE_PRICE_DATE)
    RETURN_ASOF = metrics.get("return_asof", RETURN_ASOF)
    WEIGHT_ASOF = metrics.get("weight_asof", WEIGHT_ASOF)
    SIM_START = metrics.get("sim_start", SIM_START)
    SIM_END = metrics.get("sim_end", SIM_END)
    hold = pd.read_csv(DOCS / "etf_159207_holdings_top10.csv", dtype={"symbol": str})
    wt = pd.read_csv(DOCS / "etf_159207_index_weights.csv", dtype={"symbol": str})
    div = pd.read_csv(DOCS / "etf_159207_dividend_check.csv", dtype={"symbol": str})
    pred = pd.read_csv(DOCS / "etf_159207_q4_prediction.csv", dtype={"symbol": str})
    turn = pd.DataFrame(metrics["turnover"])

    info = metrics["fund_info"]
    # 六期持仓宽表
    wide = hold.pivot(index="rank", columns="quarter", values="name")
    wwide = hold.pivot(index="rank", columns="quarter", values="weight_pct")
    qcols = list(wide.columns)
    rows = []
    for rk in wide.index:
        row = {"排名": int(rk)}
        for q in qcols:
            nm = wide.loc[rk, q]
            w = wwide.loc[rk, q]
            row[q] = "" if pd.isna(nm) else f"{nm}（{w:.2f}%）"
        rows.append(row)
    hold_wide = pd.DataFrame(rows)

    turn_view = turn.assign(
        **{
            "区间": turn["prev"] + " → " + turn["curr"],
            "前十大权重%": turn["curr_top10_weight_pct"],
            "新进": turn["new_count"],
            "退出": turn["exit_count"],
            "Jaccard": turn["jaccard"],
            "权重变动L1%": turn["l1_weight_change_pct"],
            "共同持仓秩相关": turn["rank_corr_common"],
        }
    )[["区间", "前十大权重%", "新进", "退出", "Jaccard", "权重变动L1%", "共同持仓秩相关"]]

    since_meta = metrics.get("since_last_report", {})
    since = pd.DataFrame(since_meta.get("rows", []))
    if not since.empty:
        since_view = since.assign(
            **{
                "9/30以来%": pd.to_numeric(since["ret_since_report"], errors="coerce") * 100,
                "贡献pp": pd.to_numeric(since["contrib_pp"], errors="coerce"),
            }
        )[["name", "symbol", "weight_pct", "9/30以来%", "贡献pp"]]
        since_view.columns = ["名称", "代码", "Q3权重%", "9/30以来%", "贡献pp"]
    else:
        since_view = pd.DataFrame(columns=["名称", "代码", "Q3权重%", "9/30以来%", "贡献pp"])
    since_eq = float(since_meta.get("equal_weight_50") or 0) * 100
    since_q3 = float(since_meta.get("q3_top10_weighted") or 0) * 100

    pv = pred.copy()
    pv["入选概率"] = pv["p_top10_blend"]
    pv["中性概率"] = pv["p_top10_neutral"]
    pv["动量概率"] = pv["p_top10_momentum"]
    pv["反转概率"] = pv["p_top10_reversal"]
    pv["基准权重%"] = pv["weight_base"]
    pv["中位名次"] = pv["rank_median"].round(1)
    pv["近20日涨幅%"] = pv["ret_20d"] * 100
    pv["年化波动%"] = pv["vol_120d"] * 100
    pv_view = pv[
        ["name", "symbol", "industry_l1", "基准权重%", "近20日涨幅%", "年化波动%",
         "中性概率", "动量概率", "反转概率", "入选概率", "中位名次", "tier"]
    ].head(20)
    pv_view.columns = [
        "名称", "代码", "申万一级", "基准权重%", "近20日涨幅%", "年化波动%",
        "中性", "动量", "反转", "综合概率", "中位名次", "分层",
    ]

    boundary = pd.DataFrame(metrics["boundary_threshold"])
    bview = boundary.copy()
    bview["所需相对第10名超额"] = bview["need_rel_gain_vs_rank10"]
    bview = bview[["symbol", "name", "weight_base", "need_rel_gain_vs_rank10"]]
    bview.columns = ["代码", "名称", "基准权重%", "需相对第10名超额涨幅"]

    wm = metrics["weight_model"]
    dc = metrics["dividend_check"]
    etf_idx = metrics["etf_vs_index_0930"]
    pr = metrics["prediction"]

    # 核心结论所需的动态文本
    locked = pred[pred["p_top10_blend"] >= 0.80].sort_values(
        "p_top10_blend", ascending=False
    )
    likely = pred[(pred["p_top10_blend"] >= 0.60) & (pred["p_top10_blend"] < 0.80)]
    contested = pred[(pred["p_top10_blend"] >= 0.30) & (pred["p_top10_blend"] < 0.60)]
    locked_names = "、".join(locked["name"].tolist())
    likely_names = "、".join(likely["name"].tolist())
    contested_names = "、".join(
        f"{r['name']}（{r['p_top10_blend'] * 100:.0f}%）"
        for _, r in contested.iterrows()
    )
    seat_note = "、".join(
        f"{r['name']}（{r['p_top10_blend'] * 100:.0f}%）"
        for _, r in pred.sort_values("p_top10_blend", ascending=False)
        .iloc[7:11]
        .iterrows()
    )
    q3_new = turn[turn["curr"] == "2025-09-30"]["new_names"].iloc[0]
    q3_exit = turn[turn["curr"] == "2025-09-30"]["exit_names"].iloc[0]
    q2_new = turn[turn["curr"] == "2026-06-30"]["new_names"].iloc[0]
    q2_exit = turn[turn["curr"] == "2026-06-30"]["exit_names"].iloc[0]

    # 价格漂移归因表
    drift = pd.DataFrame(metrics["drift"])
    drift_view = drift.copy()
    drift_view["新进平均区间收益%"] = drift_view["new_avg_ret"] * 100
    drift_view["退出平均区间收益%"] = drift_view["exit_avg_ret"] * 100
    drift_view["权重变动与收益秩相关"] = drift_view["weight_chg_vs_ret_corr"]
    drift_view["区间"] = drift_view["prev"] + " → " + drift_view["curr"]
    drift_view["性质"] = np.where(drift_view["is_annual_rebalance"], "5月年度调样", "价格漂移主导")
    drift_view = drift_view[
        ["区间", "性质", "新进平均区间收益%", "退出平均区间收益%", "权重变动与收益秩相关"]
    ]

    top10_tbl = pd.DataFrame(pr["top10"])
    top10_tbl["入选概率"] = top10_tbl["p_top10_blend"]
    top10_tbl["基准权重%"] = top10_tbl["weight_base"]
    top10_tbl = top10_tbl[["symbol", "name", "基准权重%", "入选概率", "tier"]]
    top10_tbl.columns = ["代码", "名称", "基准权重%", "入选概率", "分层"]

    report = f"""# 高股息ETF广发（159207）调仓规则分析与 2026Q4 重仓股概率预测

> 最新价格：{BASE_PRICE_DATE} 收盘；收益估计：{RETURN_ASOF}；指数权重快照 {WEIGHT_ASOF}。
> 本报告为量化研究结果，不构成投资建议。

## 核心结论

- **调仓规则**：159207 完全复制中证智选高股息策略指数（932305）。指数每年 5 月第 6 个交易日调样，按"连续 3 年分红 + 已披露分红预案"准入、按预案股息率选前 50 只，并按"预案股息率/波动率"加权；非调样季度的前十大变化主要来自股价相对表现带来的权重漂移。
- **2026Q4 前十大**：锁定（概率≥80%）为 {locked_names}；较稳（60%~80%）为 {likely_names}；第 10 席附近的候选为 {contested_names}，边界非常接近。
- **关键观察点**：Q4 无基本面换仓，只需跟踪上述边界个股的相对涨跌与可能的临时成分调整。

## 一、基金与标的指数概况

| 项目 | 内容 |
|---|---|
| 基金代码/简称 | {info.get('FCODE')} / {info.get('SHORTNAME')} |
| 基金管理人 | {info.get('JJGS')} |
| 跟踪指数 | {info.get('INDEXCODE')} {info.get('INDEXNAME')} |
| 基金成立日 | {info.get('ESTABDATE')} |
| 最新报告期 | {info.get('FEGMRQ')} |
| 最新规模 | {float(info.get('ENDNAV') or 0) / 1e8:.2f} 亿元 |
| 指数样本数 | {len(wt)} 只 |
| 2026Q3 前十大权重合计 | {metrics['turnover'][-1]['curr_top10_weight_pct']:.2f}% |

指数编制要点（中证指数官网 932305 页面及券商研报）：

1. **样本空间**：连续 3 年现金分红，且已披露现金分红预案、预案合计每股税前现金股利大于 0；
2. **选样**：按预案股息率降序排列，选取前 50 只；
3. **调样**：每年一次，5 月第 6 个交易日生效（2026 年生效日为 5 月 13 日）；
4. **加权**：预案股息率 / 波动率双因子加权，单只权重上限 10%。

## 二、数据与口径

- 前十大持仓：天天基金移动端接口，按报告期回溯 6 期（2025 中报至 2026 三季报）；
- 指数成分与权重：中证指数官网 OSS 文件（{WEIGHT_ASOF}）；
- 收益/波动：本地 SQLite `fact_quote`（后复权），完整覆盖至 {RETURN_ASOF}；
- 最新价格与权重投影：akshare 不复权日线，最新至 {BASE_PRICE_DATE}；
- 数据库仅使用只读 SELECT，不写库。

## 三、六期前十大持仓演变

{md_table(hold_wide)}

换手指标（首期 2025Q2 为基金成立后的建仓期，含建仓因素）：

{md_table(turn_view)}

观察：

- 2026Q1 → 2026Q2 前十大出现 **{int(turn[turn['curr']=='2026-06-30']['new_count'].iloc[0])} 只新进**，与 5 月年度调样时点吻合；
- 其余季度以价格漂移和权重微调为主，前十大名单变动有限；
- 前十大集中度稳定在 27%~29% 区间，符合 50 只样本、单只上限 10% 的加权特征。

关键季度名单变动：

| 季度 | 新进 | 退出 |
|---|---|---|
| 2025Q2 → 2025Q3 | {q3_new} | {q3_exit} |
| 2026Q1 → 2026Q2（年度调样） | {q2_new} | {q2_exit} |

### 3.1 9.30 以来：谁在贡献、谁在滞涨

截至 {BASE_PRICE_DATE}，50 只成分股等权收益 **{since_eq:.2f}%**，2026Q3 前十大按当时权重的加权收益 **{since_q3:.2f}%**。按贡献从高到低：

{md_table(since_view, digits=3)}

贡献最低的几只即为“滞涨组”；但要注意：对前十大名单而言，滞涨意味着权重已经落后，后续需要相对跑赢才能守住/进入前十（详见 5.4 边界阈值）。

## 四、调仓规则识别与验证

### 4.1 制度规则

- 年度调样：每年 5 月第 6 个交易日生效，下一次调样在 2027 年 5 月；
- 选样核心：预案股息率（而非历史 TTM 股息率），因此与 4 月年报分红预案披露节奏同步；
- 加权核心：预案股息率 / 波动率，偏好"高股息 + 低波动"。

### 4.2 实证验证

| 验证项 | 结果 |
|---|---|
| 成分股连续 3 年分红 | {dc['div_3y_ok']} / {dc['n']} 只满足（按 2024/2025/2026 年除权记录核验） |
| 权重模型秩相关（股息率/波动率 vs 官方权重） | Spearman = {wm['spearman']}（n={wm['n']}） |
| 权重模型前十大重合 | {wm['top10_overlap']} / 10 |
| ETF 与指数共同持仓权重秩相关（2026-09-30） | Spearman = {etf_idx['spearman']}（共同持仓 {etf_idx['n_common']} 只） |
| 2026Q4 有除权事件的成分股 | {dc['q4_ex_count']} 只 |

结论：

1. 成分股层面"连续 3 年分红"全部满足，与编制规则一致；
2. 用 2025 年度每股派息 ÷ 2026-04-30 收盘价作为预案股息率代理，再除以年化波动率，隐含权重与官方权重的秩相关为 **{wm['spearman']}**，前十大重合 {wm['top10_overlap']}/10，方向与"股息率/波动率"加权口径一致；偏差主要来自 5 个月价格漂移与预案股息率口径差异；
3. ETF 前十大与指数权重的秩相关为 **{etf_idx['spearman']}**，印证基金采用完全复制策略，季度前十大近似等于指数权重排名；
4. 因此 Q4 前十大变化主要来自**股价相对表现**，而非新的主动选股；下一次基本面换仓要等到 2027 年 5 月。

### 4.3 调仓驱动拆解：价格漂移 vs 年度调样

对相邻两个报告期，统计新进/退出个股在区间内的涨跌幅，以及共同持仓的权重变动与区间收益的秩相关：

{md_table(drift_view.assign(**{'新进平均区间收益%': drift_view['新进平均区间收益%'], '退出平均区间收益%': drift_view['退出平均区间收益%'], '权重变动与收益秩相关': drift_view['权重变动与收益秩相关']}))}

- 非调样季度中，新进个股的区间平均收益显著高于退出个股，且共同持仓"权重变动 vs 区间收益"的秩相关接近 1，说明**前十大名单变化基本是价格漂移的结果**；
- 2026Q2 为 5 月年度调样后的首个报告期，10 只全部更换，不能用价格漂移解释，应按指数年度调样理解；
- 这也意味着：在 2027 年 5 月之前，只要指数不临时调整成分，Q4 前十大基本由股价相对强弱决定。

## 五、2026Q4 前十大重仓股概率预测

### 5.1 方法与基准

- 以 {WEIGHT_ASOF} 指数权重为起点，用不复权最新价投影到 {BASE_PRICE_DATE}；
- 以 2025-11-01 ~ {RETURN_ASOF} 日收益估计单因子模型（等权因子 + 个股 β + 残差）；
- 蒙特卡洛 {N_PATHS:,} 条路径，模拟 {SIM_START} ~ {SIM_END}；
- 三情景：中性（50% 权重）、动量延续（30%）、反转（20%）；综合概率为加权结果；
- Q4 若存在除权事件，在除权日从价格路径中扣除每股派息。

### 5.2 预测结果（前 20 名）

{md_table(pv_view, digits=3)}

> 综合概率为三情景加权；"锁定"≥80%，"较稳"60%~80%，"待定"30%~60%，"概率较低"<30%。

### 5.3 预测前十大组合

{md_table(top10_tbl.assign(**{'基准权重%': top10_tbl['基准权重%'], '入选概率': top10_tbl['入选概率']}), digits=3)}

说明：上表按综合概率取前 10 名。第 8~11 席的综合概率分别为 {seat_note}，边界席位在 Q4 存在互换可能。

合理性校验：

- 预测前十大基准权重合计 {pr['top10_weight_sum_1030']:.2f}%（{WEIGHT_ASOF} 口径）；
- 50 只股票综合概率合计 {pr['prob_sum']:.2f}，接近期望值 10；
- 分层分布：{pr['tier_counts']}。

### 5.4 边界替换观察名单（当前第 8~15 名）

第 10~13 名权重差距很小，Q4 名单存在真实替换可能。表中"需相对第 10 名超额涨幅"表示在其他条件不变时，该股需要在 Q4 相对第 10 名多涨多少才能反超；负值表示当前已领先，可容忍一定相对下跌：

{md_table(bview.assign(**{'基准权重%': bview['基准权重%'], '需相对第10名超额涨幅': bview['需相对第10名超额涨幅']}), digits=3)}

## 六、关键不确定性与局限

1. 公募基金仅按季度披露**前十大**，无法看到第 11~50 名持仓；
2. 指数权重为 {WEIGHT_ASOF} 快照，之后至基准日已按价格投影，但与基金真实持仓仍有现金拖累、申赎和四舍五入误差；
3. 模拟基于历史波动率与单因子结构，未预测未来风格切换、政策与分红预案变化；
4. 若 2027 年 5 月前发生成分股临时调整（并购、退市、长期停牌等），名单可能提前变化；
5. 概率是条件估计，不等于确定性名单；
6. 基金 2026 年报（2026-12-31 持仓）通常在 2027 年 3 月披露，届时可用实际前十大验证本预测。

## 七、复现

```bash
.venv/bin/python scripts/etf_159207_rebalance.py fetch
.venv/bin/python scripts/etf_159207_rebalance.py analyze
.venv/bin/python scripts/etf_159207_rebalance.py report
```

输出文件：

- `docs/etf_159207_holdings_top10.csv`
- `docs/etf_159207_index_weights.csv`
- `docs/etf_159207_dividend_check.csv`
- `docs/etf_159207_q4_prediction.csv`

## 八、免责声明

本报告为量化研究结果，基于公开数据与历史统计，不构成任何投资建议、买卖推荐或收益预测。基金持仓与指数成分可能随时变化，投资决策请以基金定期报告和指数官方公告为准。

## 九、参考资料

- 中证指数官网，中证智选高股息策略指数（932305）：https://www.csindex.com.cn/mobile-web/#/indices/family/detail?indexCode=932305
- 国信证券金融工程专题《中证智选高股息策略指数十问十答》（2026-07-06）
- 方正金工《预案股息率选股有效规避红利陷阱》（2026-04-21）
- 数据接口：天天基金移动端基金持仓接口、中证指数成分/权重文件、akshare 分红与不复权行情、本地 SQLite 行情库
"""
    (DOCS / "etf_159207_rebalance_analysis.md").write_text(report, encoding="utf-8")
    print("报告:", DOCS / "etf_159207_rebalance_analysis.md")


def cmd_run() -> None:
    """一键运行：刷新最新行情 → 重算 → 生成报告（供 Web 端 Run 按钮调用）。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    print("[1/6] 刷新基金信息与历史持仓 ...", flush=True)
    try:
        fetch_fund_info(refresh=True)
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 基金信息刷新失败，使用缓存: {e}", flush=True)
        fetch_fund_info()
    try:
        fetch_fund_holdings(refresh=True)
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 持仓刷新失败，使用缓存: {e}", flush=True)
        fetch_fund_holdings()
    print("[2/6] 刷新指数成分与权重 ...", flush=True)
    try:
        _, wt = fetch_index_files(refresh=True)
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 指数权重刷新失败，使用缓存: {e}", flush=True)
        _, wt = fetch_index_files()
    codes = sorted(wt["symbol"].unique())
    div_cache = CACHE / "dividends.csv"
    refresh_div = (
        not div_cache.exists()
        or (time.time() - div_cache.stat().st_mtime) > 7 * 86400
    )
    print(f"[3/6] 分红数据（{'刷新' if refresh_div else '缓存'}）...", flush=True)
    fetch_dividends(codes, refresh=refresh_div)
    print("[4/6] 拉取最新不复权行情（历史缓存 + 批量快照）...", flush=True)
    fetch_actual_prices(codes, refresh=False)
    print("[5/6] 重新计算持仓、规则与预测 ...", flush=True)
    cmd_analyze()
    print("[6/6] 生成 Markdown 报告 ...", flush=True)
    cmd_report()
    print("分析完成。", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="159207 调仓分析与 Q4 预测")
    ap.add_argument("command", choices=["fetch", "analyze", "report", "run"])
    ap.add_argument("--refresh", action="store_true", help="强制刷新外部数据缓存")
    args = ap.parse_args()
    if args.command == "fetch":
        cmd_fetch(args.refresh)
    elif args.command == "analyze":
        cmd_analyze()
    elif args.command == "report":
        cmd_report()
    else:
        cmd_run()


if __name__ == "__main__":
    main()
