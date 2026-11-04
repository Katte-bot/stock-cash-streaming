"""申万行业分类同步（2021 版）。

数据源：申万宏源行业分类明细 `StockClassifyUse_stock.xls`（全市场个股 → 申万三级代码）。
说明：
- 一次请求即可拿到全部个股，避免逐行业拉成分（`sw_index_third_cons` 接口已不可用）；
- 行业代码为 6 位：前 2 位 = 一级、前 4 位 = 二级、完整 6 位 = 三级；
  文件名只有代码没有名称，一级名称用 `SW_L1_NAMES` 静态映射（2021 版 31 个一级行业）；
- 个股可能有多条历史记录，按「计入日期」取最新一条且不晚于当日，避免未来函数；
- 同一股票只保留一条记录；
- 供横截面行业中性化与行业对比使用，缺失时回退 THS 板块（dim_sector_member）。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..common.logging import logger
from ..storage.repository import Repository

SW_XLS_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"

# 申万 2021 版一级行业（代码前两位 → 名称）；历史遗留代码统一归入「未分类」
SW_L1_NAMES = {
    "11": "农林牧渔",
    "21": "采掘",
    "22": "基础化工",
    "23": "钢铁",
    "24": "有色金属",
    "27": "电子",
    "28": "汽车",
    "33": "家用电器",
    "34": "食品饮料",
    "35": "纺织服饰",
    "36": "轻工制造",
    "37": "医药生物",
    "41": "公用事业",
    "42": "交通运输",
    "43": "房地产",
    "44": "商贸零售",
    "45": "商贸零售",
    "46": "社会服务",
    "48": "银行",
    "49": "非银金融",
    "51": "综合",
    "61": "建筑材料",
    "62": "建筑装饰",
    "63": "电力设备",
    "64": "机械设备",
    "65": "国防军工",
    "71": "计算机",
    "72": "传媒",
    "73": "通信",
    "74": "煤炭",
    "75": "石油石化",
    "76": "环保",
    "77": "美容护理",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_symbol(x: str) -> str | None:
    s = str(x).strip().split(".")[0].zfill(6)
    return s if len(s) == 6 and s.isdigit() and s[0] in "036" else None


def fetch_sw_class() -> pd.DataFrame:
    """下载申万分类明细并按个股取最新一条（列：symbol / level1_code / level2_code / level3_code）。"""
    import io

    import requests
    import urllib3

    urllib3.disable_warnings()
    resp = requests.get(
        SW_XLS_URL, verify=False, timeout=120, headers={"User-Agent": "Mozilla/5.0"}
    )
    resp.raise_for_status()
    raw = pd.read_excel(io.BytesIO(resp.content),
                        dtype={"股票代码": "str", "行业代码": "str"})
    raw = raw.rename(columns={"股票代码": "symbol", "行业代码": "industry_code",
                              "计入日期": "start_date"})
    raw["symbol"] = raw["symbol"].fillna("").astype(str).str.zfill(6)
    raw["industry_code"] = raw["industry_code"].fillna("").astype(str)
    raw["start_date"] = pd.to_datetime(raw["start_date"], errors="coerce")
    raw = raw[raw["industry_code"].str.len() == 6]

    today = pd.Timestamp(datetime.now().date())
    raw = raw[raw["start_date"].isna() | (raw["start_date"] <= today)]
    latest = (raw.sort_values(["symbol", "start_date"], na_position="first")
                 .groupby("symbol", as_index=False).tail(1))
    latest["level1_code"] = latest["industry_code"].str[:2]
    latest["level2_code"] = latest["industry_code"].str[:4]
    latest["level3_code"] = latest["industry_code"]
    return latest[["symbol", "level1_code", "level2_code", "level3_code"]]


def sync_sw_industry(repo: Repository, sleep: float | None = None,
                     limit: int | None = None) -> dict:
    """拉取申万分类并写入 dim_stock_industry（sleep/limit 保留签名兼容）。"""
    latest = fetch_sw_class()
    now = _now()
    rows: dict[str, dict] = {}
    for r in latest.itertuples():
        sym = _clean_symbol(r.symbol)
        if not sym or sym in rows:
            continue
        rows[sym] = {
            "symbol": sym,
            "level1": SW_L1_NAMES.get(r.level1_code, "未分类"),
            "level2": r.level2_code,
            "level3": r.level3_code,
            "src": "sw",
            "updated_at": now,
        }
        if limit and len(rows) >= limit:
            break

    if rows:
        repo.upsert("dim_stock_industry", list(rows.values()), ["symbol"])
    repo.set_watermark("dim_stock_industry", now[:10])
    logger.info(f"申万行业同步完成：股票 {len(rows)} 只")
    return {"industries_ok": len(rows), "industries_fail": 0, "symbols": len(rows)}


def industry_map(repo: Repository) -> dict[str, str]:
    """symbol -> 一级行业（缺失时回退 THS 板块代码）。"""
    out = {r["symbol"]: r["level1"] for r in repo.query(
        "SELECT symbol, level1 FROM dim_stock_industry WHERE level1 IS NOT NULL")}
    for r in repo.query("SELECT symbol, sector_code FROM dim_sector_member"):
        out.setdefault(r["symbol"], r["sector_code"])
    return out
