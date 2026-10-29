"""股票池管理 API：列表/详情/解析/新建/改名/增删成员/启动任务/调度开关。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..pipeline.pool_jobs import get_pool_job, start_pool_job
from ..research.pool_admin import (
    PoolNotFound,
    PoolReadonly,
    PoolValidationError,
    add_members,
    create_pool,
    get_pool,
    list_pools,
    remove_member,
    rename_pool,
    resolve_stock_names,
    set_daily_enabled,
)
from ..storage.repository import open_repository

router = APIRouter(prefix="/api/pools", tags=["pools"])


class MemberIn(BaseModel):
    symbol: str
    name: str | None = None


class ResolveRequest(BaseModel):
    codes: list[str]


class PoolCreate(BaseModel):
    name: str
    description: str = ""
    members: list[MemberIn]


class PoolRename(BaseModel):
    name: str


class MemberAdd(BaseModel):
    members: list[MemberIn]


class ScheduleToggle(BaseModel):
    enabled: bool


def _call(fn):
    """统一把业务异常映射为 HTTP 状态码。"""
    try:
        return fn()
    except PoolValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except PoolReadonly as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except PoolNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


def _with_repo(fn):
    repo = open_repository()
    try:
        return _call(lambda: fn(repo))
    finally:
        repo.close()


@router.get("")
def pools() -> dict:
    """池列表 + 数据/回测/调度/今日任务状态（含全局最新交易日）。"""
    def _run(repo):
        latest = repo.query("SELECT MAX(trade_date) AS d FROM fact_quote")[0]["d"]
        return {"pools": list_pools(repo), "latest": latest}

    return _with_repo(_run)


@router.get("/{pool_id}")
def pool_detail(pool_id: str) -> dict:
    return _with_repo(lambda repo: get_pool(repo, pool_id))


@router.post("/resolve")
def resolve(payload: ResolveRequest) -> dict:
    """代码 → 名称解析（不落库）。非法 token 以 valid=False 返回。"""
    return _with_repo(
        lambda repo: {"items": resolve_stock_names(repo, payload.codes)})


@router.post("")
def create(payload: PoolCreate) -> dict:
    members = [m.model_dump() for m in payload.members]
    return _with_repo(
        lambda repo: create_pool(repo, payload.name, members, payload.description))


@router.patch("/{pool_id}")
def rename(pool_id: str, payload: PoolRename) -> dict:
    return _with_repo(lambda repo: rename_pool(repo, pool_id, payload.name))


@router.post("/{pool_id}/members")
def add(pool_id: str, payload: MemberAdd) -> dict:
    members = [m.model_dump() for m in payload.members]
    return _with_repo(lambda repo: add_members(repo, pool_id, members))


@router.delete("/{pool_id}/members/{symbol}")
def remove(pool_id: str, symbol: str) -> dict:
    return _with_repo(lambda repo: remove_member(repo, pool_id, symbol))


@router.post("/{pool_id}/activate")
def activate(pool_id: str) -> dict:
    """启动/更新：数据 → 因子 → 回测 → 组合 → 跟踪 → 开启调度（后台执行）。"""
    _with_repo(lambda repo: get_pool(repo, pool_id))  # 池不存在直接 404
    res = start_pool_job(pool_id)
    if res.get("status") == "busy":
        raise HTTPException(status_code=409, detail=res.get("message") or "已有池任务运行中")
    return res


@router.get("/{pool_id}/job")
def job(pool_id: str) -> dict:
    """今日任务状态与步骤明细。"""
    return _with_repo(lambda repo: get_pool_job(repo, pool_id))


@router.post("/{pool_id}/schedule")
def schedule(pool_id: str, payload: ScheduleToggle) -> dict:
    return _with_repo(
        lambda repo: set_daily_enabled(repo, pool_id, payload.enabled))
