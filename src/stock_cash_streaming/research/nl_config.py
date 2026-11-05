"""自然语言「因子配置」模式：中文描述 → 白名单因子权重 → 确定性回测。

与规则模式（research/nl_rules.py）的区别：
- 规则模式描述「连阴/MACD 金叉」等择时条件；
- 因子配置模式描述「低波动 25% + 价值 25% + 质量 30% + 保守投资 20%」等权重。
两者都不执行 LLM 生成的代码，只输出受控 JSON。
"""

from __future__ import annotations

import json
import re

from ..common.logging import logger
from ..config.settings import get_settings
from ..storage.repository import Repository
from .nl_rules import SpecError, StrategyParseError
from .runner import run_strategy

GROUP_FACTORS = {
    "low_volatility": ["vol_120", "downside_vol_120", "ivol_60"],
    "value": ["ep_ttm", "bp_mrq", "cfo_yield", "fcf_yield", "div_yield_3y"],
    "quality": ["roe_ttm", "roa_ttm", "roic", "cfo_to_np", "gross_margin", "accruals"],
    "conservative_investment": ["asset_growth", "capex_to_assets",
                                "investment_intensity", "asset_turnover_change"],
    "residual_momentum": ["resid_mom_252", "resid_mom_126"],
    "pead": ["sue", "revenue_surprise", "announce_ret_1d", "attention_20"],
    "reversal": ["resid_ret_5", "resid_ret_10", "boll_pos", "rsi14"],
    "trend": ["ts_trend_60", "ts_trend_120", "ret_60"],
    "liquidity": ["amihud_20", "amount_20d"],
}

METRIC_ALIASES = {
    "EP": "ep_ttm", "BP": "bp_mrq", "FCF_YIELD": "fcf_yield", "CFO_YIELD": "cfo_yield",
    "DIV_YIELD": "div_yield_3y", "ROE": "roe_ttm", "ROA": "roa_ttm", "ROIC": "roic",
    "CFO_TO_NP": "cfo_to_np", "GROSS_MARGIN": "gross_margin", "ACCRUALS": "accruals",
    "ASSET_GROWTH": "asset_growth", "CAPEX_GROWTH": "capex_growth",
    "CAPEX": "capex_to_assets", "CAPEX_TO_ASSETS": "capex_to_assets",
    "INVESTMENT_INTENSITY": "investment_intensity",
    "ASSET_TURNOVER_CHANGE": "asset_turnover_change",
    "SUE": "sue", "REVENUE_SURPRISE": "revenue_surprise",
    "ANNOUNCE_RET": "announce_ret_1d", "ATTENTION": "attention_20",
    "RESID_RET_5": "resid_ret_5", "RESID_RET_10": "resid_ret_10",
    "BOLL_POS": "boll_pos", "RSI14": "rsi14",
    "TS_TREND_60": "ts_trend_60", "TS_TREND_120": "ts_trend_120", "RET_60": "ret_60",
    "AMIHUD_20": "amihud_20", "AMOUNT_20D": "amount_20d",
    "RESID_MOM_252": "resid_mom_252", "RESID_MOM_126": "resid_mom_126",
    "VOL_120": "vol_120", "DOWN_VOL": "downside_vol_120", "IVOL": "ivol_60",
    "DIV_YIELD": "div_yield_3y",
}

ALLOWED_UNIVERSE = {"A0", "C0", "C1", "C2", "C3"}
MAX_SINGLE_WEIGHT = 0.40

_SYSTEM_PROMPT = """你是 A 股量化策略配置生成器。把用户的中文需求转换成 JSON（只输出 JSON，不要解释）。
输出格式：
{
  "strategy_name": "≤20 字",
  "mode": "factor",
  "universe": {"id": "A0", "min_listing_days": 250, "min_amount_20d": 20000000},
  "factors": [
    {"name": "low_volatility", "weight": 0.25, "direction": "ascending", "metrics": ["VOL_120"]}
  ],
  "portfolio": {"stock_count": 10, "rebalance": "monthly", "weighting": "equal"},
  "backtest": {"start_date": "2018-01-01", "out_of_sample": true}
}

可选因子组（name 只能取这些；metrics 只能取括号内的大写别名）：
- low_volatility（VOL_120 / DOWN_VOL / IVOL）
- value（EP / BP / CFO_YIELD / FCF_YIELD / DIV_YIELD）
- quality（ROE / ROA / ROIC / CFO_TO_NP / GROSS_MARGIN / ACCRUALS）
- conservative_investment（ASSET_GROWTH / CAPEX_TO_ASSETS / INVESTMENT_INTENSITY / ASSET_TURNOVER_CHANGE）
- residual_momentum（RESID_MOM_252 / RESID_MOM_126）
- pead（SUE / REVENUE_SURPRISE / ANNOUNCE_RET / ATTENTION）
- reversal（RESID_RET_5 / RESID_RET_10 / BOLL_POS / RSI14）
- trend（TS_TREND_60 / TS_TREND_120 / RET_60）
- liquidity（AMIHUD_20 / AMOUNT_20D）

规则：
- factors 权重之和为 1，单组上限 0.4；
- stock_count 1~30；rebalance 只能是 daily / weekly / monthly；
- universe.id 只能是 A0 / C0 / C1 / C2 / C3；
- metrics 可省略（表示使用该组全部指标），但不要输出上面列表之外的名称；
- 用户没提到的参数用默认值；
- 不要输出额外字段，不要输出代码。"""


def _num(v, name: str, lo: float, hi: float, default: float | None = None) -> float:
    if v is None:
        if default is None:
            raise SpecError(f"{name} 缺失")
        return default
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise SpecError(f"{name} 必须是数字") from e
    if not (lo <= f <= hi):
        raise SpecError(f"{name} 需在 {lo}~{hi} 之间")
    return f


def validate_config(raw: dict) -> dict:
    """校验并归一化因子配置。"""
    if not isinstance(raw, dict):
        raise SpecError("配置必须是 JSON 对象")
    universe = raw.get("universe") or {}
    if not isinstance(universe, dict):
        raise SpecError('universe 必须是对象，如 {"id": "C1"}')
    uid = str(universe.get("id") or "A0").strip().upper()
    if uid not in ALLOWED_UNIVERSE and not re.fullmatch(r"U\d+", uid):
        raise SpecError(f"universe.id 不支持：{uid}")
    factors_in = raw.get("factors")
    if not isinstance(factors_in, list) or not factors_in:
        raise SpecError("factors 不能为空")
    factors = []
    total = 0.0
    for item in factors_in:
        if not isinstance(item, dict):
            raise SpecError("factors 的每一项都必须是对象")
        name = str(item.get("name") or "").strip()
        if name not in GROUP_FACTORS:
            raise SpecError(f"不支持的因子组：{name or '(空)'}")
        w = _num(item.get("weight"), f"{name}.weight", 0.0, 1.0)
        metrics = item.get("metrics") or []
        if metrics:
            mapped = [METRIC_ALIASES.get(str(m).upper(), str(m).lower()) for m in metrics]
            unknown = [m for m in mapped if m not in GROUP_FACTORS[name]]
            if unknown:
                raise SpecError(f"{name} 不支持指标：{', '.join(unknown)}")
            metrics = mapped
        factors.append({"name": name, "weight": w,
                        "direction": str(item.get("direction") or "ascending"),
                        "metrics": metrics})
        total += w
    if total <= 0:
        raise SpecError("因子权重之和必须大于 0")
    for f in factors:
        f["weight"] = round(f["weight"] / total, 6)
    # 单组上限
    for f in factors:
        if f["weight"] > MAX_SINGLE_WEIGHT + 1e-6:
            raise SpecError(f"{f['name']} 权重 {f['weight']:.0%} 超过单组上限 {MAX_SINGLE_WEIGHT:.0%}")

    pf = raw.get("portfolio") or {}
    rebalance = str(pf.get("rebalance") or "monthly").lower()
    if rebalance not in ("daily", "weekly", "monthly"):
        raise SpecError("rebalance 只能是 daily / weekly / monthly")
    stock_count = _num(pf.get("stock_count"), "stock_count", 1, 30, 10)
    bt = raw.get("backtest") or {}
    return {
        "strategy_name": str(raw.get("strategy_name") or "自然语言因子策略")[:40],
        "mode": "factor",
        "universe": {
            "id": uid,
            "min_listing_days": int(_num(universe.get("min_listing_days"),
                                         "min_listing_days", 0, 1000, 250)),
            "min_amount_20d": _num(universe.get("min_amount_20d"),
                                   "min_amount_20d", 0, 1e10, 2e7),
        },
        "factors": factors,
        "portfolio": {
            "stock_count": int(stock_count), "rebalance": rebalance,
            "weighting": str(pf.get("weighting") or "equal"),
        },
        "backtest": {
            "start_date": str(bt.get("start_date") or "2018-01-01"),
            "out_of_sample": bool(bt.get("out_of_sample", True)),
            "oos_start": bt.get("oos_start"),
        },
    }


def config_to_weights(config: dict) -> dict[str, float]:
    """把因子组权重展开为具体因子权重。"""
    out: dict[str, float] = {}
    for f in config["factors"]:
        names = f.get("metrics") or GROUP_FACTORS[f["name"]]
        per = f["weight"] / len(names)
        for n in names:
            out[n] = out.get(n, 0.0) + per
    return {k: round(v, 6) for k, v in out.items()}


def describe_config(config: dict) -> list[str]:
    from ..features.registry import get as factor_spec

    label = {"low_volatility": "低波动", "value": "价值", "quality": "质量",
             "conservative_investment": "保守投资", "residual_momentum": "残差动量",
             "pead": "财报超预期", "reversal": "短期反转", "trend": "趋势",
             "liquidity": "流动性"}
    lines = [f"样本域：{config['universe']['id']}",
             f"调仓：{config['portfolio']['rebalance']} · 持有 {config['portfolio']['stock_count']} 只"]
    for f in config["factors"]:
        names = f.get("metrics") or GROUP_FACTORS[f["name"]]
        shown = "、".join((factor_spec(n).label if factor_spec(n) else n) for n in names)
        lines.append(f"{label.get(f['name'], f['name'])} {f['weight']:.0%}：{shown}")
    return lines


def parse_nl_config(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise SpecError("请先输入策略描述")
    cfg = get_settings()
    if not cfg.openai_api_key:
        raise StrategyParseError(
            "未配置 LLM（OPENAI_API_KEY），可手动填写因子配置 JSON 后回测")
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
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StrategyParseError("LLM 返回的内容不是合法 JSON，请换一种说法") from e
    try:
        return validate_config(data)
    except SpecError as e:
        raise StrategyParseError(f"LLM 返回的配置不可用：{e}") from e


def run_config(repo: Repository, config: dict, force_refresh: bool = False,
               save: bool = False, nl_text: str = "") -> dict:
    """按因子配置运行确定性回测；save=True 时登记到自然语言策略库（research 状态）。"""
    cfg = validate_config(config)
    weights = config_to_weights(cfg)
    params = {
        "factor_weights": weights,
        "top_k": cfg["portfolio"]["stock_count"],
        "rebalance": cfg["portfolio"]["rebalance"],
        "weighting": cfg["portfolio"]["weighting"],
        "min_factors": min(3, len(weights)),
    }
    res = run_strategy(
        repo, "nl_factor", universe=cfg["universe"]["id"],
        start=cfg["backtest"].get("start_date"),
        oos_start=cfg["backtest"].get("oos_start"), params=params,
        persist=True, force_refresh=force_refresh,
    )
    out = {"config": cfg, "weights": weights, "describe": describe_config(cfg), "result": res}
    if save:
        from .nl_rules import _new_id, _now

        sid = _new_id(repo)
        now = _now()
        summary = {
            "run_id": res["run_id"], "start": res["start"], "end": res["end"],
            "annual_return": res["metrics"].get("annual_return"),
            "sharpe": res["metrics"].get("sharpe"),
            "max_drawdown": res["metrics"].get("max_drawdown"),
            "oos_annual": (res.get("oos") or {}).get("annual_return"),
            "score": (res.get("scorecard") or {}).get("score"),
            "factor_weights": weights,
        }
        repo.upsert("dim_nl_strategy", [{
            "strategy_id": sid, "name": cfg["strategy_name"][:40],
            "nl_text": nl_text or "", "universe_id": cfg["universe"]["id"],
            "spec_json": json.dumps({"mode": "factor", "config": cfg}, ensure_ascii=False),
            "last_result": json.dumps(summary, ensure_ascii=False, default=str),
            "last_run_at": now, "created_at": now, "updated_at": now,
        }], ["strategy_id"])
        out["saved"] = {"strategy_id": sid, "name": cfg["strategy_name"]}
    logger.info(f"自然语言因子策略：{cfg['strategy_name']} · {len(weights)} 个因子")
    return out
