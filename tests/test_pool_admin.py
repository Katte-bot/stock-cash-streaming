"""自定义股票池业务逻辑测试（临时 SQLite，不联网）。"""

import pytest

from stock_cash_streaming.research import pool_admin as pa
from stock_cash_streaming.research.pool_admin import (
    PoolReadonly,
    PoolValidationError,
    add_members,
    create_pool,
    get_pool,
    list_pools,
    normalize_codes,
    remove_member,
    rename_pool,
    resolve_stock_names,
    set_daily_enabled,
)
from stock_cash_streaming.storage.repository import open_repository


@pytest.fixture()
def repo(tmp_path):
    r = open_repository(str(tmp_path / "test.sqlite3"))
    try:
        yield r
    finally:
        r.close()


def _m(symbol: str, name: str) -> dict:
    return {"symbol": symbol, "name": name}


def test_normalize_codes():
    raw = "600406, 000001\n159611  600406；abc 1234567"
    assert normalize_codes(raw) == ["600406", "000001", "159611"]
    assert normalize_codes(["1", "0001"]) == ["000001"]


def test_create_pool_and_status(repo):
    pool = create_pool(repo, "我的测试池", [_m("600406", "国电南瑞"), _m("1", "平安银行")])
    assert pool["id"] == "U1"
    assert pool["is_custom"] is True
    assert pool["readonly"] is False
    assert pool["members_count"] == 2
    assert {m["symbol"] for m in pool["members"]} == {"600406", "000001"}

    listed = {p["id"]: p for p in list_pools(repo)}
    assert "C0" in listed  # 迁移时已落 C0 行
    assert listed["U1"]["data_state"] == "missing"
    assert listed["U1"]["backtest_state"] == "missing"
    # 新建池默认纳入每日池任务
    assert listed["U1"]["schedule_enabled"] is True


def test_rename_add_remove(repo):
    create_pool(repo, "池A", [_m("600406", "国电南瑞")])
    before = get_pool(repo, "U1")["members_updated_at"]

    assert rename_pool(repo, "U1", "池B")["name"] == "池B"

    res = add_members(repo, "U1", [_m("600519", "贵州茅台"), _m("600406", "国电南瑞")])
    assert [x["symbol"] for x in res["added"]] == ["600519"]
    assert res["skipped"] == ["600406"]
    assert get_pool(repo, "U1")["members_updated_at"] >= before

    remove_member(repo, "U1", "600519")
    assert get_pool(repo, "U1")["members_count"] == 1


def test_builtin_readonly(repo):
    with pytest.raises(PoolReadonly):
        rename_pool(repo, "C0", "新名字")
    with pytest.raises(PoolReadonly):
        add_members(repo, "C0", [_m("600406", "国电南瑞")])
    with pytest.raises(PoolReadonly):
        remove_member(repo, "C0", "600406")
    with pytest.raises(PoolValidationError):
        set_daily_enabled(repo, "C0", False)


def test_name_unique_and_validation(repo):
    create_pool(repo, "重名池", [_m("600406", "国电南瑞")])
    with pytest.raises(PoolValidationError):
        create_pool(repo, "重名池", [_m("600519", "贵州茅台")])
    with pytest.raises(PoolValidationError):
        create_pool(repo, "   ", [_m("600519", "贵州茅台")])
    with pytest.raises(PoolValidationError):
        create_pool(repo, "空成员", [])
    with pytest.raises(PoolValidationError):
        create_pool(repo, "ST池", [_m("600001", "*ST测试")])


def test_resolve_stock_names(repo, monkeypatch):
    repo.upsert("dim_stock", [{"symbol": "600406", "name": "国电南瑞", "market": "sh"}], ["symbol"])
    monkeypatch.setattr(pa, "_spot_names", lambda: {"000002": "万科A"})

    items = {x["symbol"]: x for x in resolve_stock_names(
        repo, ["600406", "2", "999999", "abc"])}
    assert items["600406"]["name"] == "国电南瑞"
    assert items["600406"]["resolved"] is True
    assert items["000002"]["name"] == "万科A"
    assert items["999999"]["resolved"] is False
    assert items["abc"]["valid"] is False
