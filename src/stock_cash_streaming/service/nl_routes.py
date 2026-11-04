"""自然语言策略 API：解析、回测、策略保存/列表/删除/重跑。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..research.nl_rules import (
    SpecError,
    StrategyParseError,
    delete_strategy,
    get_strategy,
    list_strategies,
    parse_nl_strategy,
    run_nl_backtest,
    save_strategy,
    update_strategy_result,
)
from ..storage.repository import open_repository

router = APIRouter(prefix="/api/nl", tags=["nl"])


class ParseIn(BaseModel):
    text: str


class BacktestIn(BaseModel):
    spec: dict
    universe: str = "C0"
    top_n: int = 5
    start: str | None = None
    end: str | None = None
    save: bool = False
    name: str | None = None
    nl_text: str | None = None


class ConfigIn(BaseModel):
    text: str | None = None
    config: dict | None = None
    run: bool = True
    force_refresh: bool = False


def _with_repo(fn):
    repo = open_repository()
    try:
        return fn(repo)
    finally:
        repo.close()


def _guard(fn):
    """统一错误映射：DSL 非法 → 400；LLM 解析失败 → 503。"""
    try:
        return fn()
    except SpecError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except StrategyParseError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/parse")
def parse(payload: ParseIn) -> dict:
    """自然语言 → DSL（不落库）。"""
    def _run():
        spec = parse_nl_strategy(payload.text)
        from ..research.nl_rules import describe_spec
        return {"spec": spec, "describe": describe_spec(spec)}

    return _guard(_run)


@router.post("/backtest")
def backtest(payload: BacktestIn) -> dict:
    """按 DSL 与股票池运行回测；save=true 时保存策略与最近结果。"""
    if not (1 <= payload.top_n <= 20):
        raise HTTPException(status_code=400, detail="最多持仓数需在 1~20 之间")

    def _run(repo):
        res = run_nl_backtest(repo, payload.spec, payload.universe,
                              top_n=payload.top_n, start=payload.start,
                              end=payload.end)
        if payload.save:
            res["saved"] = save_strategy(repo, res["spec"], payload.nl_text or "",
                                         payload.universe, res, payload.name)
        return res

    return _guard(lambda: _with_repo(_run))


@router.get("/strategies")
def strategies() -> dict:
    return {"items": _with_repo(list_strategies)}


@router.post("/config")
def nl_config(payload: ConfigIn) -> dict:
    """自然语言 → 因子配置 → 确定性回测。"""
    from ..research.nl_config import (
        config_to_weights,
        describe_config,
        parse_nl_config,
        run_config,
        validate_config,
    )

    def _run(repo):
        if payload.config:
            cfg = validate_config(payload.config)
        elif payload.text:
            cfg = parse_nl_config(payload.text)
        else:
            raise HTTPException(status_code=400, detail="请提供 text 或 config")
        weights = config_to_weights(cfg)
        out = {"config": cfg, "weights": weights, "describe": describe_config(cfg)}
        if payload.run:
            out.update(run_config(repo, cfg, force_refresh=payload.force_refresh))
        return out

    return _guard(lambda: _with_repo(_run))


@router.get("/strategies/{strategy_id}")
def strategy_detail(strategy_id: str) -> dict:
    item = _with_repo(lambda repo: get_strategy(repo, strategy_id))
    if not item:
        raise HTTPException(status_code=404, detail="策略不存在")
    return item


@router.delete("/strategies/{strategy_id}")
def strategy_delete(strategy_id: str) -> dict:
    ok = _with_repo(lambda repo: delete_strategy(repo, strategy_id))
    if not ok:
        raise HTTPException(status_code=404, detail="策略不存在")
    return {"removed": strategy_id}


@router.post("/strategies/{strategy_id}/run")
def strategy_run(strategy_id: str, universe: str = "C0", top_n: int = 5) -> dict:
    """用已保存策略重跑并更新最近结果。"""
    def _run(repo):
        item = get_strategy(repo, strategy_id)
        if not item:
            raise HTTPException(status_code=404, detail="策略不存在")
        res = run_nl_backtest(repo, item["spec"], universe, top_n=top_n)
        res["saved"] = update_strategy_result(repo, strategy_id, universe, res)
        return res

    return _guard(lambda: _with_repo(_run))
