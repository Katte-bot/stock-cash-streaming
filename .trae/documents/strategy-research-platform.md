# 策略研究平台升级开发计划（v2）

> 目标：把当前「单一防御型策略 + 规则回测 + 模拟跟踪」升级为**多收益来源的策略研究平台**：策略库、因子实验室、策略对比、因子择时、样本外验证、池级策略绑定。
> 本文只描述设计与实施计划，不含任何收益预测或投资建议。

---

## 1. 背景与目标

### 1.1 输入材料的核心结论

- 不直接照搬海外因子，优先采用在 A 股有实证支持、且能处理 A 股交易制度的策略。
- 现有防御策略偏重「风险 / 交易行为 / 微观结构」，缺少：**估值、盈利质量、投资行为、残差动量、财报事件、因子择时**。
- 建议四层架构：基本面选股 → 价格行为与交易 → 市场环境识别 → 动态策略组合。
- 第一批优先做：**Value + Quality、Residual Momentum、Conservative Investment、PEAD、Factor Momentum**。
- 回测规则（样本外、walk-forward、次日成交、涨跌停/停牌/T+1、财报时点、成本、历史成分、基准）比继续调权重更重要。
- 自然语言模块应从「生成规则」升级为「生成标准化策略配置 → 确定性回测 → 样本外 → 评分」。

### 1.2 本平台升级目标

1. 建立**策略注册表**，把策略从引擎里的 `if/elif` 解耦为可插拔模块，新增策略不改回测主循环。
2. 建立**因子注册表 + 统一面板**，因子口径、方向、版本、依赖单点定义，供回测、IC、NL、页面共用。
3. 新增 **v4 技术/交易因子**与 **v5 基本面/事件因子**，覆盖四层架构所需信号。
4. 实现 10 类策略（首批 5 个），统一使用同一套行情、成本、涨跌停、可交易性与样本外规则。
5. 新增**研究页面**：策略库、策略详情、策略对比、因子实验室、因子择时；股票池详情可绑定策略。
6. 升级自然语言模块：支持「规则模式」与「因子配置模式」，输出可复现的 JSON 配置。
7. 所有新策略/因子具备**无未来函数测试**、样本外结果与统一评分卡。

### 1.3 非目标（本期不做）

- 不做 LLM 生成并执行任意 Python 代码。
- 不做融资融券、多空对冲、杠杆、日内高频。
- 不做参数大规模自动寻优（只保留 walk-forward 验证，避免过拟合）。
- 不承诺任何收益率目标。

### 1.4 已确认的决策（2026-11-04）

| 决策项 | 结论 |
|---|---|
| 首批策略范围 | **第一批 5 个**：Value+Quality、Residual Momentum、Conservative Investment、PEAD、Factor Momentum |
| 回测样本域 | **扩展到全 A**（需新增全 A 行情回填、行业分类与流动性过滤） |
| 界面结构 | **研究 Hub + 顶部 Tab**（策略库 / 策略对比 / 因子实验室 / 因子择时 / 自然语言） |
| 财务数据 | **本期接入现金流量表**，补齐 CAPEX、折旧等，使保守投资策略口径接近 q-factor |

---

## 2. 现状评估

| 模块 | 现状 | 主要缺口 |
|---|---|---|
| 数据 | `fact_quote` 221 只、2018-01-02 ~ 2026-11-02（约 43.6 万行）；`fact_financial` 211 只、12 个指标、公告日 point-in-time | **目标扩展到全 A（约 5,000+ 只）**；无指数行情表；无资本开支/折旧；无全 A 行业分类；无分析师一致预期；无历史成分快照 |
| 技术因子 | `features/engine.py` v2，19 个（vol_20、turnover、amount、amihud、boll、ret_5/20 等） | 缺 60/120/250 波动、下行波动、Beta、特质波动、残差动量、趋势、反转确认 |
| 基本面因子 | `features/fundamentals.py` v3，12 个（roe_ttm、cfo_to_np、ep_ttm、bp_mrq、fcf_yield、div_* 等） | 缺 ROA/ROIC、应计、资产增长、周转率变化、SUE/PEAD、规模 |
| 策略 | `backtest/engine.py` 内 `if/elif`：composite/inverted/flow_rules/defensive/defensive_enhanced/defensive_valuation | 硬编码；池任务 `pool_jobs.py` 固定 `strategy="defensive"` |
| 回测 | 已有成本模型、分板块涨跌停、T+1 近似、缓冲、波动目标、`run_walkforward` 雏形 | 无标准 OOS 拆分与持久化；无容量约束；无统一评分卡 |
| 研究 | `factor_diag.py`（IC）、`regime.py`（三态）、`experiment.py`（消融/滚动）、`strategy_lab.py`（批量） | 均为脚本级，结果不落库、不在页面呈现 |
| NL | `research/nl_rules.py` 规则 DSL（连阴/MACD/RSI 等 10 类指标） | 不能表达「低波动 25% + 价值 25% + 质量 30% + 保守投资 20%」这类因子配置 |
| UI | 左侧导航 + 总览 + 池详情 + `#/nl` | 无策略库/详情/对比/因子实验室/择时页 |

### 2.1 全 A 扩容的影响评估

| 项目 | 现状（221 只） | 全 A（约 5,000+ 只） | 应对 |
|---|---|---|---|
| `fact_quote` 行数 | 43.6 万 | 约 1,100 万（2018 起） | 分批回填 + 单票 watermark 断点续跑；新增 `(trade_date, symbol)` 索引 |
| 库体积 | 约 1.86 GB | 预计 4–8 GB（行情 + 财务） | 定期 `VACUUM`；备份策略改为每周 |
| 因子行数（若全量日频入库） | 1,400 万行 | 3–4 亿行，不可接受 | **研究因子不落日频**：按需计算 + Parquet 缓存；SQLite 只存池成员因子与月度 IC 摘要 |
| 单次面板内存 | 约 60 MB | 2100 × 5400 × 8 字节 ≈ 90 MB/矩阵 | 因子矩阵用 `float32`、按策略只加载依赖因子 |
| 全 A 行业分类 | 仅 71 只 THS 板块成分 | 缺失 | 新增 `dim_stock_industry`（申万一级/二级），中性化优先用它，缺失回退 THS |
| 回填耗时 | 分钟级 | 小时级（受数据源限流） | 分两批：先中证 800 + 创业板/科创板，再其余；夜间执行、可中断续跑 |
| 幸存者偏差 | 当前成分 | 仍只能取到「当前可见」全 A 列表 | 页面明示；每月记录成分快照；退市股尽量补充（数据源允许时） |

> 结论：**行情全 A、研究因子按需** 是可行方案；不把全 A 日频因子全部写入 SQLite。

---

## 3. 总体架构

### 3.1 分层

```
┌───────────────────────────────────────────────────────────────┐
│ 展示层  index.html：研究 Hub（策略库/对比/因子/择时/NL）+ 池详情   │
├───────────────────────────────────────────────────────────────┤
│ 服务层  service/research_routes.py、nl_routes.py、pool_routes.py │
├───────────────────────────────────────────────────────────────┤
│ 研究层  research/: runner(回测编排) ic、regime、factor_momentum、 │
│         scorecard、walkforward、attribution                      │
├───────────────────────────────────────────────────────────────┤
│ 策略层  strategy/registry.py + strategy/library/*（10 类策略）    │
├───────────────────────────────────────────────────────────────┤
│ 因子层  features/registry.py + panel.py + technical_v4.py +      │
│         fundamental_v5.py + events.py                            │
├───────────────────────────────────────────────────────────────┤
│ 数据层  SQLite：fact_quote / fact_factor / fact_financial /       │
│         fact_index_quote / fact_earnings_event / dim_*           │
└───────────────────────────────────────────────────────────────┘
```

### 3.2 关键模块职责

| 新模块 | 职责 |
|---|---|
| `features/registry.py` | 因子元数据：`name / category / direction / version / window / deps / description / neutral`；供计算、IC、UI、NL 白名单统一读取 |
| `features/panel.py` | 一次性构建 `close/ret/pct/amount/turnover/volume` 与因子透视表；统一 ST/停牌/上市天数掩码；替代 `strategy_lab.load_panel` 与 `engine._load_quotes` 的重复实现 |
| `features/technical_v4.py` | 新增技术/交易因子（波动、Beta、特质波动、残差动量、趋势、反转、容量） |
| `features/fundamental_v5.py` | 新增基本面/事件因子（ROA/ROIC、应计、资产增长、SUE、公告日收益） |
| `strategy/registry.py` | `@register("value_quality")` 装饰器、`list_strategies()`、`get_strategy()`、统一 `StrategySpec` |
| `strategy/library/*.py` | 每个策略一个文件，输入 `Panel`，输出评分矩阵（行=日期，列=股票） |
| `research/runner.py` | 统一回测编排：样本内/样本外、成本/涨跌停/容量、结果落库、指标计算 |
| `research/ic.py` | 因子 IC / RankIC / ICIR，按池、按年、按市场状态 |
| `research/factor_momentum.py` | 用候选策略近 3/6/12 个月表现计算动态权重，生成 meta 策略评分 |
| `research/scorecard.py` | 策略评分卡（年化、回撤、夏普、卡玛、超额、换手、IC、OOS 一致性） |
| `research/regime.py` | 扩展为市场状态表：趋势 / 波动分位 / 宽度 / 成交额变化，落 `fact_regime_state` |
| `ingest/full_market.py` | 全 A 股票列表、行情分批回填、单票 watermark、断点续跑与覆盖度统计 |
| `ingest/statements.py` | 现金流量表 / 资产负债表关键科目入库（CAPEX、折旧、总资产、有息负债），沿用公告日 point-in-time |
| `ingest/industry.py` | 全 A 申万一级/二级行业分类，写入 `dim_stock_industry` |
| `features/cache.py` | 研究因子 Parquet 缓存：`data/research_cache/{version}_{universe_hash}_{date}.parquet`，带 TTL 与版本校验 |

### 3.3 数据流

```
行情/财务/指数 → 因子计算(v2/v3/v4/v5) → fact_factor
                                    ↓
         Panel（一次加载）+ eligibility 掩码
                                    ↓
   策略注册表 → 评分矩阵 → 回测引擎（样本内/样本外）→ 指标 + 净值 + 交易
                                    ↓
      fact_strategy_run / fact_strategy_metric / fact_factor_ic / 研究页面
```

---

## 4. 因子体系

### 4.1 v4 技术 / 交易因子（`features/technical_v4.py`）

| 因子 | 口径 | 服务策略 |
|---|---|---|
| `vol_60 / vol_120 / vol_250` | 日收益滚动标准差 × √252 | 低波动 |
| `downside_vol_120` | 仅负收益的滚动标准差 × √252 | 低波动（下行风险） |
| `beta_250` | 个股对等权市场收益的 250 日滚动 β | 低 Beta、残差动量 |
| `ivol_60` | 对市场+行业收益回归后的残差波动 | 低特质风险 |
| `ret_60 / ret_120 / ret_200` | 区间累计收益 | 时间序列动量 |
| `resid_mom_126 / resid_mom_252` | 剔除市场+行业后的残差累计（剔除最近 21 日）÷ 残差波动 | 残差动量 |
| `resid_ret_5 / 10 / 20` | 同上的短窗残差收益 | 短期反转 |
| `ts_trend_60 / 120` | 收盘 > MA60/120 且均线向上（0/1） | 趋势跟踪 |
| `dist_high_60` | 收盘 / 60 日最高 − 1 | 反转、趋势 |
| `lower_shadow_20` | 近 20 日下影线占振幅比例均值 | 反转确认 |
| `volume_shrink_5_20` | 5 日均量 / 20 日均量 | 反转确认 |
| `amihud_std_20` | Amihud 非流动性的 20 日波动 | 流动性 |
| `amount_20d`（已有） | 20 日日均成交额 | 容量约束 |

**残差动量实现要点**：
- 市场收益用池内等权收益代理；行业收益用 `dim_sector_member` 分组的等权收益代理（无需新增数据源）。
- 滚动 2 元回归用滚动协方差矩阵求解，避免逐日 `statsmodels` 调用；`min_periods=120`。
- 残差动量 = 回归窗口末端（t−21）往前 126/252 日残差之和 ÷ 残差标准差（信息比），严格只用 t 及以前数据。

### 4.2 v5 基本面 / 事件因子（`features/fundamental_v5.py`）

| 因子 | 口径 / 数据来源 | 服务策略 |
|---|---|---|
| `ep_ttm`（已有） | 净利润 TTM / 总市值 | 价值 |
| `bp_mrq`（已有） | 净资产 / 总市值 | 价值 |
| `cfo_yield` | 经营现金流 TTM / 总市值（新增） | 价值 |
| `fcf_yield`（已有） | 每股自由现金流 / 收盘价 | 价值 |
| `div_yield_3y`（已有） | 近 3 年现金分红 / 收盘价 | 价值 |
| `size_mcap` | 收盘 × 股本（用于市值分组与中性化） | 全部 |
| `roa_ttm` | 总资产报酬率（新浪「总资产报酬率」） | 质量 |
| `roic` | 投入资本回报率（新浪「投入资本回报率」） | 质量 |
| `gross_margin` / `net_margin` | 新增入库 | 质量 |
| `cfo_to_np` / `cfo_to_np_std_8q`（已有） | 经营现金流 / 净利润 | 质量 |
| `accruals` | (净利润 − 经营现金流) / 总资产 | 质量 |
| `total_assets` | 总资产（本期接入资产负债表，取「资产总计」） | 质量 / 投资 |
| `asset_growth` | 总资产同比 | 保守投资 |
| `asset_turnover_change` | 总资产周转率同比变化 | 保守投资 |
| `capex_growth` | 资本开支同比（现金流量表「购建固定资产、无形资产和其他长期资产支付的现金」） | 保守投资 |
| `capex_to_assets` | 资本开支 / 总资产 | 保守投资 |
| `investment_intensity` | (资本开支 + 资产增长) / 营收增长 | 保守投资 |
| `sue` | 单季净利润同比惊喜 ÷ 过去 8 季惊喜标准差 | PEAD |
| `revenue_surprise` | 单季营收同比惊喜 | PEAD |
| `announce_ret_1d` | 公告日后首个交易日异常收益（相对等权市场） | PEAD |
| `attention_20` | 公告后 20 日换手/成交额相对自身中位数 | PEAD |

**数据来源与处理**：
1. 新浪财务摘要已确认可直接取到「总资产报酬率、投入资本回报率、总资产周转率、商誉、营业成本、息税前利润」等，`ingest/fundamentals.py::METRIC_MAP` 增补即可。
2. **本期接入现金流量表与资产负债表**（`ingest/statements.py`）：CAPEX、折旧、资产总计、有息负债；沿用报告期 + 公告日，`fact_financial` 增加 `capex / depreciation / total_assets / interest_debt` 指标。
3. **EV/EBITDA 仍暂不做**：虽有折旧与有息负债，但 EBITDA 口径需统一（是否含少数股东、是否加回摊销），先保证 EP/BP/CFO/FCF 口径稳定，后续再评估。
4. PEAD 使用**SUE（季节随机游走）**，不依赖分析师一致预期；公告日一律用 `fact_financial.pub_date`，缺失时用法定披露截止日兜底（已有逻辑）。
5. 所有财务因子继续按 `pub_date <= t` 做 point-in-time 对齐，新增单测防未来函数。

### 4.3 因子版本策略

| 版本 | 内容 | 说明 |
|---|---|---|
| v2 | 现有 19 个技术/资金流 | 不动，保证现有回测可复现 |
| v3 | 现有 12 个基本面 | 不动 |
| v4 | 4.1 新增技术/交易 | 新策略使用 |
| v5 | 4.2 新增基本面/事件 | 新策略使用 |

---

## 5. 策略库（10 类，本期交付 5 类）

统一接口：

```python
@dataclass
class StrategySpec:
    id: str                 # value_quality
    name: str               # 价值质量
    category: str           # fundamental / price / event / meta
    description: str
    factors: list[str]      # 依赖因子（用于检查数据就绪）
    defaults: dict          # top_k / rebalance / weighting / params
    build_score: Callable[[Panel, dict], pd.DataFrame]
```

| 优先级 | 策略 id | 名称 | 核心实现 |
|---|---|---|---|
| P0 | `value_quality` | 价值 + 质量 | `z(ep)+z(bp)+z(cfo_yield)+z(fcf_yield)+z(div_yield)` 行业中性后与 `z(roe)+z(roa)+z(roic)+z(cfo_to_np)−z(accruals)` 合成；剔除亏损（EP≤0）与高应计 |
| P0 | `residual_momentum` | 残差动量 | `resid_mom_252` 与 `resid_mom_126` 等权；剔除最近 21 日；行业中性 |
| P0 | `factor_momentum` | 因子动量 / 择时 | 候选策略近 3/6/12 个月夏普加权 → 动态权重 → 加权各策略横截面 z 分数 → TopK |
| P1 | `conservative_investment` | 保守投资 | `−z(asset_growth) − z(capex_to_assets) + z(asset_turnover_change)`；行业中性 |
| P1 | `pead` | 财报超预期漂移 | 公告后 `sue`、`revenue_surprise`、`announce_ret_1d` 合成；持有 60 个交易日或至下次公告 |
| P2 | `short_reversal` | 短期反转 | `−z(resid_ret_5/10)` + 企稳确认（`lower_shadow_20`、`volume_shrink`、RSI 回升）；高成本敏感性单独报告 |
| P2 | `trend_following` | 时间序列动量 | `ts_trend_60/120` + `ret_60/120`；叠加市场趋势过滤 |
| P2 | `liquidity_premium` | 流动性溢价 | `z(amihud_20)` 作为 Alpha；同时用 `amount_20d` 做容量上限（单票不超过 20 日成交额的 1%） |
| P1 | `low_vol_multidim` | 多维低波动 | `−z(vol_120) − z(downside_vol_120) − z(ivol_60)`；与现有 defensive 对比 |
| — | `defensive`（现有） | 防御增强 | 迁移为注册表适配器，生产默认不变 |

> P0/P1 为本期交付；P2 为 Phase 6 后续批次，不在本期验收范围内。

**组合与中性化约定**：
- 横截面先做 1%/99% 缩尾，再 z-score；行业中性 = 行业内去均值（优先 `dim_stock_industry.level1`，缺失回退 `dim_sector_member`）。
- 价值因子按市值分组（小市值侧重 BP，大市值侧重 EP），权重在策略参数中可配。
- 缺失值不填 0，直接不参与排名；覆盖不足的股票不进入候选。

---

## 6. 回测与研究引擎

### 6.1 统一编排（`research/runner.py`）

输入：`strategy_id / universe / start / end / oos_start / cost_model / limit_model / top_k / rebalance / weighting`。

输出（同时返回并落库）：
- 全样本与样本外净值、基准（沪深300/中证500/中证1000 + 池等权）；
- 指标：CAGR、MaxDD、Sharpe、Calmar、年化超额、年化换手、成本占比、胜率；
- 分年度、分市场状态、分市值组表现；
- IC / RankIC / ICIR（因子层）；
- 最差 5 笔交易、最大连续亏损；
- 数据完整度与参数快照（可复现）。

### 6.2 样本外与 walk-forward

- 默认切分：`2018-01 ~ 2023-12` 样本内、`2024-01 ~ 最新` 样本外；页面可改。
- 滚动验证复用 `experiment.run_walkforward`：训练 3 年 → 测试 1 年 → 前滚；只用于参数稳定性检查，不做全样本寻优。
- 任何参数调整必须记录到 `params_json`，页面显示「样本内 / 样本外」两列。

### 6.3 A 股交易约束（沿用并补强）

- 信号 t 日收盘生成，t+1 收盘成交（现有引擎已按 `score.shift(1)`）。
- 涨跌停：`_limit_threshold(board)`；停牌/ST/上市不足 60 日：`eligibility_panel`。
- 成本：佣金 + 过户费 + 印花税 + 滑点（`_a_share_rates`）。
- 新增**容量约束**：单票目标金额 ≤ 近 20 日日均成交额 × `capacity_pct`（默认 1%）；超限降权或跳过。
- 基准：新增 `fact_index_quote`（沪深300/中证500/中证1000），避免每次回测实时请求腾讯接口。

### 6.4 评分卡（`research/scorecard.py`）

将指标归一化为 0–100 分，仅用于**研究排序**，不构成投资建议：

```
score = 25*rank(CAGR) + 20*rank(Sharpe) + 15*rank(Calmar)
      + 15*rank(-MaxDD) + 10*rank(超额) + 10*rank(ICIR)
      +  5*rank(OOS 一致性)
```

同时输出「不通过」原因（如 OOS 年化为负、换手过高、容量不足、IC 方向不稳定）。

### 6.5 全 A 研究计算方案（关键）

| 环节 | 方案 |
|---|---|
| 行情 | `fact_quote` 存全 A 日线；读取时按回测区间与股票池裁剪，`float32` |
| 研究因子 | **不落 SQLite 日频**：`features/cache.py` 按（版本、股票池哈希、日期）生成 Parquet 面板，命中即复用；参数/版本变更自动失效 |
| 池因子 | C0~C3 维持每日写入 `fact_factor`，保证池详情/热力图/信号/纸面账户不变 |
| 候选股票压缩 | 每日先按可交易性与流动性过滤（ST/停牌/上市 <250 日/20 日均额 < 2,000 万），再做横截面排名；通常保留 2,000–3,500 只 |
| 因子计算 | 技术因子按股票分组滚动；残差动量用滚动协方差向量化；财务因子按 pub_date 对齐 |
| 回测 | 只保留 `close/ret/pct + 策略依赖因子` 矩阵；不加载全部因子 |
| 缓存清理 | 保留最近 90 天；版本升级时清除旧版本目录 |
| 首轮回填 | 分两批：中证 800 + 创业板/科创板 → 其余；单票 watermark，失败重试 3 次后记录缺口 |

> 目标：全 A 单策略回测在可接受时间内完成；研究页给出「数据覆盖度 / 缓存命中率 / 过滤后股票数」提示，避免误读。

---

## 7. 数据模型

### 7.1 新增表

```sql
-- 策略注册表（与代码 registry 同步）
CREATE TABLE IF NOT EXISTS dim_strategy (
    strategy_id   TEXT PRIMARY KEY,
    name          TEXT,
    category      TEXT,          -- fundamental / price / event / meta
    description   TEXT,
    spec_json     TEXT,          -- 默认参数、依赖因子
    status        TEXT,          -- research / candidate / production
    created_at    TEXT,
    updated_at    TEXT
);

-- 策略回测记录（一次运行一行）
CREATE TABLE IF NOT EXISTS fact_strategy_run (
    run_id        TEXT PRIMARY KEY,
    strategy_id   TEXT,
    universe_id   TEXT,
    start_date    TEXT,
    end_date      TEXT,
    oos_start     TEXT,
    params_json   TEXT,
    metrics_json  TEXT,          -- 全样本 + OOS + 分年度 + 分状态
    score         REAL,
    created_at    TEXT
);

-- 因子 IC 快照
CREATE TABLE IF NOT EXISTS fact_factor_ic (
    factor      TEXT,
    universe_id TEXT,
    trade_date  TEXT,
    horizon     INTEGER,         -- 1 / 5 / 20
    ic          REAL,
    rank_ic     REAL,
    n           INTEGER,
    PRIMARY KEY (factor, universe_id, trade_date, horizon)
);

-- 市场状态
CREATE TABLE IF NOT EXISTS fact_regime_state (
    trade_date   TEXT PRIMARY KEY,
    trend        TEXT,           -- up / range / down
    vol_state    TEXT,           -- low / mid / high
    breadth      REAL,           -- 站上 MA20 的股票占比
    amount_chg   REAL,           -- 全市场成交额 20 日变化
    style        TEXT            -- value / growth / small / large（按近期因子收益）
);

-- 因子动量权重
CREATE TABLE IF NOT EXISTS fact_strategy_weight (
    trade_date   TEXT,
    strategy_id  TEXT,
    weight       REAL,
    reason       TEXT,           -- 近期夏普/波动/得分
    PRIMARY KEY (trade_date, strategy_id)
);

-- 指数行情（回测基准与残差回归的市场收益）
CREATE TABLE IF NOT EXISTS fact_index_quote (
    index_code  TEXT,
    trade_date  TEXT,
    close       REAL,
    pct_chg     REAL,
    PRIMARY KEY (index_code, trade_date)
);

-- 财报事件（PEAD 审计用）
CREATE TABLE IF NOT EXISTS fact_earnings_event (
    symbol          TEXT,
    stat_date       TEXT,
    pub_date        TEXT,
    sue             REAL,
    revenue_surprise REAL,
    announce_ret_1d REAL,
    PRIMARY KEY (symbol, stat_date)
);

-- 全 A 行业分类（中性化与行业对比）
CREATE TABLE IF NOT EXISTS dim_stock_industry (
    symbol      TEXT PRIMARY KEY,
    level1      TEXT,
    level2      TEXT,
    src         TEXT,
    updated_at  TEXT
);
```

### 7.2 变更已有表

- `dim_universe` 增加 `strategy_id TEXT NOT NULL DEFAULT 'defensive'`（迁移时全部池设为 `defensive`，保证行为不变）。
- 新增内置研究池 **`A0 全 A`**（`is_custom=0`、`daily_enabled=0`）：不参与每日池任务，只用于研究回测；候选股按可交易性 + 流动性动态过滤（见 6.5）。
- `backtest_run` 增加 `metrics_json TEXT`（历史兼容，新研究统一写 `fact_strategy_run`）。
- 新增索引：`fact_quote(trade_date, symbol)`、`fact_factor_ic(factor, trade_date)`、`fact_strategy_run(strategy_id, universe_id, created_at)`。

### 7.3 历史成分（必做）

`dim_universe_member_history(universe_id, trade_date, symbol, sector_code)`：每月首个交易日记录一次池成员快照（含 A0 的有效候选集），从现在开始积累，用于未来降低幸存者偏差。历史部分无法追溯，页面需明确标注。

---

## 8. API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/research/strategies` | 策略库：注册表 + 最近一次各池指标 + 状态 |
| GET | `/api/research/strategies/{id}` | 策略详情：参数、依赖因子、最近运行、净值、分年/分状态、IC |
| POST | `/api/research/strategies/{id}/backtest` | `{universe, start, end, oos_start, top_k, rebalance, save}` |
| POST | `/api/research/compare` | `{strategy_ids, universe, start, end}` → 对比表 + 多净值 |
| GET | `/api/research/factors` | 因子元数据 + 覆盖率 + 最新 IC |
| GET | `/api/research/factors/{name}/ic` | `{universe, horizon}` → IC 序列 + 分组收益 |
| GET | `/api/research/regime` | 当前与历史市场状态 |
| GET | `/api/research/meta/weights` | 因子动量当期权重与历史 |
| POST | `/api/research/meta/run` | 重新计算因子动量权重（月度或手动） |
| POST | `/api/pools/{id}/strategy` | 绑定/切换池策略（写 `dim_universe.strategy_id`，触发池任务） |
| POST | `/api/nl/config` | 自然语言 → 因子配置（新增模式） |

所有接口沿用现有错误映射：参数非法 400、LLM 不可用 503、任务冲突 409。

---

## 9. 界面布局

### 9.1 左侧导航（保持现有结构，只扩展「研究」组）

```
概览
  · 总览
股票池
  · C0 / C1 / C2 / C3 / 自定义池…
研究
  · 策略研究   ← 研究 Hub（#/research）
操作
  · 新建股票池
```

- `A0 全 A` 不进入左侧股票池列表，只在研究页的「样本域」下拉中出现，避免与生产池混淆。
- `#/nl` 保留并重定向到 `#/research?tab=nl`，旧链接不失效。

### 9.2 研究 Hub（`#/research`）

顶部固定 Tab（移动端横向滚动）：

```
[策略库] [策略对比] [因子实验室] [因子择时] [自然语言]
```

Tab 右上角常驻**样本域选择**：`A0 全 A（默认） / C0 / C1 / C2 / C3 / 自定义池`，切换后各 Tab 同步；选择 A0 时显示「流动性过滤后股票数 / 数据覆盖度」。

#### Tab 1 策略库

- 顶部：类别筛选（全部 / 基本面 / 价格行为 / 事件 / 复合）、状态筛选（研究 / 候选 / 生产）、搜索。
- 卡片网格（每行 2–3 张）：
  - 名称 + 类别 chip + 状态 chip；
  - 一行 KPI：年化 / 夏普 / 最大回撤 / 最近 IC；
  - 迷你净值 sparkline；
  - 操作：`回测`、`详情`、`加入对比`、`设为池策略`（需已回测）。
- 底部：**策略排行榜**（按评分卡排序，含 OOS 标记；红色提示「样本外跑输基准」）。
- 「新建策略」入口：跳转自然语言 Tab；因子配置模式生成后自动入库为 `research` 状态。

#### Tab 2 策略对比

- 多选策略（至少 2 个）+ 选择池 + 区间 + 样本外起点。
- 净值对比图（策略 vs 基准）、回撤曲线。
- 指标表：CAGR / MaxDD / Sharpe / Calmar / 超额 / 换手 / IC / ICIR / OOS 一致性。
- 相关性矩阵（策略间日收益相关），提示「高相关策略可合并」。
- 操作：导出 CSV（研究记录）。

#### Tab 3 因子实验室

- 左侧因子列表（按类别分组，显示覆盖率）。
- 右侧：
  - IC / RankIC 时序与累计曲线；
  - 10 分组年化收益柱状图（多空仅作研究展示）；
  - 分年度 IC 热力表；
  - 因子相关性热力图（与现有池详情热力图同风格）；
  - 「按市场状态拆分」切换（上行/震荡/下行）。

#### Tab 4 因子择时

- 当期权重：横向条形图（Value / Quality / LowVol / ResidualMom / Reversal / PEAD…），标注权重来源（3/6/12 月夏普）。
- 权重历史：堆叠面积图（每月一行）。
- 候选策略近期表现表：3M / 6M / 12M 收益、夏普、波动、是否进入组合。
- 说明区：明确「历史表现不代表未来；权重由规则计算，不含主观判断」。

#### Tab 5 自然语言

- 两种模式切换：
  1. **规则模式**（现有）：连阴、MACD 金叉等，保留现有交互与结果。
  2. **因子配置模式**（新增）：输入如「低波动 25%、价值 25%、质量 30%、保守投资 20%，每月调仓，持有 10 只」，LLM 输出配置 JSON → 页面展示因子权重条 + 池 + 调仓频率 → 运行「回测 + 样本外」。
- 配置 JSON 可手动编辑（白名单校验），不执行代码。
- 保存后进入策略库（状态 `research`）。

### 9.3 策略详情（`#/research/{id}`）

顶栏：名称、类别、状态、数据区间、`运行回测`、`设为池策略`。

分区（可折叠，默认展开「绩效」）：
1. **规则与参数**：因子权重、方向、中性化、TopK、调仓、成本、容量。
2. **绩效**：KPI、净值 vs 基准、回撤。
3. **分年度 / 分状态**：表格 + 热力图。
4. **因子与 IC**：IC/RankIC/ICIR、因子相关性。
5. **持仓与交易**：最新目标组合、最近 50 笔交易、最差 5 笔。
6. **稳健性**：样本内 vs 样本外、walk-forward 分段结果、容量提示。

### 9.4 股票池详情页改造

在「当前目标组合」卡片上方新增一行**策略配置条**：

```
当前策略：防御增强（defensive）  [更换策略]
本池回测：年化 x% · 夏普 x · 截至 2026-11-02
```

`更换策略` 打开弹窗：策略库列表（带该池最近回测指标）+ 搜索；确认后：
- 写 `dim_universe.strategy_id`；
- 触发池任务（回测 → 目标组合 → 跟踪 → 模拟账户）；
- 页面轮询任务状态，完成后刷新。

### 9.5 移动端

- 研究 Hub 的 Tab 横向滚动，卡片单列；
- 策略详情的 6 个分区默认折叠为手风琴；
- 对比页指标表横向滚动，净值图高度压缩。

---

## 10. 自然语言升级

### 10.1 两阶段输出

```
中文描述 → LLM → 策略配置 JSON（白名单） → 校验/归一化 → 确定性回测 → 样本外 → 评分 → 入库
```

### 10.2 配置 Schema（新增）

```json
{
  "strategy_name": "A股防御型价值质量策略",
  "mode": "factor",
  "universe": {"id": "A0", "exclude_st": true, "min_listing_days": 250, "min_amount_20d": 20000000},
  "factors": [
    {"name": "low_volatility", "weight": 0.25, "direction": "ascending", "lookback_days": 120},
    {"name": "value", "weight": 0.25, "direction": "descending", "metrics": ["EP", "FCF_YIELD"]},
    {"name": "quality", "weight": 0.30, "direction": "descending", "metrics": ["ROIC", "ROA", "CFO_TO_NP"]},
    {"name": "conservative_investment", "weight": 0.20, "direction": "ascending", "metrics": ["ASSET_GROWTH"]}
  ],
  "portfolio": {"stock_count": 10, "rebalance": "monthly", "weighting": "equal", "max_sector": 0.3},
  "backtest": {"start_date": "2018-01-01", "execution": "next_tradable_day",
               "include_transaction_costs": true, "out_of_sample": true}
}
```

校验规则：
- `universe.id` ∈ `A0 / C0 / C1 / C2 / C3 / 自定义池`；
- `factors[].name` 必须在因子注册表白名单内；权重和归一化为 1；单因子权重上限 0.4；
- `stock_count` 1–30；`rebalance` ∈ daily/weekly/monthly；
- 未知字段忽略并提示，不允许执行脚本；
- 校验失败返回 400 并列出可选项。

### 10.3 规则模式兼容

现有 `entry.all` DSL 继续可用；`mode=rule` 与 `mode=factor` 共用回测引擎与结果展示，保存到 `dim_strategy`（规则策略也注册为策略，便于对比）。

---

## 11. 调度与任务

| 频率 | 任务 |
|---|---|
| 每日 16:10 | 全 A 行情增量 → 池成员 v2/v3/v4/v5 因子 → 各池按 `dim_universe.strategy_id` 回测 → 信号 → 目标组合 → 跟踪 → 模拟账户 |
| 每周五 | 全 A 研究因子快照（Parquet 缓存）→ 因子 IC 快照（A0 + 各池 × 因子 × horizon 1/5/20）→ 市场状态表 |
| 每月首个交易日 | 因子动量权重重算 → meta 策略调仓 → 策略排行榜刷新 → 成员快照入库 |
| 手动 | 研究页「回测 / 对比 / 重新计算权重」；CLI；全 A 行情/财务回填 |

并发：沿用 `data/pool_job.lock`；研究批量回测与每日池任务互斥，冲突返回 409。

计算预算控制：
- 生产池任务只算池成员；研究回测按需构建全 A 面板并复用 Parquet 缓存；
- 全 A 候选先做流动性过滤（20 日均额 ≥ 2,000 万、非 ST/停牌、上市 ≥ 250 日），再做排名；
- 因子面板一次加载、多策略复用（`features/panel.py`），矩阵用 `float32`；
- 因子动量只对「候选 + 生产」状态策略计算，避免全量重跑；
- 回填任务在夜间执行、可中断，完成后记录覆盖度。

---

## 12. 测试与验收

### 12.1 单元测试

- 因子正确性：`vol_120`、`downside_vol`、`beta`、`resid_mom` 在合成序列上的已知值。
- 无未来函数：截断面板后重算，最后一行因子值与全量计算一致（v4/v5 全部因子）。
- PEAD 时点：公告日前后收益只使用 pub_date 之后的数据。
- 残差动量：构造 `R_i = 2*R_mkt + 噪声`，残差均值接近 0，β 接近 2。
- 因子动量权重：给定 3 个策略的月度收益，权重符合规则（上限于 0.4、和为 1、全负时回撤到防御）。
- 容量约束：单票金额超过 20 日成交额 1% 时被削减/跳过。
- 注册表：重复 id 报错、缺失因子时策略不可运行。

### 12.2 集成测试

- 合成 SQLite（复用 `tests/test_pool_admin.py` 的临时库模式）+ 300 日行情：
  - 每个策略跑通并返回完整结构；
  - 样本内/样本外切分正确；
  - `fact_strategy_run`、`fact_strategy_ic` 落库幂等。
- API：策略列表/详情/回测/对比/权重绑定/删除。
- 池策略切换：切换后 `pool_jobs` 使用新策略名回测，旧结果不覆盖。

### 12.3 验收标准

1. `pytest` 全绿，新增测试覆盖每个新因子与策略。
2. 全 A 行情覆盖率 ≥ 95%（当前可见股票），A0 研究回测可运行。
3. 首批 5 类策略全部可在页面运行，输出全样本 + 样本外指标。
4. 每个策略可在池详情绑定，日更后目标组合/跟踪/模拟账户随之更新。
5. 因子实验室能展示 IC、分组收益、相关性。
6. 因子择时页展示当期与历史权重。
7. NL 两种模式均可生成配置并回测，且配置可审计、可复现。
8. 所有结果注明「历史研究，不构成投资建议」。

---

## 13. 实施阶段

> 按依赖顺序推进，每阶段结束都可独立验收、不破坏现有生产行为。

### Phase 0A 数据底座：全 A + 财务报表

- `ingest/full_market.py`：全 A 股票列表、行情分批回填（单票 watermark、失败重试、断点续跑、覆盖度统计）；
- `ingest/industry.py`：申万一级/二级行业分类 → `dim_stock_industry`；
- 指数行情（沪深300/中证500/中证1000）→ `fact_index_quote`；
- `ingest/statements.py`：现金流量表（CAPEX、折旧）+ 资产负债表（资产总计、有息负债）→ `fact_financial` 新增指标；
- 数据库索引、备份策略（每周）与库体积监控。

**灰度与回滚**：全 A 回填只做增量写入，不改动现有 C0~C3 数据路径；回填期间 A0 显示覆盖率不足并禁止正式对比；迁移前做一次完整备份，异常时可回滚到旧库。

**验收**：2018 年至今全 A 行情覆盖率 ≥ 95%（当前可见股票）；财务指标覆盖 ≥ 90%；回填可中断续跑；A0 研究池可用。

### Phase 0B 基础设施

- `features/registry.py`、`features/panel.py`、`features/cache.py`；
- `strategy/registry.py` + `defensive` 适配器；
- `dim_strategy` / `fact_strategy_run` / `fact_factor_ic` / `fact_regime_state` 建表与迁移；
- `research/runner.py` 统一编排（先接现有 defensive）；
- `dim_universe.strategy_id` 默认 `defensive`；内置 `A0 全 A` 研究池。

**验收**：现有 C0~C3 回测结果与升级前一致；注册表可列出策略；A0 回测可运行；`pytest` 通过。

### Phase 1 技术因子 v4 + 因子实验室

- 波动/Beta/特质波动/残差动量/趋势/反转/容量因子；
- `research/ic.py` + `fact_factor_ic`；
- 前端「因子实验室」Tab（IC、分组收益、相关性）。

**验收**：v4 因子无未来函数测试通过；IC 页可切换因子/样本域/持有期。

### Phase 2 基本面因子 v5 + PEAD 事件

- 扩展 `METRIC_MAP`（ROA/ROIC/周转率/商誉等）并回填；
- 应计、资产增长、CAPEX 增长、SUE、公告日收益；
- `fact_earnings_event` + PIT 测试。

**验收**：财务因子覆盖 ≥ 90%；PEAD 事件日期与 `pub_date` 对齐；CAPEX 口径抽查通过。

### Phase 3 策略库（首批 5 个）

- `value_quality`、`residual_momentum`、`conservative_investment`、`pead`、`factor_momentum`；
- 统一评分卡、样本外切分、walk-forward；
- CLI：`cli research backtest --strategy …`、`cli research compare …`、`cli research meta`。

**验收**：5 个策略在 A0 与 C0~C3 跑通；输出全样本 + OOS；排行榜按评分排序。

### Phase 4 研究页面

- 研究 Hub 五个 Tab、策略详情、对比、池策略切换弹窗；
- 移动端适配；
- 旧 `#/nl` 重定向。

**验收**：浏览器实测完整流程（选策略 → 回测 → 对比 → 绑定到池 → 池详情刷新）。

### Phase 5 自然语言因子配置

- 配置 Schema、白名单校验、两种模式；
- 生成后入库并跳转策略详情。

**验收**：示例描述可生成 4 因子配置并跑通样本外；手动编辑 JSON 非法时给出明确错误。

### Phase 6 后续扩展（本期不强制）

- 第二批策略：`short_reversal`、`trend_following`、`liquidity_premium`、`low_vol_multidim`；
- 换手惩罚、最差交易报告、成本/容量敏感性报告；
- 历史成分数据源接入（进一步降低幸存者偏差）。

**验收**：按需逐项验收，不影响前五阶段交付。

---

## 14. 风险与限制

1. **幸存者偏差**：即使扩到全 A，也只能取到「当前可见」股票列表，历史区间未包含已退市/被调出的股票。缓解：每月记录成员快照（面向未来）；页面明示；Phase 6 评估接入历史成分/退市股数据。
2. **财务数据修订**：`fact_financial` 为当前可见值，历史修订只能从 `fact_financial_revision` 建立后开始记录；已用 `pub_date` 控制可见时点，但无法还原历史重述。缓解：关键结论用 OOS 与多口径交叉验证。
3. **全 A 数据量**：行情约 1,100 万行、财务回填请求量大；首轮回填受数据源限流影响。缓解：分批 + 断点续跑 + 夜间执行；研究因子用 Parquet 缓存，不写入 SQLite 日频。
4. **行业分类质量**：申万分类与历史变更无法完全还原；中性化结果依赖分类准确性。缓解：优先使用申万一级，缺失回退 THS；行业中性化前后结果对比展示。
5. **成本与容量**：短期反转、低流动性策略对成本/冲击极敏感，必须报告扣费后结果与容量上限，不能只看多空学术收益。
6. **过拟合**：禁止全样本调参；所有参数变更记录在 `params_json`，页面同时展示样本内/样本外。
7. **数据源稳定性**：新浪/腾讯/同花顺/巨潮接口可能限流或变更；行情、财务、指数入库均需可重试、可断点续跑。
8. **计算资源**：全 A 面板矩阵较大，需控制并发与内存；研究任务与每日生产任务互斥，避免 SQLite 写锁竞争。

---

## 15. 决策记录与后续可选项

### 15.1 已确认（2026-11-04）

| 决策项 | 结论 |
|---|---|
| 首批策略 | 第一批 5 个 |
| 样本域 | 全 A（A0 研究池 + 现有 C0~C3） |
| 界面 | 研究 Hub + 顶部 Tab |
| 财务数据 | 本期接入现金流量表/资产负债表 |

### 15.2 后续可选项（本期不做，记录备查）

- EV/EBITDA 估值口径；
- 分析师一致预期与真正的「超预期」；
- 历史时点成分与退市股补全；
- 第二批策略与成本/容量敏感性报告；
- 多策略资金分配（风险预算、目标波动）在实盘账户层面的落地。

> 接下来按 Phase 0A → Phase 5 顺序实施；每阶段结束提交可运行版本与验收记录。
