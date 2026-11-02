# 自然语言策略（LLM → 白名单 DSL → 回测）实施计划

## 1. 摘要

在左侧导航新增「自然语言策略」入口：用户用中文描述交易想法（如「周K线9连阴后买入并持仓1周」），系统调用现有 LLM 配置把它解析成**白名单 DSL（JSON 规则）**，页面展示解析后的规则供确认，用户选择股票池后一键回测，输出收益/胜率/回撤/净值曲线与逐笔交易明细；策略可保存并复用。

已确认的产品决策：

- **执行形式**：白名单 DSL。LLM 只输出受控 JSON，后端用内置引擎解释执行；不允许生成任意 Python 代码。
- **建仓口径**：组合多股等权。每个交易日扫描池内标的，有信号即买入，最多同时持有 N 只（默认 5），持有 K 个交易日后卖出。
- **落库**：保存策略（名称 + 原始自然语言 + DSL + 最近一次回测摘要），逐笔成交不落库，每次现算。

---

## 2. 现状分析

### 2.1 可复用能力

- 行情与池成员：`fact_quote(close, pct_chg, volume, amount)`；`universe_members(repo, universe)` 返回 `{symbol: sector_code}`（已过滤 ST/退），见 [universe.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/research/universe.py#L167-L197)。
- 可交易性过滤：`eligibility_panel(repo, symbols)` → dates × symbols 的 bool 掩码（停牌/新股/ST/退市），见 [universe.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/research/universe.py#L275-L337)。
- 成本与涨跌停口径：`_a_share_rates(trade_date, cost_rate)` 与 `_limit_threshold(symbol, trade_date, "board")`，见 [engine.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/backtest/engine.py#L30-L53)。
- 指标计算：`compute_metrics(nav, benchmark)`，见 [metrics.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/backtest/metrics.py#L12-L45)。
- LLM 调用范式：`OpenAI(api_key, base_url)` + `chat.completions.create(model, messages, temperature)`，见 [daily.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/report/daily.py#L96-L109)；配置项 `openai_api_key / openai_base_url / llm_model`，见 [settings.py](file:///data02/stock-cash-streaming/src/stock_cash_streaming/config/settings.py#L37-L40)。
- 前端：左侧导航 `data-nav` + hash 路由（`#/overview`、`#/pool/{id}`），页面区块 `.page` + `.card`，见 [index.html](file:///data02/stock-cash-streaming/src/stock_cash_streaming/service/templates/index.html#L275-L291)；已有 `fmtMoney/fmtPct/chart/ax/tip` 等工具函数。
- 现有回测引擎是「截面打分 → 权重」模式（`run_backtest`），不支持「事件触发 + 固定持有期」，因此需要新增一个轻量事件驱动回测器。

### 2.2 关键约束

- 现有 `fact_quote.close` 为后复权价，收益连续，可直接用于净值与交易；成本/涨跌停口径与现有回测一致即可。
- 策略必须可解释：DSL 在页面以中文摘要 + JSON 双视图展示，用户确认后再回测。
- LLM 不可用时（未配置 key / 调用失败）必须仍可使用：提供「手动编辑 JSON」入口。

---

## 3. 方案设计

### 3.1 DSL 定义（白名单）

```json
{
  "name": "周K线9连阴后买入持有1周",
  "timeframe": "W",                         // "D" 日线 / "W" 周线
  "entry": {                                 // 条件组，默认 all（全部满足）
    "all": [
      {"indicator": "consecutive_down", "params": {"n": 9}}
    ]
  },
  "hold_days": 5                             // 持仓交易日数，1~60
}
```

**支持的指标（闭集）**

| indicator | 参数 | 含义 |
|---|---|---|
| `consecutive_down` | `n` | 连续 n 根阴线（收盘 < 前收） |
| `consecutive_up` | `n` | 连续 n 根阳线 |
| `macd_cross` | `fast=12, slow=26, signal=9, direction=golden\|death` | MACD 金叉/死叉 |
| `ma_cross` | `fast=5, slow=20, direction=golden\|death` | 均线金叉/死叉 |
| `rsi` | `n=14, op=<\|>\|<=∣>=, value` | RSI 比较 |
| `return_pct` | `n, op, value` | 近 n 根 K 线涨跌幅（%）比较 |
| `close_vs_ma` | `n, op, value` | 收盘相对 N 日均线偏离（%） |
| `boll` | `n=20, k=2.0, side=lower\|upper` | 触及布林上/下轨 |
| `volume_shrink` | `n=5, ratio=0.7` | 近 n 根均量 / 20 根均量 < ratio |

校验规则（`validate_spec`）：
- `timeframe ∈ {D, W}`；`hold_days ∈ [1, 60]`；`entry.all` 非空且 ≤ 5 个条件；
- 指标必须在白名单内；参数类型/范围受限（`n ∈ [2, 120]`、`op` 枚举等）；
- 任何未知字段直接拒绝，避免 LLM 幻觉被静默忽略。

示例映射：
- 「周K线9连阴后买入并持仓1周」→ `W + consecutive_down(9) + hold_days=5`
- 「日K线10连阴后买入并持仓2日」→ `D + consecutive_down(10) + hold_days=2`
- 「周K线 MACD 金叉后买入并持仓2周」→ `W + macd_cross(golden) + hold_days=10`

### 3.2 新增模块 `src/stock_cash_streaming/research/nl_rules.py`

**纯计算（便于单测）**

- `compute_indicator(df_by_symbol, cond) -> pd.DataFrame[dates × symbols] bool`
  - 输入为按日（或按周）重采样后的 DataFrame 字典（`close / volume`），逐指标实现。
- `resample_bars(close, volume, timeframe) -> (close, volume, bar_end_dates)`
  - `D` 原样；`W` 用 `W-FRI` 重采样，标签取该周最后一个交易日（用于执行日映射）。
- `build_signal_matrix(prices, spec) -> pd.DataFrame[dates × symbols] bool`
  - 组合 `entry.all` 各条件（`&`）。
- `simulate_rule_portfolio(prices, signals, eligibility, pct, capital=100000, top_n=5, hold_days=5)`
  - 事件驱动：
    1. 每个交易日 d：先检查持仓，达到 `hold_days` 的按 d 收盘卖出（跌停不可卖则顺延）；
    2. 再处理 d-1 收盘产生的信号：按信号日排序、按代码升序取前 `top_n - 持仓数` 只，按 d 收盘买入，单只目标金额 = 初始资金 / `top_n`，整手（100 股），现金不足则跳过；
    3. 记录 `trades`（买入日、卖出日、持有交易日、买入价、卖出价、收益率）与每日 `nav`（含现金）。
  - 约束：不加杠杆、不做空；涨停不可买；`eligibility=False` 的标的不参与；卖出时若跌停顺延到下一可交易日。
  - 返回 `{nav, benchmark, trades, positions, metrics, trade_stats}`，其中 `trade_stats` 含 `trades / win_rate / avg_win / avg_loss / avg_hold_days / max_concurrent`。

**LLM 解析**

- `parse_nl_strategy(text: str) -> dict`
  - 系统提示词要求「只输出 JSON，且只能使用给定指标与参数」；使用 `response_format={"type": "json_object"}`（DeepSeek 兼容），`temperature=0`；
  - 解析后调用 `validate_spec` 归一化；失败抛 `StrategyParseError`（带可读原因）；
  - 未配置 `openai_api_key` 抛 `StrategyParseError("未配置 LLM（OPENAI_API_KEY）")`，API 层返回 503，页面回退到手动 JSON。
- `describe_spec(spec) -> list[str]`：中文逐条规则（如「周线连续 9 根阴线」「持有 5 个交易日」），用于页面确认。

**回测入口**

- `run_nl_backtest(repo, spec, universe, top_n=5, start=None, end=None, capital=100000) -> dict`
  - 加载池成员 → 行情矩阵（后复权 close/pct/volume，按需切片）→ `eligibility_panel` → 信号 → `simulate_rule_portfolio` → `compute_metrics`；
  - 返回 `{spec, describe, universe, params, metrics, trade_stats, nav:[{date, nav, benchmark}], trades:[...]}`，供 API 直接返回。

### 3.3 存储（`schema.sql` 追加，启动自动建表）

```sql
CREATE TABLE IF NOT EXISTS dim_nl_strategy (
    strategy_id  TEXT PRIMARY KEY,   -- NL1, NL2...
    name         TEXT,
    nl_text      TEXT,               -- 用户原始描述
    spec_json    TEXT,               -- 归一化后的 DSL
    universe_id  TEXT,               -- 最近一次回测的池
    last_result  TEXT,               -- JSON 摘要（年化/胜率/回撤/交易数/时间）
    last_run_at  TEXT,
    created_at   TEXT,
    updated_at   TEXT
);
```

### 3.4 API（新增 `service/nl_routes.py`，`APIRouter(prefix="/api/nl")`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/nl/parse` | `{text}` → `{spec, describe}`；未配置 LLM → 503 |
| POST | `/api/nl/backtest` | `{spec, universe, top_n=5, start?, end?, save=false, name?, nl_text?}` → 回测结果；`save=true` 时落库并更新 `last_result` |
| GET | `/api/nl/strategies` | 已保存策略列表（含最近结果摘要） |
| GET | `/api/nl/strategies/{id}` | 详情（含 DSL） |
| DELETE | `/api/nl/strategies/{id}` | 删除 |
| POST | `/api/nl/strategies/{id}/run?universe=` | 用已存策略重跑并更新 `last_result` |

错误映射：DSL 非法/股票池为空 → 400；LLM 未配置 → 503；未捕获异常 → 500 并记录日志。

### 3.5 CLI（`cli.py` 新增 `nl` 子命令）

```bash
.venv/bin/python -m stock_cash_streaming.cli nl \
  --text "周K线9连阴后买入并持仓1周" --universe C0 --top-n 5 [--save]
.venv/bin/python -m stock_cash_streaming.cli nl --spec-file spec.json --universe C1
```

`--spec-file` 用于无 LLM 时直接跑 JSON，便于测试与运维排查。

### 3.6 前端（`index.html`）

**左侧导航**：新增分组「研究」→「自然语言策略」（`data-nav="nl"`，hash `#/nl`）。

**新页面 `#pageNL`（两栏，移动端单列）**
1. **策略描述卡**：`textarea` 输入（placeholder 给出三条示例）+「解析策略」按钮 + 状态行；
   - 解析成功：显示「规则确认」区域——名称、周期（日线/周线）、条件逐条中文、持仓天数，以及可折叠的 JSON；
   - 解析失败/无 LLM：提示原因，并展开「手动编辑 JSON」`textarea` +「应用规则」按钮。
2. **回测设置卡**：股票池下拉（`/api/pools`）、最多持仓数（默认 5）、开始/结束日期（可选）、「运行回测」、「保存策略」勾选 + 名称。
3. **结果区**：
   - KPI：累计收益、年化、夏普、最大回撤、交易笔数、**胜率**、平均持有交易日、期末资金；
   - 净值曲线（策略 vs 该池等权，复用 `chart/ax/tip`）；
   - 交易明细表：买入日、卖出日、股票、买入价、卖出价、持有天数、收益率（最近 200 笔，红涨绿跌）；
   - 规则摘要与参数回显。
4. **已保存策略列表**：名称、周期/持仓、最近年化/胜率、操作（载入、重跑、删除）。

**路由改造**：`parseHash` 增加 `nl` 分支；`applyView` 增加 `$('pageNL')` 显隐、标题「自然语言策略」、`setActiveNav` 支持 `data-nav="nl"`。

### 3.7 测试（新增 `tests/test_nl_rules.py`）

1. `validate_spec`：未知指标、`hold_days=0/61`、空条件、非法 `op` 均报错；合法示例归一化成功。
2. `resample_bars`：构造 10 个交易日（跨两周）→ 周线 2 根，标签为各周最后交易日。
3. `build_signal_matrix`：合成「连续 3 根阴线」价格 → 第 4 根信号为 True，其它 False；MACD 金叉在构造序列上至少触发一次。
4. `simulate_rule_portfolio`：3 只标的、信号集中在第 5 天 → 买入不超过 `top_n`、持有恰好 `hold_days` 后卖出、现金不为负、交易收益率计算正确、`win_rate == 盈利笔数/总笔数`。
5. `parse_nl_strategy`：monkeypatch OpenAI 返回合法 JSON → 得到归一化 spec；返回缺字段/非法指标 → `StrategyParseError`。
6. `run_nl_backtest`（临时 SQLite + 合成 `fact_quote` + monkeypatch `universe_members`）：返回 metrics/trades/nav 结构完整。

### 3.8 文档

- `docs/PROJECT_MEMORY.md` 新增 22.18「自然语言策略」：DSL 白名单、执行口径（次日收盘、整手、最多 N 只、持有 K 日）、LLM 配置与降级、API/页面、局限。
- `docs/operations-runbook.md`：新增一行「自然语言策略解析失败 / 无 LLM 怎么办」（手动 JSON 或 `--spec-file`）。
- `docs/dev-deployment.md`：常见问题加一行入口说明。

---

## 4. 关键决策与假设

1. **只用 DSL**：不执行 LLM 生成的代码；DSL 指标为闭集，超出范围时提示「暂不支持，请换一种说法」，并允许手动编辑 JSON。
2. **执行时点**：信号在 K 线收盘计算，**次一交易日收盘**成交（与现有回测/组合口径一致）；周线信号的执行日为下一交易日。
3. **持仓期**：`hold_days` 按交易日计（1 周≈5 日、2 周≈10 日）；到期按当日收盘卖出，跌停顺延。
4. **仓位**：每只目标金额 = 初始资金 / `top_n`，整手买入；满仓后新信号忽略；不加杠杆、不做空、不加仓。
5. **同一天多信号**：按代码升序取前 N，保证结果可复现。
6. **风控口径**：复用 `eligibility_panel`（停牌/新股/ST）与 `board` 涨跌停模型；成本复用 `_a_share_rates`（含滑点）。
7. **落库范围**：只存策略与最近一次摘要；逐笔成交每次现算，不落库（避免表膨胀）。
8. **LLM**：复用现有 `OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL`；未配置时功能降级为手动 JSON，不影响回测。
9. **不做**：多条件 OR 嵌套（仅 `all`，可后续扩展）、参数自动优化、策略对比、组合级仓位管理。

---

## 5. 实施顺序

1. `research/nl_rules.py`：DSL 定义/校验、指标计算、周线重采样、组合回测、`run_nl_backtest`、LLM 解析。
2. `storage/schema.sql`：新增 `dim_nl_strategy`。
3. `service/nl_routes.py` + `app.py` `include_router`。
4. `cli.py`：新增 `nl` 子命令（`--text / --spec-file / --universe / --top-n / --save`）。
5. `index.html`：导航项、`#pageNL`、路由与渲染逻辑。
6. `tests/test_nl_rules.py`。
7. 文档（PROJECT_MEMORY / runbook / dev-deployment）。
8. 迁移、测试、API/页面/CLI 验收，提交推送。

---

## 6. 验收步骤

### 6.1 自动化

```bash
cd /data02/stock-cash-streaming
.venv/bin/python -m compileall -q src
.venv/bin/python -m pytest -q        # 现有 22 项 + 新增 NL 测试全部通过
```

### 6.2 CLI（不依赖 LLM）

```bash
cat > /tmp/spec.json <<'JSON'
{"name":"周线9连阴","timeframe":"W","entry":{"all":[{"indicator":"consecutive_down","params":{"n":9}}]},"hold_days":5}
JSON
.venv/bin/python -m stock_cash_streaming.cli nl --spec-file /tmp/spec.json --universe C0
# 期望：打印规则摘要、年化/胜率/最大回撤/交易笔数，以及最近 5 笔交易
```

### 6.3 API

```bash
curl -s -X POST http://127.0.0.1:8000/api/nl/parse \
  -H 'Content-Type: application/json' \
  -d '{"text":"周K线9连阴后买入并持仓1周"}'

curl -s -X POST http://127.0.0.1:8000/api/nl/backtest \
  -H 'Content-Type: application/json' \
  -d '{"spec":{"timeframe":"W","entry":{"all":[{"indicator":"consecutive_down","params":{"n":9}}]},"hold_days":5},"universe":"C0","top_n":5,"save":true,"name":"周线9连阴"}'

curl -s http://127.0.0.1:8000/api/nl/strategies
```

### 6.4 页面

1. 左侧出现「自然语言策略」，点击进入 `#/nl`。
2. 输入「日K线10连阴后买入，并持仓2日」→「解析策略」→ 显示规则（日线、连续 10 根阴线、持有 2 日）。
3. 选择股票池 C0、最多持仓 5 →「运行回测」→ 出现 KPI、净值曲线、交易明细；胜率与交易笔数一致。
4. 勾选保存 → 列表出现该策略；「重跑」可再次回测；「删除」后消失。
5. 未配置 LLM 时：解析提示 503 原因，可切换到手动 JSON 完成回测。
6. 移动端 390px：页面单列、表格可横向滚动。

### 6.5 回归

- 现有页面（总览/池详情/模拟持仓）、`cli daily`、池任务、`/api/pools` 等行为不变。
- 旧库启动自动建表，无需手工 SQL。
