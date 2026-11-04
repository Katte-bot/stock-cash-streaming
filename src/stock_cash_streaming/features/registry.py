"""因子注册表：因子口径、方向、版本与依赖的唯一来源。

用途：
- 策略库按名称声明依赖因子；
- 因子实验室展示元数据与分组；
- 自然语言「因子配置」模式做白名单校验；
- 计算层按注册表选择需要落库/缓存的因子。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FactorSpec:
    name: str
    label: str
    category: str            # risk / value / quality / investment / momentum / reversal / trend / liquidity / event / flow / size
    version: str             # v2 / v3 / v4 / v5
    higher_is_better: bool
    description: str = ""
    lookback: int = 0
    deps: tuple[str, ...] = field(default_factory=tuple)


_REGISTRY: dict[str, FactorSpec] = {}


def register(spec: FactorSpec) -> FactorSpec:
    if spec.name in _REGISTRY:
        raise ValueError(f"因子重复注册：{spec.name}")
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> FactorSpec | None:
    return _REGISTRY.get(name)


def all_names() -> list[str]:
    return sorted(_REGISTRY)


def by_version(version: str) -> list[FactorSpec]:
    return [s for s in _REGISTRY.values() if s.version == version]


def by_category(category: str) -> list[FactorSpec]:
    return [s for s in _REGISTRY.values() if s.category == category]


def as_dicts() -> list[dict]:
    return [
        {
            "name": s.name, "label": s.label, "category": s.category,
            "version": s.version, "higher_is_better": s.higher_is_better,
            "description": s.description, "lookback": s.lookback,
            "deps": list(s.deps),
        }
        for s in sorted(_REGISTRY.values(), key=lambda x: (x.version, x.category, x.name))
    ]


def _r(name, label, category, version, higher, desc="", lookback=0, deps=()):
    register(FactorSpec(name, label, category, version, higher, desc, lookback, tuple(deps)))


# ---------- v2 技术/资金流（已落库） ----------
_r("ret_5", "5日收益", "momentum", "v2", True, "近 5 个交易日累计收益（%）", 5)
_r("ret_20", "20日收益", "momentum", "v2", True, "近 20 个交易日累计收益（%）", 20)
_r("ma5_ratio", "偏离MA5", "trend", "v2", True, "收盘相对 5 日均线偏离", 5)
_r("ma20_ratio", "偏离MA20", "trend", "v2", True, "收盘相对 20 日均线偏离", 20)
_r("boll_pos", "布林位置", "reversal", "v2", False, "布林带内相对位置（0=下轨，1=上轨）", 20)
_r("rsi14", "RSI14", "reversal", "v2", False, "14 日相对强弱指标", 14)
_r("macd_hist", "MACD柱", "momentum", "v2", True, "MACD 柱（DIF-DEA）", 26)
_r("vol_20", "20日波动", "risk", "v2", False, "20 日年化波动率", 20)
_r("turnover_5", "5日换手", "liquidity", "v2", False, "5 日均换手率", 5)
_r("ma60_ratio", "偏离MA60", "trend", "v2", True, "收盘相对 60 日均线偏离", 60)
_r("amount_20d", "20日成交额", "liquidity", "v2", False, "20 日日均成交额", 20)
_r("max_ret_20", "20日最大单日涨幅", "risk", "v2", False, "彩票效应代理", 20)
_r("turnover_std_20", "换手波动", "liquidity", "v2", False, "20 日换手率标准差", 20)
_r("amihud_20", "Amihud非流动性", "liquidity", "v2", True, "|收益|/成交额 的 20 日均值", 20)
_r("main_net_5d", "5日主力净流入", "flow", "v2", True, "5 日主力净流入", 5)
_r("main_net_ratio_5d", "5日主力净流入占比", "flow", "v2", True, "5 日主力净流入/成交额", 5)
_r("inflow_days_5", "5日流入天数", "flow", "v2", True, "近 5 日主力净流入为正的天数", 5)
_r("institution_trend", "机构趋势", "flow", "v2", True, "10 日主力净流入均值/绝对值均值", 10)
_r("xl_ratio_5d", "5日超大单占比", "flow", "v2", True, "超大单净额/成交额的 5 日均值", 5)

# ---------- v3 基本面（已落库） ----------
_r("roe_ttm", "ROE(TTM)", "quality", "v3", True, "净资产收益率（TTM）")
_r("roe_std_8q", "ROE稳定性", "quality", "v3", False, "过去 8 季 ROE 标准差")
_r("cfo_to_np", "现金流/净利润", "quality", "v3", True, "经营现金流 TTM / 净利润 TTM")
_r("cfo_to_np_std_8q", "现金利润匹配稳定性", "quality", "v3", False, "过去 8 季 CFO/NP 标准差")
_r("cfo_to_rev", "现金流/营收", "quality", "v3", True, "经营现金流 TTM / 营收 TTM")
_r("leverage", "资产负债率", "quality", "v3", False, "负债/资产")
_r("ep_ttm", "盈利收益率", "value", "v3", True, "净利润 TTM / 总市值")
_r("bp_mrq", "账面市值比", "value", "v3", True, "净资产 / 总市值")
_r("fcf_yield", "自由现金流收益率", "value", "v3", True, "每股自由现金流 / 收盘价")
_r("div_yield_3y", "股息率(3年)", "value", "v3", True, "近 3 年现金分红 / 收盘价")
_r("div_continuity_3y", "分红连续性", "value", "v3", True, "近 3 年分红年份占比")
_r("div_payout", "分红率", "value", "v3", False, "分红 / 每股收益")

# ---------- v4 技术/交易（研究因子，按需计算） ----------
_r("vol_60", "60日波动", "risk", "v4", False, "60 日年化波动率", 60)
_r("vol_120", "120日波动", "risk", "v4", False, "120 日年化波动率", 120)
_r("vol_250", "250日波动", "risk", "v4", False, "250 日年化波动率", 250)
_r("downside_vol_120", "下行波动", "risk", "v4", False, "仅负收益的 120 日年化波动率", 120)
_r("beta_250", "Beta", "risk", "v4", False, "对等权市场的 250 日滚动 Beta", 250)
_r("ivol_60", "特质波动", "risk", "v4", False, "剔除市场收益后的 60 日残差波动", 60)
_r("ret_60", "60日收益", "momentum", "v4", True, "近 60 个交易日累计收益（%）", 60)
_r("ret_120", "120日收益", "momentum", "v4", True, "近 120 个交易日累计收益（%）", 120)
_r("ret_200", "200日收益", "momentum", "v4", True, "近 200 个交易日累计收益（%）", 200)
_r("resid_mom_126", "残差动量(6月)", "momentum", "v4", True, "剔除市场/行业后 126 日残差累计（剔除最近 21 日）", 147)
_r("resid_mom_252", "残差动量(12月)", "momentum", "v4", True, "剔除市场/行业后 252 日残差累计（剔除最近 21 日）", 273)
_r("resid_ret_5", "残差收益(5日)", "reversal", "v4", False, "剔除市场/行业后 5 日残差收益", 5)
_r("resid_ret_10", "残差收益(10日)", "reversal", "v4", False, "剔除市场/行业后 10 日残差收益", 10)
_r("resid_ret_20", "残差收益(20日)", "reversal", "v4", False, "剔除市场/行业后 20 日残差收益", 20)
_r("ts_trend_60", "趋势(60日)", "trend", "v4", True, "收盘>MA60 且 MA60 上行", 60)
_r("ts_trend_120", "趋势(120日)", "trend", "v4", True, "收盘>MA120 且 MA120 上行", 120)
_r("dist_high_60", "距60日高点", "reversal", "v4", True, "收盘/60 日最高-1", 60)
_r("lower_shadow_20", "下影线比例", "reversal", "v4", True, "近 20 日下影线占振幅比例均值", 20)
_r("volume_shrink_5_20", "缩量", "reversal", "v4", False, "5 日均量/20 日均量", 20)
_r("amihud_std_20", "非流动性波动", "liquidity", "v4", True, "Amihud 的 20 日标准差", 20)

# ---------- v5 基本面/事件（研究因子，按需计算） ----------
_r("roa_ttm", "ROA(TTM)", "quality", "v5", True, "净利润 TTM / 总资产", deps=("total_assets",))
_r("roic", "ROIC", "quality", "v5", True, "净利润 TTM /（净资产+有息负债）", deps=("total_assets",))
_r("gross_margin", "毛利率", "quality", "v5", True, "毛利/营收")
_r("net_margin", "净利率", "quality", "v5", True, "净利润/营收")
_r("accruals", "应计利润", "quality", "v5", False, "(净利润-经营现金流)/总资产", deps=("total_assets",))
_r("total_assets", "总资产", "size", "v5", True, "资产总计（资产负债表）")
_r("size_mcap", "总市值", "size", "v5", True, "收盘价 × 股本")
_r("cfo_yield", "现金收益率", "value", "v5", True, "经营现金流 TTM / 总市值")
_r("asset_growth", "资产增长", "investment", "v5", False, "总资产同比", deps=("total_assets",))
_r("asset_turnover_change", "周转率变化", "investment", "v5", True, "总资产周转率同比变化", deps=("total_assets",))
_r("capex_growth", "资本开支增长", "investment", "v5", False, "购建长期资产支付现金同比", deps=("capex",))
_r("capex_to_assets", "资本开支/资产", "investment", "v5", False, "CAPEX TTM / 总资产", deps=("capex", "total_assets"))
_r("investment_intensity", "投资强度", "investment", "v5", False, "(CAPEX+资产增长)/营收增长", deps=("capex", "total_assets"))
_r("sue", "盈利惊喜(SUE)", "event", "v5", True, "单季净利润同比惊喜/过去 8 季标准差")
_r("revenue_surprise", "营收惊喜", "event", "v5", True, "单季营收同比惊喜")
_r("announce_ret_1d", "公告日异常收益", "event", "v5", True, "公告后首个交易日相对市场收益")
_r("attention_20", "公告后关注度", "event", "v5", True, "公告后 20 日换手/自身中位数")
