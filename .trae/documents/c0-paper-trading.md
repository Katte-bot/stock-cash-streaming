# C0 模拟持仓（10 万纸面账户）实施计划

## 1. 摘要

为 C0 股票池新增「模拟持仓」：以 100,000 元虚拟资金，按与回测/目标组合完全一致的月频调仓规则生成**可读的成交流水 + 当前持仓 + 资产曲线**，并在每日流水线中自动延续，供直观持续观察。其它池暂不启用（模块预留 `universe_id`）。

已确认的产品决策：

- **起点**：历史回放 + 实时跟踪。首次初始化时跑一次 C0 回测，把 2018 年以来的月频调仓权重回放成历史成交；之后每个调仓日继续追加。
- **资金**：默认 100,000 元，页面提供「重置」按钮（按当前最新组合重新建仓，清空旧流水）。
- **页面位置**：池详情新增独立分区，保留现有「模拟跟踪」净值视角。

---

## 2. 现状分析

### 2.1 已有能力（可复用）

- 目标组合：`build_target_portfolio()` 生成 Top12 / 1÷vol 加权 / 25% 波动目标的权重，落库 `fact_target_portfolio`（`is_rebalance` 标记调仓），首次建档强制 `is_rebalance=1`，见 [portfolio.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/strategy/portfolio.py#L277-L292)。
- 日频净值跟踪：`simulate_tracking()` 按快照 + 收盘价推净值，但只输出**权重视角**的净值，没有成交、股数、现金，见 [tracking.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/strategy/tracking.py#L69-L140)。
- 调仓日历：`_rebalance_position()` 用 `i % 20` 与回测对齐，见 [portfolio.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/strategy/portfolio.py#L92-L101)。
- 回测权重：`run_backtest(..., return_weights=True)` 返回逐日持仓权重 DataFrame（`weight_hist`），权重在 d 日持有、即 d-1 收盘执行，见 [engine.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/backtest/engine.py#L579-L586)。
- 行情：`fact_quote(symbol, trade_date, close, ...)`，价格为后复权，天然规避拆股/分红导致的股数变化。
- 每日流水线：增量 → 因子 → 信号 → 目标组合 → 跟踪 → 池任务，见 [daily.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/pipeline/daily.py#L31-L135)。

### 2.2 关键事实（已核实数据）

- C0 的 `fact_target_portfolio` 只有 2026-10-19 起的快照（1 次调仓），**无法直接回放长历史**；因此历史段必须由回测权重生成。
- `fact_tracking_nav` 只有 5 天记录，现有「模拟跟踪」目前处于刚起步状态。
- 回测权重与实盘快照使用同一调仓日历（`i % 20`）与同一套成本/涨跌停口径，边界衔接不会产生口径冲突。

---

## 3. 方案设计

### 3.1 数据表（`storage/schema.sql` 追加，`CREATE TABLE IF NOT EXISTS` 自动建表）

```sql
-- 模拟账户（每个股票池一个）
CREATE TABLE IF NOT EXISTS fact_paper_account (
    universe_id     TEXT PRIMARY KEY,
    initial_capital REAL,      -- 初始资金（默认 100000）
    cash            REAL,      -- 最新现金
    start_date      TEXT,      -- 首个成交日
    last_date       TEXT,      -- 最近结算日
    total_value     REAL,      -- 最近总资产
    rebalances      INTEGER DEFAULT 0,
    trades_count    INTEGER DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT
);

-- 模拟调仓事件（回放用；backtest=历史回测权重，live=目标组合快照）
CREATE TABLE IF NOT EXISTS fact_paper_event (
    universe_id TEXT,
    exec_date   TEXT,          -- 执行日（该日收盘成交）
    symbol      TEXT,
    weight      REAL,
    source      TEXT,          -- backtest / live
    created_at  TEXT,
    PRIMARY KEY (universe_id, exec_date, symbol)
);

-- 成交流水（append-only 语义，重建时按池删除重写）
CREATE TABLE IF NOT EXISTS fact_paper_trade (
    universe_id TEXT,
    trade_date  TEXT,
    seq         INTEGER,       -- 同日序号，保证顺序稳定
    symbol      TEXT,
    name        TEXT,
    side        TEXT,          -- buy / sell
    price       REAL,
    shares      INTEGER,
    amount      REAL,          -- 成交金额（不含费用）
    fee         REAL,
    reason      TEXT,          -- initial / rebalance
    created_at  TEXT,
    PRIMARY KEY (universe_id, trade_date, seq)
);

-- 当前持仓（每次重建后整体覆盖）
CREATE TABLE IF NOT EXISTS fact_paper_position (
    universe_id  TEXT,
    symbol       TEXT,
    name         TEXT,
    shares       INTEGER,
    avg_cost     REAL,         -- 含买入费用摊薄后的每股成本
    last_price   REAL,
    market_value REAL,
    weight       REAL,         -- 占总资产比例
    pnl          REAL,
    updated_at   TEXT,
    PRIMARY KEY (universe_id, symbol)
);

-- 资产曲线
CREATE TABLE IF NOT EXISTS fact_paper_nav (
    universe_id TEXT,
    trade_date  TEXT,
    total_value REAL,
    cash        REAL,
    market_value REAL,
    ret         REAL,          -- 累计收益率
    benchmark   REAL,          -- 该池等权基准（起点归一）
    drawdown    REAL,
    created_at  TEXT,
    PRIMARY KEY (universe_id, trade_date)
);
```

不需要 `ALTER`，`db.py` 的 `init_schema` 会在服务启动时自动建表。

### 3.2 业务模块：新增 `src/stock_cash_streaming/strategy/paper.py`

**常量**

```python
DEFAULT_CAPITAL = 100_000.0
LOT = 100
COMMISSION_RATE, MIN_COMMISSION, TRANSFER_RATE = 0.00025, 5.0, 0.00001
STAMP_BEFORE, STAMP_AFTER, STAMP_SWITCH = 0.001, 0.0005, "2023-08-28"
```

**纯函数（便于单测，不碰数据库）**

- `trade_fee(side, trade_date, amount) -> float`
  - 佣金 `max(5, amount*0.00025)`；过户费 `amount*0.00001`；卖出另加印花税（2023-08-28 前 0.1%，之后 0.05%）。
- `simulate_ledger(prices: pd.DataFrame, events: list[tuple[str, dict[str, float]]], capital: float) -> dict`
  - `prices`：index=trade_date、columns=symbol、已 ffill 的收盘价。
  - 逐事件执行：先按目标权重算目标市值 → 折整手（`floor(target_value / (price*(1+费率)) / LOT) * LOT`）→ **先卖后买**（卖出释放现金）→ 若现金不足以完成全部买入，按剩余现金等比缩量后再折整手 → 仍不足 1 手的标的跳过（记录 `skipped` 计数）。
  - 买入更新 `avg_cost = (old_shares*avg_cost + amount + fee) / new_shares`；卖出不改 `avg_cost`。
  - 每日收盘结算：`total = cash + Σ shares*close`，写 nav（`ret`、`drawdown`）。
  - 返回 `{trades, nav, positions, cash, total_value, skipped}`，全部为普通 dict/list，按日期升序。
- `_events_from_weights(weights: pd.DataFrame, prices: pd.DataFrame, source: str) -> list[dict]`
  - 遍历 `weights`，当某日权重与上一日不同（`np.allclose` 容差 1e-8）即视为一次调仓，**执行日 = 该交易日的上一交易日**（因为权重在 d 日持有 = d-1 收盘建仓）；首日全 0 跳过。

**数据库函数**

- `get_account(repo, universe_id) -> dict | None`
- `rebuild_paper_account(repo, universe_id="C0", capital=DEFAULT_CAPITAL) -> dict`
  1. 取该池成员（`universe_members`），空则返回 `{"status": "empty"}`。
  2. 跑 `run_backtest(repo, top_k=12, rebalance="monthly", hold_buffer=1.0, strategy="defensive", weighting="vol_inv", vol_target=0.25, universe=members, eligibility=eligibility_panel(repo, members), cost_model="a_share", limit_model="board", persist=False, return_weights=True)`。
  3. `weights` → backtest 事件；再用 `fact_target_portfolio`（`strategy=portfolio_strategy_name(universe)` 且 `is_rebalance=1`）生成 `live` 事件，仅保留 `exec_date > 最后一个 backtest 事件日` 的记录。
  4. 写入 `fact_paper_event`（先按池删除再插入）、`trades/positions/nav/account`（同样先删后写，事务内 `commit`）。
  5. 返回 `{"status": "ok", ...摘要}`。
- `refresh_paper_account(repo, universe_id="C0") -> dict`
  - 无账户 → 调 `rebuild_paper_account`；
  - 有账户 → 追加新的 `live` 事件（`exec_date > MAX(event.exec_date)`），从 **已存事件** 重放（不再跑回测，约 <1s），重写 trades/positions/nav/account。
- `reset_paper_account(repo, universe_id, capital)`：删该池 5 张表数据 → `rebuild_paper_account`。
- `paper_summary(repo, universe_id, trade_limit=100) -> dict`：账户 + 持仓（按权重降序）+ 最近 N 笔成交（倒序）+ nav 序列 + 指标（累计/年化/最大回撤/调仓次数/成交笔数）。未初始化返回 `{"status": "not_initialized"}`。

**基准**：nav 的 `benchmark` 用该池成员等权（`close/base` 求均值，起点与首个成交日对齐），与 `simulate_tracking` 口径一致。

**并发**：模块内 `_LOCK = threading.Lock()`；`rebuild/reset` 另用 `pool_jobs.try_acquire_job_lock()` 非阻塞抢跨进程锁，抢不到返回 `busy`（API 409）。

### 3.3 API（`service/app.py` 追加两个路由）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/paper?universe=C0&trades=100` | 账户摘要 + 持仓 + 最近成交 + 资产曲线；未初始化返回 `status=not_initialized` |
| POST | `/api/paper/reset?universe=C0&capital=100000` | 重建账户（首次初始化 / 重置），同步执行，约 10–30 秒；资金范围 1 万–100 万 |

错误映射：资金非法 → 400；`busy`（每日流水线运行中）→ 409。

### 3.4 每日自动延续（`pipeline/daily.py`）

在 `tracking_candidate` 之后、`risk_monitor` 之前插入：

```python
try:
    from ..strategy.paper import refresh_paper_account
    result["steps"]["paper"] = refresh_paper_account(repo, "C0")
except Exception as e:
    result["steps"]["paper"] = {"error": str(e)}
```

- 每日 `daily` 已重建目标组合与跟踪，paper 在此之后追加 live 事件并重放，天然保持最新。
- 首次部署当天跑一次 `daily --force` 即完成初始化（含一次 C0 回测）。

### 3.5 CLI（`cli.py` 新增 `paper` 子命令）

```bash
.venv/bin/python -m stock_cash_streaming.cli paper --universe C0            # 打印账户摘要
.venv/bin/python -m stock_cash_streaming.cli paper --universe C0 --reset --capital 100000
```

参数：`--universe`（默认 C0）、`--capital`（默认 100000）、`--reset`（强制重建）、`--show`（默认打印摘要，`--reset` 后也打印）。

### 3.6 前端（`service/templates/index.html`）

**分区重排（池详情页）**：① 行情与信号（展开）② 组合与跟踪（展开）**③ 模拟持仓（展开，新增）** ④ 回测与历史（展开）⑤ 因子热力图（折叠）。同步修改 `sec-head` 文案编号。

**③ 模拟持仓卡片内容**：

```
[初始资金 100,000] [总资产 118,420] [累计收益 +18.42%] [现金 3,120] [持仓市值 115,300] [仓位 97.4%]
年化收益 · 最大回撤 · 调仓次数 · 成交笔数
资产曲线（总资产 vs 该池等权基准）
持仓表：股票 | 持股数 | 成本价 | 现价 | 市值 | 占比 | 盈亏
成交流水：日期 | 方向 | 股票 | 价格 | 股数 | 金额 | 费用   （最近 50 笔，可滚动）
[重置账户（10 万）]  说明：按回测/目标组合月频调仓，整手成交，含佣金/印花税/过户费；不含分红再投
```

- 未初始化态：显示说明 + 按钮「初始化模拟账户（10 万）」，点击调 `POST /api/paper/reset`，期间按钮禁用并显示「初始化中…（约 20 秒）」。
- 新增 JS：`loadPaper()`（在 `load()` 内调用）、`renderPaper(p)`、`resetPaper()`；图表 id `paperChart`，颜色沿用现有 `#6366f1`（总资产）与 `#22d3ee` 虚线（基准）。
- 切换池时重新拉取；若该池未启用模拟账户（非 C0），卡片显示「当前仅对 C0 开放模拟持仓」。

### 3.7 测试（新增 `tests/test_paper.py`）

1. `trade_fee`：买入无印花税、卖出有；佣金最低 5 元；2023-08-28 前后印花税不同。
2. `simulate_ledger`：2 只标的、60 个交易日、2 次调仓事件
   - 所有成交股数为 100 的整数倍；
   - `total_value == cash + 持仓市值`（每日）；
   - 首次事件产生 buy、第二次事件产生 sell+buy；
   - 资金不足时不出现负现金；
   - nav 长度 = 交易日数。
3. `_events_from_weights`：权重不变不产生事件；变化日期的执行日为上一交易日。
4. 幂等：同一事件集重放两次，trades/nav 完全一致。
5. `rebuild` 与 `reset(capital=200000)`：在临时 SQLite 上 mock 掉 `run_backtest`（monkeypatch `paper.run_backtest`），验证账户/持仓/流水写入且资金翻倍后持仓市值同比例变化。

### 3.8 文档

- `docs/PROJECT_MEMORY.md` 新增 22.17「C0 模拟持仓」：数据表、费用与整手规则、历史段来自回测权重、实时段来自目标组合快照、每日刷新、重置接口、局限（不含分红/送转，价格为后复权；资金不足 1 手时跳过）。
- `docs/operations-runbook.md` 增加一条：模拟账户初始化/重置命令（`cli paper --reset`）与 `task_log`/接口排查方式。
- `docs/dev-deployment.md` 常见问题加一行「模拟持仓在哪」。

---

## 4. 关键决策与假设

1. **历史段来源**：回测 `return_weights` 的逐日权重（同一策略、同一成本口径），而不是 `fact_target_portfolio`（历史快照仅从 2026-10-19 开始）。
2. **实时段来源**：`fact_target_portfolio` 中 `is_rebalance=1` 的快照，执行日 = 信号日次一交易日收盘；只取晚于历史段最后事件日的快照，避免重复。
3. **整手**：A 股最小 100 股；买不起 1 手的目标标的跳过，不产生负现金。
4. **费用**：佣金万 2.5（最低 5 元）+ 过户费万 0.1 + 卖出印花税（按日期）；不含滑点（回测有滑点，纸面账户按收盘价真实成交更直观）。
5. **不做**：分红/送转（后复权价格已含）、融资融券、T+0、手工下单、多账户；暂仅 C0，`universe_id` 字段为后续扩展预留。
6. **重建策略**：每次刷新从 `fact_paper_event` 全量重放（约 <1s），保证幂等；只有 `rebuild/reset` 才跑回测。
7. **并发**：重建/重置抢 `data/pool_job.lock`，与每日流水线互斥；GET 只读不加锁。

---

## 5. 实施顺序

1. `storage/schema.sql`：新增 5 张表。
2. `strategy/paper.py`：常量、费用、`simulate_ledger`、事件生成、`rebuild/refresh/reset/summary`。
3. `pipeline/daily.py`：接入 `refresh_paper_account`。
4. `cli.py`：新增 `paper` 子命令并注册。
5. `service/app.py`：`/api/paper`、`/api/paper/reset`。
6. `service/templates/index.html`：分区重排 + 模拟持仓卡片 + 图表/表格/重置交互。
7. `tests/test_paper.py`：单元测试。
8. 文档（PROJECT_MEMORY / operations-runbook / dev-deployment）。
9. 迁移 + 测试 + API/页面/每日任务验收。

---

## 6. 验收步骤

### 6.1 自动化

```bash
cd /data02/stock-cash-streaming
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest -q     # 现有 16 项 + 新增 paper 测试全部通过
```

### 6.2 初始化与 CLI

```bash
.venv/bin/python -m stock_cash_streaming.cli paper --universe C0
# 期望：初始资金 100000，总资产/累计收益/调仓次数/成交笔数，持仓前 5 只
.venv/bin/python -m stock_cash_streaming.cli paper --universe C0 --reset --capital 100000
```

数据库检查：

```bash
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('data/stock_cash_streaming.sqlite3')
print(c.execute("select * from fact_paper_account").fetchall())
print(c.execute("select count(*), min(trade_date), max(trade_date) from fact_paper_trade").fetchall())
print(c.execute("select count(*) from fact_paper_position").fetchall())
print(c.execute("select min(trade_date), max(trade_date), count(*) from fact_paper_nav").fetchall())
PY
```

期望：账户 1 行（C0，100000）；成交数百笔、覆盖 2018 至今；持仓 10–12 只；nav 覆盖同一区间。

### 6.3 API

```bash
curl -s 'http://127.0.0.1:8000/api/paper?universe=C0' | head -c 600
curl -s -X POST 'http://127.0.0.1:8000/api/paper/reset?universe=C0&capital=100000' | head -c 400
```

### 6.4 页面

1. 打开 C0 详情页，新增「③ 模拟持仓」分区：KPI、资产曲线、持仓表、成交流水可见。
2. 持仓占比合计 ≈ 仓位；市值合计 ≈ 持仓市值；现金 + 市值 = 总资产。
3. 点「重置账户」→ 确认后按钮变「初始化中…」，完成后数据刷新且流水从新起点重建。
4. 切换 C1/C2/C3：显示「当前仅对 C0 开放模拟持仓」。
5. 移动端 390px：表格可横向滚动，卡片不溢出。

### 6.5 每日任务

```bash
.venv/bin/python -m stock_cash_streaming.cli daily --force
```

检查 `task_log` 中 `daily_YYYYMMDD` 的 `message.steps.paper` 为 `ok`，且 `fact_paper_nav` 最新日期 = `fact_quote` 最新日期；重复执行结果幂等（成交笔数不增加）。

### 6.6 回归

- C0~C3 现有回测、目标组合、模拟跟踪、因子热力图、股票池管理行为不变。
- 旧库启动自动建表，无需手工 SQL。
