# 自定义股票池管理功能实施计划

## 1. 摘要

目标：在现有 C0~C3 体系上增加“用户自定义股票池”的完整闭环：

1. 新建股票池：输入代码 → 解析股票名称 → 用户确认 → 入库。
2. 编辑自定义池名称。
3. 向自定义池添加股票、删除股票。
4. 每个池提供“启动/更新”按钮：按需补行情/资金流/财务 → 重算因子 → 回测 → 目标组合/跟踪 → 开启每日调度；已是新数据/已回测/已开启调度时自动跳过并说明原因。
5. 顶部股票池 tab 动态展示自定义池，现有 C0~C3 行为保持不变。

已确认的产品决策：

- **内置 C0/C1/C2/C3 只读**：名称、成员不可在页面修改；仍可通过原有 CLI/刷新流程维护。
- **自定义池完全可编辑**：名称、成员均可增删改。
- **每日调度范围**：每日增量数据 + 因子 + 目标组合/跟踪；回测只在“数据日期前进”或“成员发生变化”时重跑。
- 内置池也可以点击“启动/更新”，但 C0 的组合/跟踪由全局每日流水线负责，不重复跑；C1~C3 可建立自己的每日池任务。

---

## 2. 现状分析

### 2.1 数据层

- `dim_universe`：`universe_id, name, description, updated_at`，见 [schema.sql](file:///data02/stock-cash-streaming/src/stock_cash_streaming/storage/schema.sql#L20-L25)。
- `dim_universe_member`：`universe_id, symbol, sector_code, source, updated_at`，主键 `(universe_id, symbol)`，见 [schema.sql](file:///data02/stock-cash-streaming/src/stock_cash_streaming/storage/schema.sql#L28-L35)。
- C0 不存 `dim_universe_member`，成员由 `dim_sector_member` 动态派生；C1 来自 `config/universe_c1.csv`；C2/C3 来自 `config/universes.yaml`。
- `list_universes()` 在没有 C0 行时会插入一个虚拟 C0，见 [universe.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/research/universe.py#L232-L240)。
- `refresh_universe()` 是“整体替换”，见 [universe.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/research/universe.py#L80-L98)；目前不会记录成员变更时间。

### 2.2 服务层

- 现有 `/api/universes` 只读取 `docs/experiments_universes.csv`，用于底部“股票池年度表现”，不是池管理接口，见 [app.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/service/app.py#L197-L230)。
- 没有任何池/成员写接口。
- `/api/overview`、`/api/factors/latest`、`/api/portfolio`、`/api/tracking` 已支持任意 `universe` 参数；回测按 `defensive_top12_monthly_{universe}` 读取，见 [app.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/service/app.py#L34-L134) 与 [app.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/service/app.py#L233-L260)。
- `/api/stock/{symbol}` 不返回名称；名称应从 `dim_stock.name` 获取。

### 2.3 数据/策略层

- `sync_symbols()` 负责补行情与新浪资金流，见 [universe_sync.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/ingest/universe_sync.py#L16-L63)。
- `sync_fundamentals()` 负责财务/分红/上市日，见 [fundamentals.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/ingest/fundamentals.py#L214-L282)。
- `compute_all()` 当前全库计算 v2 因子，不支持按代码集合裁剪，见 [engine.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/features/engine.py#L132-L156)。
- `compute_fundamental_factors(repo, symbols=...)` 已支持按代码集合计算 v3。
- `run_backtest()` 支持 `universe`、`eligibility`、`universe_tag`，落库策略名为 `defensive_top12_monthly_{tag}`，见 [engine.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/backtest/engine.py#L411-L442) 与 [engine.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/backtest/engine.py#L683-L707)。
- `build_target_portfolio()` 已支持 `universe_id`，非 C0 自动追加 `_{universe_id}` 后缀，见 [portfolio.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/strategy/portfolio.py#L120-L141)。
- `simulate_tracking()` 的**基准**固定为 `dim_sector_member` 全部成分，见 [tracking.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/strategy/tracking.py#L115-L123)。自定义池需要改为按池成员计算基准。
- 每日流水线只对 C0 做信号/组合/跟踪；增量行情已覆盖所有 `dim_universe_member`，见 [daily.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/pipeline/daily.py#L31-L133) 与 [incremental.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/ingest/incremental.py#L48-L57)。
- 调度器每个工作日 16:10 触发一次，判断 `task_log=daily_YYYYMMDD`，见 [scheduler.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/pipeline/scheduler.py#L22-L55)。

### 2.4 前端

- 单页 `index.html`，原生 JS；顶部 tab 由 `POOLS = ['C0','C1','C2','C3']` 硬编码，见 [index.html](file:///data02/stock-cash-streaming/src/stock_cash_streaming/service/templates/index.html#L331-L446)。
- 已有 `.card`、`.tabs/.tab`、`.table-wrap`、`.modal`、`.btn` 等样式可复用。
- 当前没有任何 `input/textarea/select` 控件，需要补充表单样式。

---

## 3. 方案设计

### 3.1 数据库变更

修改 [schema.sql](file:///data02/stock-cash-streaming/src/stock_cash_streaming/storage/schema.sql#L20-L25)：

```sql
CREATE TABLE IF NOT EXISTS dim_universe (
    universe_id        TEXT PRIMARY KEY,
    name               TEXT,
    description        TEXT,
    updated_at         TEXT,
    is_custom          INTEGER NOT NULL DEFAULT 0,
    daily_enabled      INTEGER NOT NULL DEFAULT 0,
    members_updated_at TEXT
);
```

修改 [db.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/storage/db.py#L29-L34) 的 `_migrate()`：

- 对旧库逐列 `ALTER TABLE dim_universe ADD COLUMN ...`：
  - `is_custom INTEGER NOT NULL DEFAULT 0`
  - `daily_enabled INTEGER NOT NULL DEFAULT 0`
  - `members_updated_at TEXT`
- 确保 C0 行存在：
  ```sql
  INSERT INTO dim_universe
    (universe_id, name, description, updated_at, is_custom, daily_enabled)
  VALUES ('C0', 'C0 当前板块成分', '电网设备 + 天然气', datetime('now','localtime'), 0, 1)
  ON CONFLICT(universe_id) DO NOTHING;
  ```
- C0 的 `daily_enabled=1` 表示“由全局每日流水线维护”，但池任务循环会跳过 C0，避免重复。

修改 [universe.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/research/universe.py#L80-L98)：

- `refresh_universe()` 的 `dim_universe` upsert 增加 `members_updated_at=now`，用于判断回测是否因成员变化而失效。
- `prune_universe()` 删除成员后同样更新 `members_updated_at`。
- `list_universes()` 继续兼容虚拟 C0，但迁移后 C0 行已存在，不会重复插入。

### 3.2 股票池管理服务

新增 `src/stock_cash_streaming/research/pool_admin.py`，只做业务逻辑，不依赖 FastAPI：

- `normalize_codes(raw: str | list[str]) -> list[str]`
  - 支持换行/逗号/空格分隔；`zfill(6)`；去重；只保留 6 位数字；返回顺序稳定。
- `resolve_stock_names(repo, codes) -> list[dict]`
  - 先查 `dim_stock.name`；
  - 缺失的代码调用一次 `get_source().spot()`（腾讯全市场快照），用 `symbol/name` 映射补齐；
  - 返回 `{symbol, name, market, is_etf, resolved}`；
  - 不写库；`resolved=False` 时前端要求用户手工填写名称。
- `list_pools(repo) -> list[dict]`
  - 遍历 `dim_universe`（C0/C1/C2/C3 + 自定义）；
  - 每个池返回 `id, name, description, is_custom, daily_enabled, members_count`；
  - 计算数据状态：
    - `global_latest = MAX(fact_quote.trade_date)`；
    - 每个成员的 `MAX(fact_quote.trade_date)`；
    - `data_state = missing | stale | latest`、`stale_count`、`missing_count`、`data_as_of`。
  - 计算回测状态：
    - 最新 `backtest_run`，策略名 `defensive_top12_monthly_{pool_id}`；
    - `backtest_state = missing | stale | latest`；
    - 条件：`end_date >= global_latest` 且 `created_at >= members_updated_at`（`members_updated_at` 为空时视为需要重跑）。
  - 计算今日池任务状态：读 `task_log` 的 `task_id=pool_{id}_YYYYMMDD`。
  - C0 的 `schedule_enabled=True` 且 `schedule_note="全局每日流水线"`；其余看 `daily_enabled`。
- `get_pool(repo, pool_id) -> dict`
  - 池元数据 + 成员列表（`symbol, name, sector_code, source, updated_at`）；
  - C0 成员通过 `universe_members()` 派生；
  - 内置池标记 `readonly=True`。
- `create_pool(repo, name, members, description="") -> dict`
  - 名称 `strip` 后非空、最长 40 字符、全局唯一；
  - 成员非空、去重、每只必须有名称（解析结果或用户手工输入）；
  - 生成自定义 ID：`U1`, `U2`…（取 `dim_universe` 中 `^U\d+$` 的最大编号 +1，冲突则递增）；
  - `dim_universe`：`is_custom=1, daily_enabled=0, members_updated_at=now`；
  - `dim_stock`：upsert `symbol,name,market,updated_at`，保留已有 `industry`；
  - `dim_universe_member`：`source='manual'`，`sector_code` 取 `dim_stock.industry`，没有则 `"自定义"`。
- `rename_pool(repo, pool_id, name)`
  - 仅 `is_custom=1`；名称唯一性校验；更新 `name, updated_at`。
- `add_members(repo, pool_id, members) -> dict`
  - 仅 `is_custom=1`；解析/校验名称；已存在成员跳过；upsert `dim_stock` 与 `dim_universe_member`；更新 `members_updated_at`；返回 `{added, skipped}`。
- `remove_member(repo, pool_id, symbol)`
  - 仅 `is_custom=1`；删除成员；更新 `members_updated_at`。
- `set_daily_enabled(repo, pool_id, enabled)`
  - 内置池也允许开关（C0 除外，C0 固定由全局流水线维护）；写入 `daily_enabled`。
- 自定义异常：
  - `PoolNotFound`、`PoolReadonly`、`PoolValidationError`，供 API 映射为 404/403/400。

> 内置池只读：`rename_pool/add_members/remove_member` 对 `is_custom=0` 抛 `PoolReadonly`。C0 的成员永远由板块成分派生。

### 3.3 因子引擎支持按股票池计算

修改 [engine.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/features/engine.py)：

- `_load_quotes(repo, symbols: list[str] | None = None)`：当 `symbols` 非空时 SQL 增加 `WHERE symbol IN (...)`。
- `_load_flows(repo, symbols: list[str] | None = None)`：同上。
- `compute_all(repo, include_flow=True, keep_factors=None, symbols=None)`：把 `symbols` 传给两个 loader。
- 默认 `symbols=None` 保持现有全库行为；`pipeline/daily.py` 不需要改调用。
- 这样池任务只重算该池成员的 v2 因子，避免每次全库 43 万行重算。

### 3.4 跟踪基准按股票池

修改 [tracking.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/strategy/tracking.py#L69-L123)：

- `simulate_tracking(..., universe_id: str | None = None)`：
  - `universe_id` 为空时保持现有行为（`dim_sector_member` 全部成分）；
  - 非空时 `bench_syms = universe_symbols(repo, universe_id)`，基准改为该池等权买入持有。
- 修改 [app.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/service/app.py#L247-L260)：`/api/tracking` 非 C0 时传 `universe_id=universe`；C0 不传，保持历史基准不变。
- 池任务对非 C0 传 `universe_id=pool_id`；C0 不传。
- 可选：给 `cli tracking` 增加 `--universe` 参数，保持 CLI/API 一致（低风险）。

### 3.5 池激活与每日任务

新增 `src/stock_cash_streaming/pipeline/pool_jobs.py`：

#### 3.5.1 状态与并发

- 模块级 `_JOB_LOCK = threading.Lock()`、`_RUNNING_POOL: str | None`。
- **跨进程锁**：`fcntl.flock` 锁文件 `data/pool_job.lock`，避免 uvicorn 中的手动任务与 scheduler 进程的每日池任务同时写 SQLite。`run_pool_job` 默认尝试非阻塞获取；获取失败返回 `busy`，API 返回 409。
- `run_daily_pipeline` 在入口获取同一把跨进程锁；若获取失败，返回 `skipped`，reason=`池任务运行中，稍后重试`。这样手动激活与每日流水线不会并发写库。
- 在 `storage/db.py` 的连接初始化中增加 `PRAGMA busy_timeout=5000`，进一步降低偶发写锁冲突。
- 任务状态写入 `task_log`：
  - `task_id = f"pool_{pool_id}_{YYYYMMDD}"`
  - `name = f"pool_job:{pool_id}"`
  - `status = running | ok | skipped | failed | busy`
  - `message = JSON({actions:[{step,status,message}], error})`
- `get_pool_job(repo, pool_id)` 读今日行；没有则返回 `idle`；若状态为 `running` 且 `started_at` 超过 2 小时，视为 `failed`（服务重启/线程中断），message=`任务中断，可重新启动`。
- `start_pool_job(pool_id)` 用于 API：抢跨进程锁 → 启动 `threading.Thread` → 返回 `running`；线程内自行 `open_repository()` 并在 finally 释放锁。
- `run_pool_job(repo, pool_id, force=False, trigger="manual", lock_held=False)` 用于线程和每日调度器，执行实际步骤；`lock_held=True` 时不重复抢锁。

#### 3.5.2 激活步骤（幂等）

按顺序执行，每一步把结果追加到 `actions` 并更新 `task_log`：

1. **加载池**：`get_pool`；空池直接失败。
2. **行情/资金流**：
   - `global_latest = MAX(fact_quote.trade_date)`；
   - `missing`：池内无任何行情的成员 → `sync_symbols(start_date="20180101", with_flow=True)`；
   - `stale`：最新行情 < `global_latest` 的成员 → `sync_symbols(start_date=global_latest 前 10 天, with_flow=False)`；
   - 若某个 stale 成员在 `fact_stock_flow` 中没有记录，再单独 `sync_symbols([sym], with_flow=True)`；
   - 同步完成后重新计算 `latest_data_date = MAX(fact_quote.trade_date)`，后续回测/组合/跟踪都以它为准；
   - 全部最新 → `skipped`，message=`已是 {latest_data_date}`。
3. **财务/上市日**：
   - 找出非 ETF 且 `fact_financial` 无记录或 `dim_stock.ipo_date` 为空的成员；
   - `sync_fundamentals(repo, need, start_year=2017)`；
   - 无需要 → `skipped`。
4. **因子**：
   - `compute_all(repo, symbols=members)`（v2）；
   - `compute_fundamental_factors(repo, symbols=非ETF成员)`（v3）；
   - 若池内 v2 因子在 `latest_data_date` 已齐全且数据未更新，可跳过（通过 `fact_factor` 计数判断）。
5. **回测**：
   - `backtest_state=latest` 且非 `force` → `skipped`；
   - 否则：
     ```python
     run_backtest(
       repo, top_k=12, rebalance="monthly", hold_buffer=1.0,
       strategy="defensive", weighting="vol_inv", vol_target=0.25,
       universe=members, eligibility=eligibility_panel(repo, members),
       cost_model="a_share", limit_model="board",
       universe_tag=pool_id, persist=True,
     )
     ```
   - 返回 `run_id`、`annual`、`sharpe`。
6. **目标组合**（C0 跳过，由全局流水线负责）：
   - 若 `fact_target_run` 已有 `(trade_date=latest_data_date, strategy=defensive_top12_monthly_{pool})` → `skipped`；
   - 否则 `build_target_portfolio(repo, persist=True, universe_id=pool_id, strategy="defensive")`。
7. **模拟跟踪**（C0 跳过）：
   - 若 `fact_tracking_nav` 的 `MAX(trade_date)=latest_data_date` → `skipped`；
   - 否则 `simulate_tracking(repo, strategy=portfolio_strategy_name(pool_id), persist=True, universe_id=pool_id)`。
8. **开启调度**：
   - 以上数据/因子/回测成功 → `set_daily_enabled(pool_id, True)`；
   - 已是开启状态 → `skipped`，message=`已开启`；
   - C0 标记 `schedule_note="全局每日流水线"`。

最终：
- 全部跳过 → `skipped`；
- 有失败 → `failed`（数据/因子/回测失败 fail-fast；组合/跟踪失败记录错误但继续后续步骤）；
- 否则 `ok`。

#### 3.5.3 每日调度接入

修改 [daily.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/pipeline/daily.py#L61-L114) 在 C0 的组合/跟踪/风险监控之后：

```python
from .pool_jobs import run_enabled_pool_jobs
result["steps"]["pool_jobs"] = run_enabled_pool_jobs(repo)
```

`run_enabled_pool_jobs(repo)`：

- 查询 `dim_universe WHERE daily_enabled=1 AND universe_id<>'C0'`；
- 逐个调用 `run_pool_job(repo, pool_id, force=False, trigger="scheduler")`；
- 单个池失败只记录到 `task_log` 和返回摘要，不影响其他池和主流水线；
- 主 `task_log` 的 message 中保留每个池的状态概要。

这样：
- 日常增量已经覆盖所有 `dim_universe_member` 的行情/资金流；
- 池任务只补该池缺失数据、因子、组合/跟踪，并在数据日期前进或成员变化时重跑回测；
- 用户点击“启动”走同一套 `run_pool_job`，行为一致。

### 3.6 API 路由

新增 `src/stock_cash_streaming/service/pool_routes.py`，使用 `APIRouter(prefix="/api/pools")`；在 [app.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/service/app.py#L16-L18) `include_router`。

接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/pools` | 池列表 + 数据/回测/调度状态 + 今日任务状态 |
| GET | `/api/pools/{pool_id}` | 池详情 + 成员（含名称/来源/只读标记） |
| POST | `/api/pools/resolve` | 代码 → 名称解析（不落库） |
| POST | `/api/pools` | 新建自定义池 |
| PATCH | `/api/pools/{pool_id}` | 修改自定义池名称 |
| POST | `/api/pools/{pool_id}/members` | 批量添加成员 |
| DELETE | `/api/pools/{pool_id}/members/{symbol}` | 删除成员 |
| POST | `/api/pools/{pool_id}/activate` | 启动/更新（后台任务） |
| GET | `/api/pools/{pool_id}/job` | 今日任务状态/步骤 |
| POST | `/api/pools/{pool_id}/schedule` | 开关每日调度（C0 不可关） |

请求体使用 Pydantic 模型：

```python
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
```

错误映射：

- `PoolValidationError` → 400
- `PoolReadonly` → 403
- `PoolNotFound` → 404
- 任务运行中 → 409
- 未捕获异常 → 500，并写入日志

### 3.7 前端改造

修改 [index.html](file:///data02/stock-cash-streaming/src/stock_cash_streaming/service/templates/index.html)：

#### 3.7.1 动态股票池

- 删除硬编码 `const POOLS = [...]`。
- 新增 `const poolsState = { list: [], active: 'C0', jobTimer: null }`。
- 新增 `loadPools()`：`GET /api/pools`；失败时回退 `[{id:'C0', name:'C0', members:0}]`。
- `renderPoolTabs()` 改为使用 `poolsState.list`：
  - 内置池显示 `C0 · 66 只`；
  - 自定义池显示 `用户名称 · N 只`（`title` 显示 ID）。
- `setPool(id)`：
  - 更新 `state.universe`；
  - 如果该 ID 存在于 `univState.data.universes`，同步底部年度卡的 active；否则底部保持原样；
  - 调 `load()`。
- 初始化改为：
  ```js
  (async () => {
    await loadPools();
    await load();
    loadUniverses();
    connectWS();
  })();
  ```
- `loadUniverses()` 不再调用 `renderPoolTabs()`，只负责底部年度卡。

#### 3.7.2 股票池管理卡片

在“策略回测”卡片之后新增：

```html
<div class="card" id="poolAdmin">
  <h2>股票池管理 <span class="tag">内置池只读 · 自定义池可编辑</span></h2>
  <p class="desc">新建池、维护成员、启动数据同步与回测、开启每日调度</p>
  <div class="pfbar">
    <button class="btn primary" id="newPoolBtn">+ 新建股票池</button>
    <span class="reasons" id="poolAdminMsg"></span>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>池</th><th>成员</th><th>数据</th><th>回测</th><th>调度</th><th>操作</th></tr></thead>
      <tbody id="poolAdminBody"></tbody>
    </table>
  </div>
  <div class="report" id="poolJobBox" style="display:none;margin-top:12px"></div>
</div>
```

行内操作：

- 内置池：`启动/更新`、`查看成员`；名称/成员编辑按钮显示为禁用或“内置只读”。
- 自定义池：`启动/更新`、`改名`、`添加股票`、`管理成员`、`开关调度`。
- 状态 badge：
  - 数据：`最新 2026-10-26` / `落后 N 只` / `无数据`
  - 回测：`已回测至 ...` / `需更新` / `未回测`
  - 调度：`已开启` / `未开启` / `全局每日`（C0）

#### 3.7.3 新建/添加成员弹窗

新增 `#poolModal`：

- 模式：`create` / `add` / `rename`。
- 字段：
  - 池名称（create/rename）；
  - 代码输入 `textarea`（create/add），支持逗号/换行/空格；
  - `解析名称` 按钮；
  - 解析结果表：代码、名称、市场、ETF 标记、状态；
  - 未识别行提供名称输入框；
  - `确认创建` / `确认添加` / `保存名称`。
- 前端函数：
  - `openPoolModal(mode, poolId?)`
  - `resolvePoolCodes()`
  - `submitPoolModal()`
- 校验：
  - 池名非空；
  - 每个代码有名称；
  - 至少一个成员；
  - 重复代码在前端去重。

#### 3.7.4 成员管理弹窗

新增 `#memberModal`：

- 标题：`池名 · 成员管理`
- 表格：代码、名称、板块、来源、更新时间、操作（删除）。
- 内置池：不显示删除按钮，顶部提示“内置池成员只读”。
- 删除时 `confirm()`，调用 `DELETE /api/pools/{id}/members/{symbol}`，成功后刷新弹窗和池列表。

#### 3.7.5 启动/更新任务

- `activatePool(poolId)`：
  - `POST /api/pools/{id}/activate`；
  - 若 409：显示“已有池任务运行中”；
  - 显示 `#poolJobBox`，每 2 秒轮询 `GET /api/pools/{id}/job`；
  - 渲染步骤：
    ```
    ✓ 数据：已是 2026-10-26（跳过）
    ✓ 回测：已是最新（跳过）
    ✓ 组合：已更新
    ✓ 跟踪：5 个交易日
    ✓ 调度：已开启
    ```
  - `ok/skipped/failed` 时停止轮询，刷新 `loadPools()`，若当前池是该池则 `load()`。
- 任务运行中，按钮禁用并显示“运行中…”。

#### 3.7.6 样式

- 新增 `.form-row`、`.field`、`.input`、`.textarea`、`.pool-status`、`.badge-ok/.badge-warn/.badge-off`、`.icon-btn`。
- 复用现有 `.btn`、`.card`、`.table-wrap`、`.modal`。
- 移动端：管理表格横向滚动；弹窗宽度 `min(720px, 100%)`；输入框全宽。

### 3.8 测试

新增 `tests/test_pool_admin.py`（使用临时 SQLite，不联网）：

- `normalize_codes`：去重、补零、非法字符过滤。
- 创建自定义池：ID 为 `U1`，成员/名称入库。
- 修改名称、添加成员、删除成员；`members_updated_at` 更新。
- 内置池 C1 改名/增删成员 → `PoolReadonly`。
- 名称唯一性校验。
- `resolve_stock_names`：`dim_stock` 命中；mock `spot()` 补齐缺失；未识别返回 `resolved=False`。

新增 `tests/test_pool_jobs.py`（mock 重活）：

- 数据已最新、回测已最新、调度已开启 → `run_pool_job` 返回 `skipped`，且不调用 `sync_symbols/run_backtest/build_target_portfolio`。
- 数据落后 → 调用 `sync_symbols`；回测 end_date 落后 → 调用 `run_backtest`。
- 成员变更时间晚于回测创建时间 → 回测被判定为 stale。
- 非 C0 的 tracking 调用带 `universe_id`。
- 单池失败不影响其他池的 `run_enabled_pool_jobs`（mock 抛错）。

### 3.9 文档

- 在 [PROJECT_MEMORY.md](file:///data02/stock-cash-streaming/docs/PROJECT_MEMORY.md) 增加一节“自定义股票池与池级每日任务”，记录：
  - 内置只读、自定义 `U*` 池；
  - `dim_universe` 新字段；
  - 池任务幂等规则；
  - C0 由全局流水线维护；
  - 跟踪基准按池成员。
- 在 [operations-runbook.md](file:///data02/stock-cash-streaming/docs/operations-runbook.md) 补一条“池任务失败如何重试”（页面按钮或 `task_log` 查看）。
- 可选：在 [dev-deployment.md](file:///data02/stock-cash-streaming/docs/dev-deployment.md) 补一句“股票池管理”入口。

---

## 4. 关键决策与假设

1. **内置池只读**：只允许“启动/更新”和“查看成员”；改名/增删成员 API 返回 403。C1/C2/C3 仍可通过原 CLI/CSV/YAML 刷新。
2. **自定义池 ID**：自动生成 `U1`, `U2`…；显示名称用户可编辑，要求全局唯一。
3. **每日调度范围**：数据/因子/组合/跟踪每日更新；回测只在以下情况重跑：
   - 最新行情日期 > 最近回测 `end_date`；
   - `members_updated_at` > 最近回测 `created_at`。
4. **C0 特殊处理**：C0 的组合/跟踪/信号由全局每日流水线负责；池任务对 C0 只做数据检查与回测，不重复组合/跟踪；`daily_enabled` 固定视为开启。
5. **任务执行**：后台线程 + `task_log` 状态，前端轮询；同一时间只允许一个池任务（SQLite 单写者）。
6. **名称解析**：优先 `dim_stock`，缺失时用腾讯全市场快照；仍无法解析则要求用户手工填写名称，不允许无名成员入库。
7. **成员板块**：自定义成员 `sector_code` 优先取 `dim_stock.industry`，否则 `"自定义"`；不新增行业输入。
8. **底部年度表现卡**：继续读取 `docs/experiments_universes.csv`，仅展示内置池；自定义池的回测在顶部“策略回测”卡片展示。
9. **不包含**：删除整个股票池、池成员排序、权重自定义、鉴权。页面写入接口仅适合内网/受限安全组使用。
10. **兼容性**：C0~C3 现有 CLI、API、每日流水线、回测结果命名保持不变；新增字段有默认值，旧库自动迁移。

---

## 5. 实施顺序

1. `storage/schema.sql` + `storage/db.py`：新增字段、C0 行、迁移。
2. `research/universe.py`：`refresh_universe/prune_universe` 更新 `members_updated_at`。
3. `research/pool_admin.py`：代码规范化、名称解析、池 CRUD、状态计算。
4. `features/engine.py`：`compute_all` 支持 `symbols` 过滤。
5. `strategy/tracking.py` + `service/app.py`：跟踪基准按池；API 传 `universe_id`。
6. `pipeline/pool_jobs.py`：池任务、幂等检查、后台锁、每日任务汇总。
7. `pipeline/daily.py`：接入 `run_enabled_pool_jobs`。
8. `service/pool_routes.py` + `service/app.py`：路由与错误映射。
9. `service/templates/index.html`：动态 tab、管理卡片、弹窗、轮询、样式。
10. `tests/test_pool_admin.py`、`tests/test_pool_jobs.py`：单元测试。
11. `docs/PROJECT_MEMORY.md`、`docs/operations-runbook.md`：文档。
12. 运行迁移、测试、API/浏览器验收。

---

## 6. 验收步骤

### 6.1 自动化

```bash
cd /data02/stock-cash-streaming
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest -q
```

期望：现有 6 项测试 + 新增测试全部通过。

### 6.2 数据库迁移

```bash
.venv/bin/python -m stock_cash_streaming.cli status
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('data/stock_cash_streaming.sqlite3')
print([r[1] for r in c.execute('pragma table_info(dim_universe)')])
print(c.execute("select universe_id,name,is_custom,daily_enabled from dim_universe order by 1").fetchall())
PY
```

期望：`is_custom/daily_enabled/members_updated_at` 存在；C0 行存在；C1~C3 保留。

### 6.3 API

```bash
# 名称解析
curl -s -X POST http://127.0.0.1:8000/api/pools/resolve \
  -H 'Content-Type: application/json' \
  -d '{"codes":["600406","000001","159611"]}'

# 新建池
curl -s -X POST http://127.0.0.1:8000/api/pools \
  -H 'Content-Type: application/json' \
  -d '{"name":"我的测试池","members":[{"symbol":"600406","name":"国电南瑞"},{"symbol":"000001","name":"平安银行"}]}'

# 列表/详情/改名/加成员/删成员
curl -s http://127.0.0.1:8000/api/pools
curl -s http://127.0.0.1:8000/api/pools/U1
curl -s -X PATCH http://127.0.0.1:8000/api/pools/U1 \
  -H 'Content-Type: application/json' -d '{"name":"我的新名字"}'
curl -s -X POST http://127.0.0.1:8000/api/pools/U1/members \
  -H 'Content-Type: application/json' -d '{"members":[{"symbol":"600519","name":"贵州茅台"}]}'
curl -s -X DELETE http://127.0.0.1:8000/api/pools/U1/members/600519

# 内置池只读（应返回 403）
curl -s -o /dev/null -w '%{http_code}\n' -X PATCH http://127.0.0.1:8000/api/pools/C1 \
  -H 'Content-Type: application/json' -d '{"name":"改名"}'

# 启动并轮询
curl -s -X POST http://127.0.0.1:8000/api/pools/U1/activate
curl -s http://127.0.0.1:8000/api/pools/U1/job
```

### 6.4 页面验收

使用浏览器打开 `http://localhost:8000`：

1. 顶部 tab 出现自定义池，点击后 KPI/表格/热力图/组合/回测切换正常。
2. “股票池管理”卡片：
   - 新建池：输入代码 → 解析出名称 → 确认创建；
   - 自定义池：改名、添加股票、删除成员；
   - 内置池：改名/增删成员按钮不可用；
3. 点击“启动/更新”：
   - 首次运行显示数据/因子/回测/组合/跟踪/调度步骤；
   - 再次点击显示“已是最新/已回测/已开启调度”等跳过原因；
   - 页面状态 badge 与任务结果一致。
4. 刷新页面后自定义池仍在。
5. 移动端宽度 390px 下管理表格可横向滚动、弹窗可用。

### 6.5 每日调度验收

```bash
# 开启一个池后，手动跑一次每日流水线
.venv/bin/python -m stock_cash_streaming.cli daily --force
```

检查：

```bash
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('data/stock_cash_streaming.sqlite3')
print(c.execute("select universe_id,daily_enabled from dim_universe order by 1").fetchall())
print(c.execute("select task_id,status,ended_at from task_log where task_id like 'pool_%' order by ended_at desc limit 5").fetchall())
print(c.execute("select strategy,max(trade_date) from fact_target_run where strategy like '%_U1' group by 1").fetchall())
print(c.execute("select strategy,end_date,created_at from backtest_run where strategy like '%_U1' order by created_at desc limit 3").fetchall())
PY
```

期望：池任务 `ok/skipped`；自定义策略 `defensive_top12_monthly_U1` 有目标组合/跟踪/回测记录；第二次运行因数据/回测已最新而跳过。

### 6.6 回归

- C0~C3 顶部 tab、KPI、回测、组合、跟踪与改动前一致（C0 tracking 基准不变）。
- `cli daily`、`cli backtest --universe Cx`、`cli portfolio`、`cli tracking` 行为不变。
- 旧库自动迁移，无需手工 SQL。
