# 软件架构说明书（SAD）
## A股板块资金流横向对比与轮动量化系统

| 项目 | 内容 |
| --- | --- |
| 文档名称 | 软件架构说明书（Software Architecture Document） |
| 项目代号 | **stock-cash-streaming** |
| 版本 | v1.0 |
| 状态 | 待评审 |
| 编制日期 | 2026-10-14 |
| 关联文档 | [需求分析说明书](./requirements-specification.md)、[PROJECT_MEMORY.md](./PROJECT_MEMORY.md)、[TODO.md](./TODO.md) |

> 本说明书描述系统的**总体架构、技术选型、模块划分、数据架构、关键流程、接口与部署方案**，作为开发实施的蓝图。

---

## 1. 架构目标与设计原则

**架构目标**
1. **数据驱动、可持续**：历史初始化 + 每日增量，长跑不腐化。
2. **研究闭环**：采集 → 因子 → 策略 → 回测 → 实时预测 → 展示，全链路打通且可复现。
3. **低运维、本地优先**：单机可跑，SQLite 零配置；云端为可选增强。
4. **可扩展**：板块、因子、策略均可插拔，新增不改核心。

**设计原则**
- **分层解耦**：采集/存储/计算/服务/展示 分层，层间通过接口依赖。
- **数据源可替换**：适配器模式封装 AkShare / MCP 数据源，统一 `DataSource` 接口。
- **计算与时序无偏**：因子与回测严格避免未来函数（point-in-time）。
- **配置外部化**：《代码即框架，参数即配置》，板块/阈值/权重外置。
- **简单优先**：前端免构建（HTML5+CDN），后端单进程可跑（FastAPI + APScheduler）。

---

## 2. 总体架构

### 2.1 分层架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│  展示层  Presentation (HTML5 + ECharts + TailwindCSS + Alpine.js)     │
│  看板 · 板块对比 · 个股详情 · 热力图 · 回测报告 · AI日报 · 系统状态      │
└───────────────▲───────────────────────────────────▲─────────────────┘
                │ REST (JSON)                        │ WebSocket (增量)
┌───────────────┴───────────────────────────────────┴─────────────────┐
│  服务层  Service (FastAPI + Uvicorn)                                  │
│  查询API · 回测API · 实时推送 · 静态资源 · 任务触发                      │
└───────────────▲───────────────────────────────────▲─────────────────┘
                │                                    │
┌───────────────┴──────────────────┐   ┌─────────────┴─────────────────┐
│  计算层  Compute                  │   │  调度层  Scheduler            │
│  因子引擎 · 策略引擎 · 回测引擎    │   │  APScheduler(收盘EOD/盘中)     │
└───────────────▲──────────────────┘   └─────────────▲─────────────────┘
                │                                     │
┌───────────────┴─────────────────────────────────────┴─────────────────┐
│  存储层  Storage (Repository)                                          │
│  SQLite 主库 · 数据水位 · 原始快照 · (可选) Supabase 远程副本           │
└───────────────▲───────────────────────────────────────────────────────┘
                │
┌───────────────┴───────────────────────────────────────────────────────┐
│  采集层  Ingest (DataSource 适配器)                                    │
│  AkShareAdapter(主) · McpAdapter(备援/校验) · 限速/重试/熔断            │
└───────────────▲───────────────────────────────────────────────────────┘
                │
       外部数据源：AkShare / mcp_stock-data / (可选)LLM
```

### 2.2 数据流（Data Flow）

```
历史初始化:  数据源 ─► 采集 ─► 清洗/规范化 ─► 存储 ─► 因子 ─► 策略 ─► 回测
每日EOD:     水位 ─► 增量采集 ─► upsert ─► 因子 ─► 信号 ─► 日报 ─► 前端
盘中:        定时快照 ─► 盘中因子 ─► 实时预测 ─► WebSocket ─► 前端看板
```

---

## 3. 技术选型

| 层 | 选型 | 理由 |
| --- | --- | --- |
| 语言/运行时 | **Python 3.12** + `.venv` | 满足约束 C1；生态完善；本机已具备 |
| 数据采集 | **腾讯**(行情/全市场快照) + **同花顺**(板块资金流) 为主；`mcp_stock-data` MCP（备援）；东财源在本机被拦 | 免 Key；实测可用（详见 PROJECT_MEMORY §9） |
| 数据处理 | **pandas / numpy / scipy** | 因子与回测的事实标准 |
| 存储 | **SQLite**（主）；可选 **Supabase(Postgres)** | 零运维本地库；云端用于远程访问 |
| 计算 | 自研**因子/策略/回测引擎**（pandas 向量化 + 事件驱动） | 轻量可控、可复现；参考 `backtest_strategy` 指标口径 |
| 服务 | **FastAPI + Uvicorn** | 高性能、自动文档、原生支持 WebSocket |
| 调度 | **APScheduler**（进程内）；可选系统 cron | 单进程即可完成 EOD 与盘中定时 |
| 模板/前端 | **Jinja2** 可选 + **HTML5 + ECharts + TailwindCSS(CDN) + Alpine.js** | 免 Node 构建、界面精美、开发快 |
| 配置 | **pydantic-settings + .env + YAML** | 类型安全、密钥外置 |
| 日志 | **loguru** | 结构化、易轮转 |
| AI 日报 | **OpenAI 兼容 SDK**（可指向 DeepSeek） | 通过环境变量配置（见附录B） |
| 测试 | **pytest** | 单元/回归测试 |

**前端免构建说明**：采用 CDN 引入 ECharts/TailwindCSS/Alpine.js，模板由 FastAPI `Jinja2Templates` 渲染或直接托管静态 HTML。**无 webpack/vite**，降低长期维护成本。

---

## 4. 逻辑架构（模块划分）

```
stock_cash_streaming/                  # 主包
├── config/            # 配置与常量（板块、阈值、成本、数据源）
├── datasource/        # 数据源适配器
│   ├── base.py        #   DataSource 抽象接口
│   ├── akshare_src.py #   AkShare 实现
│   ├── mcp_src.py     #   MCP(mcp_stock-data) 实现
│   └── registry.py    #   适配器注册与故障转移策略
├── storage/           # 存储与仓储
│   ├── db.py          #   连接/迁移/事务
│   ├── schema.sql     #   建表 DDL
│   ├── repository.py  #   通用 upsert/查询
│   └── watermark.py   #   数据水位
├── ingest/            # 采集编排
│   ├── universe.py    #   目标板块/成分股
│   ├── history.py     #   历史初始化
│   ├── incremental.py #   每日增量
│   └── realtime.py    #   盘中快照
├── features/          # 因子引擎
│   ├── base.py        #   Factor 接口与注册
│   ├── flow_factor.py #   主力净流入/占比/连续天数/尾盘
│   ├── institution.py #   机构单趋势强度
│   ├── context_ircf.py#   情境-特征(IRCF)
│   └── neutralize.py  #   行业/市值中性化
├── strategy/          # 策略引擎
│   ├── base.py        #   Strategy 接口与注册
│   ├── intra_sector.py#   板块内强度排序
│   ├── persistence.py #   持续性+股价未反应
│   ├── rotation.py    #   板块间轮动
│   └── signal.py      #   综合信号分与分级
├── backtest/          # 回测引擎
│   ├── engine.py      #   事件驱动主循环
│   ├── vector.py      #   向量化快速回测
│   ├── cost.py        #   交易成本/T+1/涨跌停
│   ├── metrics.py     #   收益/夏普/回撤/IC 等
│   └── report.py      #   报告数据组装
├── predict/           # 实时预测
│   ├── intraday.py    #   盘中因子与当日信号预估
│   └── stream.py      #   WebSocket 广播
├── report/            # AI 日报
│   ├── composer.py    #   规则模板组装
│   └── llm.py         #   LLM 调用与降级
├── service/           # 服务层
│   ├── app.py         #   FastAPI 应用装配
│   ├── api/           #   REST 路由(sector/stock/factor/backtest/report/system)
│   ├── ws.py          #   WebSocket 路由
│   └── static/        #   前端静态资源(templates/、assets/)
├── scheduler/         # 调度
│   ├── jobs.py        #   EOD/盘中任务定义
│   └── runner.py      #   APScheduler 装配
├── common/            # 公共：日志、时间/交易日历、异常、缓存
└── cli.py             # 命令行入口(init/incremental/backtest/serve)
```

**模块依赖方向**：`service → {strategy, backtest, predict, report, storage}`；`{strategy, features} → storage`；`ingest → {datasource, storage}`；**计算层不反向依赖服务层**。

---

## 5. 数据架构

### 5.1 ER 关系（逻辑）

```
dim_stock ──< fact_quote           (1:N  股票-日行情)
dim_sector ─< dim_sector_member    (1:N  板块-成分)
dim_sector ──< fact_sector_flow    (1:N  板块-日资金流)
dim_stock ──< fact_stock_flow      (1:N  股票-日资金流)
dim_stock ──< fact_factor          (1:N  股票-因子值)
dim_stock ──< fact_signal          (1:N  股票-信号)
backtest_run ─< backtest_result    (1:N  回测-结果序列)
ingest_watermark                    (数据集水位)
task_log                            (任务执行历史)
```

### 5.2 表设计（SQLite DDL 草图）

```sql
-- 维度：股票
CREATE TABLE dim_stock (
  symbol TEXT PRIMARY KEY,          -- 如 600519 / 000001
  name   TEXT,
  market TEXT,                      -- sh/sz
  industry TEXT,                    -- 所属行业
  is_st   INTEGER DEFAULT 0,
  status TEXT DEFAULT 'normal',
  updated_at TEXT
);

-- 维度：板块
CREATE TABLE dim_sector (
  sector_code TEXT PRIMARY KEY,
  sector_name TEXT,
  sector_type TEXT,                 -- industry/concept
  updated_at TEXT
);

-- 板块成分
CREATE TABLE dim_sector_member (
  sector_code TEXT, symbol TEXT,
  in_date TEXT, out_date TEXT,
  PRIMARY KEY (sector_code, symbol, in_date)
);

-- 个股日行情
CREATE TABLE fact_quote (
  symbol TEXT, trade_date TEXT,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL, pct_chg REAL, turnover REAL,
  adj_factor REAL,
  src TEXT, fetched_at TEXT,
  PRIMARY KEY (symbol, trade_date)
);

-- 板块资金流
CREATE TABLE fact_sector_flow (
  sector_code TEXT, trade_date TEXT,
  main_net REAL, main_ratio REAL,
  xl_net REAL, l_net REAL, m_net REAL, s_net REAL,          -- 超大/大/中/小 净流入
  xl_ratio REAL, l_ratio REAL, m_ratio REAL, s_ratio REAL,
  src TEXT, fetched_at TEXT,
  PRIMARY KEY (sector_code, trade_date)
);

-- 个股资金流
CREATE TABLE fact_stock_flow (
  symbol TEXT, trade_date TEXT,
  main_net REAL, main_ratio REAL,
  xl_net REAL, l_net REAL, m_net REAL, s_net REAL,
  xl_ratio REAL, l_ratio REAL, m_ratio REAL, s_ratio REAL,
  close REAL, pct_chg REAL,
  src TEXT, fetched_at TEXT,
  PRIMARY KEY (symbol, trade_date)
);

-- 因子值（带版本）
CREATE TABLE fact_factor (
  factor TEXT, symbol TEXT, trade_date TEXT, version TEXT,
  value REAL, extra TEXT,
  PRIMARY KEY (factor, symbol, trade_date, version)
);

-- 信号
CREATE TABLE fact_signal (
  symbol TEXT, trade_date TEXT, phase TEXT,        -- eod / intraday
  score REAL, grade TEXT, reasons TEXT,            -- JSON: 触发理由
  PRIMARY KEY (symbol, trade_date, phase)
);

-- 回测
CREATE TABLE backtest_run (
  run_id TEXT PRIMARY KEY, strategy TEXT, params TEXT,   -- JSON
  start_date TEXT, end_date TEXT, data_version TEXT, created_at TEXT
);
CREATE TABLE backtest_result (
  run_id TEXT, trade_date TEXT, nav REAL, benchmark REAL,
  drawdown REAL, PRIMARY KEY (run_id, trade_date)
);

-- 数据水位
CREATE TABLE ingest_watermark (
  dataset TEXT PRIMARY KEY, last_date TEXT, last_run_at TEXT, status TEXT
);

-- 任务日志
CREATE TABLE task_log (
  task_id TEXT, name TEXT, status TEXT,
  started_at TEXT, ended_at TEXT, message TEXT,
  PRIMARY KEY (task_id)
);
```

**索引建议**：`fact_*` 表按 `trade_date`、`(sector_code, trade_date)`、`(symbol, trade_date)` 建索引，支撑看板与回测查询。

### 5.3 增量与幂等
- **Upsert 策略**：`INSERT ... ON CONFLICT(PK) DO UPDATE`（SQLite ≥3.24）。
- **水位**：`ingest_watermark` 记录每数据集 `last_date`；采集增量区间为 `(last_date, today]`。
- **补数**：`--date-range` 强制重跑，幂等覆盖，不改水位（或按最大值前移）。
- **原始快照**：可选将原始 JSON 落 `data/raw/YYYY-MM-DD/`，用于审计与重放。

### 5.4 数据源适配器（DataSource 抽象）

```python
class DataSource(Protocol):
    def stock_list(self) -> pd.DataFrame: ...
    def board_cons(self, board_name: str, board_type: str) -> pd.DataFrame: ...
    def daily_prices(self, symbol: str, market: str, limit: int) -> pd.DataFrame: ...
    def index_prices(self, symbol: str, period: str, limit: int) -> pd.DataFrame: ...
    def sector_fund_flow(self, days: str, cate: str) -> pd.DataFrame: ...
    def stock_fund_flow(self, symbol: str) -> pd.DataFrame: ...
    def realtime(self, symbol: str, market: str) -> dict: ...
```

- `AkShareAdapter`：直接调用 AkShare 接口（主源，免 Key）。
- `McpAdapter`：封装 `mcp_stock-data` 工具（备援/交叉校验），并可读取 `data_source_status` 熔断状态。
- `registry`：按优先级选择适配器；失败重试 → 切换备源 → 熔断降级（半开恢复）。

---

## 6. 关键流程

### 6.1 历史初始化（一次性，可重复）
```
universe.load(target_sectors)         # 读取板块配置
  └─ board_cons(电网设备/天然气) → 成分股
      └─ for each symbol: daily_prices(full range) → upsert fact_quote
      └─ sector_fund_flow(今日/5日/10日) → fact_sector_flow
      └─ stock_fund_flow(each)          → fact_stock_flow
  └─ watermark.update(all)
  └─ features.compute_all() → fact_factor
```

### 6.2 每日增量（EOD，收盘后）
```
scheduler(15:30 交易日)
  └─ check交易日历 (非交易日跳过)
  └─ incremental.run()
       ├─ 各数据集增量采集(水位→今日) → upsert
       ├─ features.compute(delta)      → fact_factor
       ├─ strategy.run()               → fact_signal(phase=eod)
       ├─ backtest.update_cache()      → 更新看板用回测缓存
       └─ report.compose()+llm()       → 日报归档
  └─ task_log 记录；失败告警
```

### 6.3 盘中实时预测
```
scheduler(9:30-11:30 / 13:00-15:00, 每5min; 14:30后每1-2min)
  └─ realtime.snapshot()           # 板块/个股资金流 + 实时行情
  └─ predict.intraday.factors()    # 盘中因子(含尾盘强度)
  └─ predict.intraday.signals()    # 当日信号预估
  └─ stream.broadcast()            # WebSocket 推送前端
```

### 6.4 回测流程
```
backtest.run(strategy, params, range)
  └─ 载入 fact_* (point-in-time 切片，避免未来函数)
  └─ 计算因子 → 生成交易信号 → 组合构建(等权/市值权重)
  └─ for each 调仓日: 成本/T+1/涨跌停约束下模拟成交
  └─ metrics() → 净值/回撤/夏普/超额/IC
  └─ 持久化 backtest_run + backtest_result
```

---

## 7. 接口设计

### 7.1 REST API（FastAPI，前缀 `/api`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/sectors` | 目标板块列表与概览 |
| GET | `/api/sectors/{code}/flow?days=today|5|10` | 板块资金流时序 |
| GET | `/api/sectors/{code}/rank?date=` | 板块内个股资金流强度排名 |
| GET | `/api/sectors/rotation` | 板块间资金轮动对比 |
| GET | `/api/stocks/{symbol}/flow` | 个股资金流（分档堆叠） |
| GET | `/api/stocks/{symbol}/factors?name=` | 个股因子时序 |
| GET | `/api/stocks/{symbol}/signals` | 个股信号与理由 |
| GET | `/api/signals?date=&grade=` | 信号清单（可按板块/分级筛选） |
| POST | `/api/backtest/run` | 提交回测（返回 run_id） |
| GET | `/api/backtest/{run_id}` | 回测结果（净值/指标） |
| GET | `/api/backtest/{run_id}/report` | 回测报告数据 |
| GET | `/api/report/latest` / `/api/report/{date}` | AI 日报 |
| GET | `/api/system/status` | 数据水位/任务历史/熔断状态 |
| POST | `/api/tasks/{name}/run` | 手动触发任务（增量/因子/信号） |

### 7.2 WebSocket

| 路径 | 消息 | 说明 |
| --- | --- | --- |
| `/ws/realtime` | `{type:"snapshot", ts, sectors:[...], signals:[...]}` | 盘中增量推送 |
| `/ws/tasks` | `{type:"task", name, status, progress}` | 任务进度推送（可选） |

### 7.3 内部接口（关键抽象）
- `DataSource`（见 §5.4）
- `Factor`：`compute(ctx: pd.DataFrame) -> pd.Series`，`name/version/params`。
- `Strategy`：`generate(date, factors) -> signals`。
- `Repository`：`upsert(table, rows)`、`query(...)`、`watermark(dataset)`。

---

## 8. 前端架构

### 8.1 技术形态
- **静态资源**：`service/static/templates/*.html` + `assets/{css,js}`。
- **样式**：TailwindCSS（CDN）+ 少量自定义 CSS 主题变量（明/暗）。
- **图表**：ECharts（CDN）；复用封装 `chart.js` 组件（K线、堆叠柱、热力图、净值）。
- **交互**：Alpine.js（轻量响应式）+ `fetch` 调 REST；`WebSocket` 订阅实时。
- **数据获取**：页面初始化拉 REST；盘中订阅 WS 局部刷新。

### 8.2 页面-数据映射

| 页面 | 主要接口 | 图表 |
| --- | --- | --- |
| 总览看板 | `/sectors`、`/signals`、`/system/status` | 卡片 + 排名条 + 迷你趋势 |
| 板块横向对比 | `/sectors/{code}/rank` | 条形/气泡/热力图 |
| 个股详情 | `/stocks/{sym}/flow|factors|signals` | 堆叠柱 + K线+布林 + 雷达 |
| 因子/热力图 | `/stocks/*/factors`、`/sectors/*/rank` | 矩阵热力图 + 时序 |
| 回测报告 | `/backtest/*` | 净值/回撤/分层/IC |
| AI 日报 | `/report/*` | Markdown 渲染 |
| 系统状态 | `/system/status` | 表格 + 状态徽标 |

### 8.3 视觉规范（摘要）
- 主色：深蓝/靛蓝（专业、沉稳）；强调色：金色（重点）。
- 涨跌：**红涨绿跌**（A股习惯）；中性灰。
- 字体：系统无衬线（PingFang/Inter）；数字用等宽字体对齐。
- 卡片圆角 12–16px、轻阴影、8pt 间距体系；关键指标大字号。

---

## 9. 回测引擎设计

**双引擎**
- `vector.py`：向量化快速回测，用于因子分层/IC 快速验证（分钟内出结果）。
- `engine.py`：事件驱动回测，逐日推进，处理 T+1、涨跌停、停牌、成本、仓位与调仓，用于策略组合精确评估。

**成本模型（`cost.py`）**
```
佣金      = 成交额 × 万2.5（双向，最低5元）
印花税    = 卖出成交额 × 0.05%
过户费    = 成交额 × 0.001%（沪深）
滑点      = 成交价 × 滑点比例（可配置）
```

**约束**：T+1 卖出限制；涨跌停/停牌不可成交；成分股 point-in-time 归属（防幸存者偏差）；**调仓频率：日频（daily rebalance）**。

**指标（`metrics.py`）**：累计/年化收益、年化波动、夏普、最大回撤、卡玛、胜率、盈亏比、换手率、超额收益（vs 基准）、RankIC 均值、ICIR、IC 胜率。

**可复现**：记录 `data_version`（数据水位快照）、参数、随机种子。

---

## 10. 实时预测设计

- **触发**：交易时段定时（默认 5 分钟）；14:30 后加密（1–2 分钟）以捕捉尾盘。
- **输入**：盘中实时快照（板块/个股资金流 + 实时行情）。
- **计算**：盘中版本的因子（含尾盘资金流入强度），当日信号预估 `phase=intraday`。
- **输出**：`{score, grade, reasons[], ts}`，经 WebSocket 广播。
- **降级**：数据源不可用时不推送，保留上次快照并标注时间；连续失败熔断。
- **稳定性跟踪**：保留 `intraday` 与 `eod` 信号，收盘后对比偏差（可选）。

---

## 11. 调度与任务

| 任务 | 触发 | 说明 |
| --- | --- | --- |
| `init_history` | 手动（一次） | 历史初始化 |
| `eod_pipeline` | 交易日 15:30 | 增量→因子→信号→日报 |
| `intraday_snapshot` | 交易时段每 5min | 实时快照与预测 |
| `refresh_members` | 每周 | 刷新板块成分 |
| `vacuum_backup` | 每周 | 数据库压缩与备份 |

- 采用 **APScheduler**（`BackgroundScheduler`），随 FastAPI 进程启动；或提供 `cli.py` 供系统 cron 调用。
- 所有任务写入 `task_log`；失败重试（指数退避）后告警。
- **交易日历**统一封装，非交易日与节假日自动跳过。

---

## 12. 部署与运行

### 12.1 虚拟环境（已创建）
- 路径：`/data02/stock-cash-streaming/.venv`
- 解释器：`.venv/bin/python`（Python 3.12.3）
- 依赖清单：[requirements.txt](../requirements.txt)
- 安装：`.venv/bin/pip install -r requirements.txt`
- 注：本机 pip 配置了企业 Nexus 源（含 401 警告），`extra-index-url` 已含 pypi.org，安装可正常回退；如失败可加 `-i https://pypi.org/simple`。

### 12.2 运行方式
```bash
# 初始化历史数据（一次性）
.venv/bin/python -m stock_cash_streaming.cli init --sectors 电网设备,天然气

# 每日增量（可被 cron 调用）
.venv/bin/python -m stock_cash_streaming.cli incremental

# 启动服务（含调度与前端）
.venv/bin/uvicorn stock_cash_streaming.service.app:app --host 0.0.0.0 --port 8000
```
浏览器访问 `http://localhost:8000`。
> 注：LLM 依赖**系统环境变量**（`OPENAI_API_KEY`/`OPENAI_BASE_URL`/`LLM_MODEL`）。若以 cron/systemd 启动，需显式注入（cron 建议用包装脚本先 `source` 环境）。

### 12.3 配置
- **系统环境变量（主）**：LLM 等敏感配置从进程环境读取（`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`LLM_MODEL`），已验证可用。
- `.env`（可选覆盖）：如需本地覆盖，可从 `.env.example` 复制；**不提交**。
- `config/*.yaml`：板块定义、因子参数、信号阈值、成本参数。
- **密钥不入库、不入版本仓**（`.gitignore` 排除 `.venv/`、`.env`、`data/`）。

### 12.4 可选云端（增强，非必须）
- 存储副本：将 SQLite 同步至 **Supabase(Postgres)**，实现远程访问（`supabase_get_project` / `supabase_apply_migration`）。
- 前端部署：静态资源可部署至 **Vercel**（`deploy_to_remote`）。
- AI 日报：LLM 配置通过 `get_llm_config` 管理的环境变量提供。

---

## 13. 横切关注点

| 关注点 | 方案 |
| --- | --- |
| 配置管理 | `pydantic-settings` 读取 `.env` + YAML；启动时校验 |
| 日志 | loguru；按日轮转；关键链路带 `trace_id` |
| 异常处理 | 分层异常体系（`DataSourceError`/`FactorError`/`BacktestError`）；统一 API 错误响应 |
| 并发 | I/O 采集用线程池限速并发；计算串行保证可复现 |
| 缓存 | 内存 LRU 缓存热点查询；回测结果落库缓存 |
| 监控 | `/api/system/status`；数据源熔断状态；任务成功率 |
| 安全 | CORS 白名单；密钥外置；只读查询默认；无自动交易 |
| 合规 | UI/日报强制风险提示与免责声明 |

---

## 14. 目录结构（目标）

```
/data02/stock-cash-streaming/
├── me.md
├── docs/                      # 文档
│   ├── requirements-specification.md
│   ├── software-architecture.md
│   ├── PROJECT_MEMORY.md      # 实施记忆
│   └── TODO.md                # 任务看板
├── requirements.txt
├── .env.example
├── .gitignore
├── .venv/                     # 虚拟环境（不入库）
├── src/stock_cash_streaming/  # 主包（见 §4）
├── config/                    # 板块/因子/策略配置
├── data/                      # SQLite 与原始快照（不入库）
└── tests/                     # pytest 测试
```

---

## 15. 实施路线（里程碑）

| 里程碑 | 内容 | 对应需求 |
| --- | --- | --- |
| **M0 环境与文档** | `.venv`、依赖、文档、memory/TODO 骨架 | C1、FR-10、AC-01/10 |
| **M1 数据底座** | 数据源适配器、存储、板块/成分、历史行情与资金流 | FR-1.x、FR-2.x、DR-* |
| **M2 因子与信号** | 因子引擎 + 策略引擎 + 信号分级 | FR-3.x、FR-4.x |
| **M3 回测** | 双引擎、成本约束、指标、报告数据 | FR-5.x |
| **M4 服务与前端** | FastAPI、REST、页面与图表 | FR-7.x、FR-8.x、UI-* |
| **M5 增量与实时** | 调度、增量、盘中预测、WebSocket | FR-1.6、FR-6.x、FR-7.3/7.4 |
| **M6 AI 日报与打磨** | LLM 日报、视觉打磨、测试与文档完善 | FR-9.x、NFR-* |

---

## 16. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 数据源接口限流/变更 | 采集失败 | 适配器 + 熔断 + 备源(MCP)；原始快照留存；可补数 |
| 资金流数据口径不一致 | 因子失真 | 统一口径；交叉校验；记录来源与版本 |
| 未来函数导致回测虚高 | 决策误导 | point-in-time 切片；代码评审；基准用例回归 |
| 板块成分漂移 | 覆盖率下降 | 每周刷新成分并保留历史归属 |
| 过拟合 | 实盘失效 | 样本外/滚动验证；参数寻优留白 |
| 单点本地部署 | 数据丢失 | 定期备份；可选云端副本 |
| 长周期维护腐化 | 难接续 | memory/TODO/变更记录；模块化与测试 |

---

## 17. 附录

### 附录A：需求-架构映射（摘要）

| 需求 | 对应架构 |
| --- | --- |
| FR-1.x 采集 | 采集层 + `DataSource` 适配器 + 熔断 |
| FR-2.x 存储 | 存储层 Repository + SQLite + 水位表 |
| FR-3.x 因子 | 计算层 `features/`（基底+注册） |
| FR-4.x 策略 | 计算层 `strategy/`（基底+注册+信号） |
| FR-5.x 回测 | 计算层 `backtest/`（双引擎+成本+指标） |
| FR-6.x 实时 | `predict/` + `scheduler/` + WebSocket |
| FR-7.x 服务调度 | `service/`(FastAPI) + `scheduler/`(APScheduler) |
| FR-8.x 前端 | 展示层（HTML5+ECharts+Tailwind+Alpine） |
| FR-9.x 日报 | `report/`（模板+LLM 降级） |
| FR-10.x 沉淀 | `docs/PROJECT_MEMORY.md`、`docs/TODO.md` |

### 附录B：LLM 环境变量（AI 日报）
系统使用 OpenAI 兼容 SDK 调用 **DeepSeek**（V4.1-Flash，模型名 `deepseek-flash`）。官方文档：`https://api-docs.deepseek.com/`

**配置来源：系统环境变量**（本项目默认不依赖 `.env`；`.env` 仅为可选本地覆盖）。已配置并验证：
```
OPENAI_API_KEY=<deepseek-api-key>       # 已配置（sk-32…）
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
```
- 代码中以 `OpenAI(api_key=..., base_url=...)` **显式构造客户端**；变量名沿用 OpenAI 兼容约定，与系统环境保持一致。
- base_url 官方值 `https://api.deepseek.com`（`/v1` 亦可）。模型名以官方为准：当前 `deepseek-flash`；旧名 `deepseek-v4-flash` 已弃用、仅临时兼容。
- **连通性已验证**：`POST /chat/completions` 返回 `model=deepseek-flash`、内容 `pong`。
- 未配置或调用失败时，日报自动降级为**规则模板**，不影响主流程。
- ⚠️ 若以 cron/systemd 运行，需确保上述变量被注入进程（见 §12.2）。

### 附录C：技术决策记录（ADR 摘要）
| 编号 | 决策 | 理由 |
| --- | --- | --- |
| ADR-01 | 存储选 SQLite 而非 Postgres（本地） | 零运维、单文件、够用；云端可选 |
| ADR-02 | 前端免构建（CDN） | 降低长期维护成本，界面仍可精美 |
| ADR-03 | 数据源适配器 + 熔断 | 提高长期可用性，缓解接口变动风险 |
| ADR-04 | 因子/策略插件化 | 支撑持续迭代与扩展板块 |

---

> **免责声明**：本系统输出均为基于历史数据的量化研究结果，不构成任何投资建议。股市有风险，入市需谨慎。
