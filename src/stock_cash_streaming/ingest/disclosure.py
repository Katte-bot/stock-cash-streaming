"""财报公告日、分红与上市日数据源（巨潮资讯 / 新浪）。

背景：Baostock 官方客户端的 `send_msg` 在连接被关闭或响应粘包时会无限阻塞，
本机实测 `query_profit_data` / `query_dividend_data` 因此不可用。这里改用：

- 公告日：巨潮资讯公告查询接口（按年报/半年报/一季报/三季报分类，取最早已披露日）
- 分红：新浪「发行与分配-分红配股详情」
- 上市日：新浪「公司资料」

所有请求都带超时与重试，单只失败只影响该标的，不会拖住整个同步流程。
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from io import StringIO

import pandas as pd
import requests

ORG_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
SINA_DIVIDEND_URL = ("https://vip.stock.finance.sina.com.cn/corp/go.php/"
                     "vISSUE_ShareBonus/stockid/{symbol}.phtml")
SINA_INFO_URL = ("https://vip.stock.finance.sina.com.cn/corp/go.php/"
                 "vCI_CorpInfo/stockid/{symbol}.phtml")

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "http://www.cninfo.com.cn/",
}
TIMEOUT = (10, 25)

# 公告分类 -> (标题正则, 报告期模板)
REPORT_CATEGORIES = {
    "年报": (re.compile(r"(20\d{2})\s*年?\s*年度报告\s*$"), "{year}-12-31"),
    "半年报": (re.compile(r"(20\d{2})\s*年?\s*半年度报告\s*$"), "{year}-06-30"),
    "一季报": (re.compile(r"(20\d{2})\s*年?\s*第一季度报告\s*$"), "{year}-03-31"),
    "三季报": (re.compile(r"(20\d{2})\s*年?\s*第三季度报告\s*$"), "{year}-09-30"),
}
CATEGORY_CODES = {
    "年报": "category_ndbg_szsh",
    "半年报": "category_bndbg_szsh",
    "一季报": "category_yjdbg_szsh",
    "三季报": "category_sjdbg_szsh",
}
# 排除摘要、更正、披露提示等非正式报告，避免把公告日提前
_EXCLUDE_WORDS = ("摘要", "英文", "更正", "补充", "问询", "说明", "意见", "审计",
                  "披露", "预约", "提示", "取消", "关于")


def _get(url: str, **kwargs) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            return requests.get(url, timeout=TIMEOUT, headers=UA, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"GET 失败 {url}: {last_err}")


def _post(url: str, payload: dict) -> dict:
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            r = requests.post(url, data=payload, timeout=TIMEOUT, headers=UA)
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"POST 失败 {url}: {last_err}")


@lru_cache(maxsize=1)
def _org_ids() -> dict[str, str]:
    """巨潮股票代码 -> orgId（进程内缓存，约 6000 条）。"""
    js = _get(ORG_URL).json()
    return {x["code"]: x["orgId"] for x in js.get("stockList", [])}


def _clean_title(title: str) -> str:
    return re.sub(r"<[^>]+>", "", title or "").strip()


def _stat_date(category: str, title: str) -> str | None:
    pattern, template = REPORT_CATEGORIES[category]
    if any(w in title for w in _EXCLUDE_WORDS):
        return None
    m = pattern.search(title)
    if not m:
        return None
    year = int(m.group(1))
    if year < 2000 or year > 2100:
        return None
    return template.format(year=year)


def fetch_report_pub_dates(symbol: str, start_year: int = 2017,
                           end_year: int | None = None) -> dict[str, str]:
    """返回 {报告期: 最早公告日}，报告期与公告日格式均为 YYYY-MM-DD。

    只保留正式报告标题（排除摘要/更正/披露提示），同一报告期取最早公告日。
    """
    end_year = end_year or time.localtime().tm_year
    org_id = _org_ids().get(symbol)
    if not org_id:
        return {}
    stock = f"{symbol},{org_id}"
    out: dict[str, str] = {}
    for category, cat_code in CATEGORY_CODES.items():
        for page in range(1, 21):
            js = _post(QUERY_URL, {
                "pageNum": page,
                "pageSize": 30,
                "column": "szse",
                "tabName": "fulltext",
                "plate": "",
                "stock": stock,
                "searchkey": "",
                "secid": "",
                "category": cat_code,
                "trade": "",
                "seDate": f"{start_year}-01-01~{end_year + 1}-12-31",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            })
            anns = js.get("announcements") or []
            for a in anns:
                title = _clean_title(a.get("announcementTitle", ""))
                stat = _stat_date(category, title)
                ts = a.get("announcementTime")
                if not stat or not ts:
                    continue
                date = time.strftime("%Y-%m-%d", time.localtime(ts / 1000))
                if stat < f"{start_year}-01-01" or stat > f"{end_year}-12-31":
                    continue
                if stat not in out or date < out[stat]:
                    out[stat] = date
            if len(anns) < 30:
                break
    return out


def fetch_dividends(symbol: str, start_year: int = 2017,
                    end_year: int | None = None) -> list[dict]:
    """新浪分红配股详情，返回 fact_dividend 所需字段。"""
    end_year = end_year or time.localtime().tm_year
    html = _get(SINA_DIVIDEND_URL.format(symbol=symbol)).text
    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:
        return []
    df = None
    for t in tables:
        try:
            cols = [c[2] for c in t.columns.tolist()]
        except (IndexError, TypeError):
            continue
        if "公告日期" in cols and any(c.startswith("派息") for c in cols):
            df = t.copy()
            df.columns = cols
            break
    if df is None:
        return []
    df = df.drop(columns=["查看详细"], errors="ignore")
    if df.empty or str(df.iloc[0, 0]).strip() == "暂时没有数据！":
        return []
    rename: dict[str, str] = {}
    for col in df.columns:
        if col == "公告日期":
            rename[col] = "announce_date"
        elif col == "除权除息日":
            rename[col] = "ex_date"
        elif col.startswith("送股"):
            rename[col] = "stock_gift"
        elif col.startswith("转增"):
            rename[col] = "stock_transfer"
        elif col.startswith("派息"):
            rename[col] = "cash"
    df = df.rename(columns=rename)[
        ["announce_date", "ex_date", "stock_gift", "stock_transfer", "cash"]]
    df["announce_date"] = pd.to_datetime(df["announce_date"], format="%Y-%m-%d",
                                         errors="coerce")
    df["ex_date"] = pd.to_datetime(df["ex_date"], format="%Y-%m-%d", errors="coerce")
    for col in ("stock_gift", "stock_transfer", "cash"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    lo, hi = pd.Timestamp(f"{start_year}-01-01"), pd.Timestamp(f"{end_year}-12-31")
    df = df[df["announce_date"].between(lo, hi)]
    rows: list[dict] = []
    for r in df.itertuples(index=False):
        cash = float(r.cash or 0) / 10.0
        stock = (float(r.stock_gift or 0) + float(r.stock_transfer or 0)) / 10.0
        if cash <= 0 and stock <= 0:
            continue
        rows.append({
            "symbol": symbol,
            "announce_date": r.announce_date.strftime("%Y-%m-%d"),
            "ex_date": "" if pd.isna(r.ex_date) else r.ex_date.strftime("%Y-%m-%d"),
            "cash_ps": cash,
            "stock_ps": stock,
        })
    return rows


def fetch_ipo_date(symbol: str) -> str | None:
    """新浪公司资料页的上市日期。"""
    html = _get(SINA_INFO_URL.format(symbol=symbol)).text
    m = re.search(r"上市日期[：:]\s*</td>\s*<td[^>]*>.*?(\d{4}-\d{2}-\d{2})", html, re.S)
    return m.group(1) if m else None
