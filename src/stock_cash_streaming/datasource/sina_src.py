"""新浪数据源适配器：板块成分 + 个股逐日四档资金流历史。

- 板块成分：`ak.stock_sector_detail(sector=<label>)`（label 见 config/sectors.yaml）。
- 个股资金流：`MoneyFlow.ssl_qsfx_lscjfb`，分页 num=1000 可取多年历史。
  字段：opendate/trade/changeratio/turnover/netamount/ratioamount/r0_net..r3_net
  其中 r0/r1/r2/r3 分别对应 超大单/大单/中单/小单。
"""

import time
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd
import requests
import yaml

from ..common.logging import logger
from .akshare_src import infer_market

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SECTORS_PATH = PROJECT_ROOT / "config" / "sectors.yaml"
SINA_FF_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
    "json_v2.php/MoneyFlow.ssl_qsfx_lscjfb"
)
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def sector_label(name: str) -> str | None:
    """从 config/sectors.yaml 读取板块对应的新浪 label。"""
    try:
        cfg = yaml.safe_load(SECTORS_PATH.read_text(encoding="utf-8"))
        for s in cfg.get("sectors", []):
            if s["name"] == name:
                return s.get("sina_label")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取 sectors.yaml 失败: {e}")
    return None


def sina_board_members(label: str) -> pd.DataFrame:
    """新浪板块成分股。"""
    df = ak.stock_sector_detail(sector=label)
    out = pd.DataFrame({
        "symbol": df["code"].astype(str).str.zfill(6),
        "name": df["name"].astype(str),
    })
    return out.drop_duplicates("symbol").reset_index(drop=True)


def sina_stock_fund_flow(symbol: str, max_pages: int = 30) -> pd.DataFrame:
    """个股逐日四档资金流历史（元）。"""
    prefix = f"{infer_market(symbol)}{symbol}"
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        url = f"{SINA_FF_URL}?daima={prefix}&page={page}&num=1000"
        try:
            data = requests.get(url, headers=_HEADERS, timeout=25).json()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"新浪资金流失败 {symbol} page={page}: {e}")
            break
        if not data:
            break
        rows += data
        if len(data) < 1000:
            break
        time.sleep(0.2)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).drop_duplicates("opendate").sort_values("opendate")
    num = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731

    out = pd.DataFrame({
        "trade_date": df["opendate"].astype(str),
        "close": num("trade"),
        "pct_chg": num("changeratio") * 100,
        "xl_net": num("r0_net"),
        "l_net": num("r1_net"),
        "m_net": num("r2_net"),
        "s_net": num("r3_net"),
    })
    out["main_net"] = out["xl_net"].fillna(0) + out["l_net"].fillna(0)
    # 成交额 ≈ 净额 / 净额占比；再据此计算主力净流入占比
    amount = num("netamount") / num("ratioamount").replace(0, np.nan)
    out["main_ratio"] = (out["main_net"] / amount * 100).round(4)
    return out.reset_index(drop=True)
