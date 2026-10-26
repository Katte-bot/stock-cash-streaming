# 任务看板（TODO）

> 用法：按里程碑推进；开工前把任务移到 **进行中**，完成后移入 **已完成** 并注明日期。
> 关联：[需求分析说明书](./requirements-specification.md) · [软件架构说明书](./software-architecture.md) · [PROJECT_MEMORY.md](./PROJECT_MEMORY.md)

## 图例
`[ ]` 待办 · `[~]` 进行中 · `[x]` 完成 · `[!]` 阻塞

---

## M0 环境与文档
- [x] 创建 `.venv` 虚拟环境（2026-10-14）
- [x] 编写《需求分析说明书》（2026-10-14）
- [x] 编写《软件架构说明书》（2026-10-14）
- [x] 建立 memory / TODO 文档骨架（2026-10-14）
- [x] 编写并安装 `requirements.txt` 核心依赖（akshare/loguru/pydantic-settings/pyyaml）
- [x] 初始化 `src/stock_cash_streaming` 包骨架与 `cli.py`（2026-10-14）
- [x] 建立 `.env.example`；LLM 配置改用**系统环境变量**（2026-10-14）
- [x] LLM 连通性验证通过（`deepseek-flash` 返回 `pong`，2026-10-14）
- [x] 建立 `config/sectors.yaml`（电网设备、天然气）

## M1 数据底座
- [x] `datasource/base.py` 定义 `DataSource` 接口
- [x] `datasource/akshare_src.py` AkShare 适配器（含重试/限速/超时）
- [x] `datasource/mcp_src.py` MCP 适配器（预留，待端点契约）
- [x] `datasource/registry.py` 数据源选择
- [x] `storage/schema.sql` 建表 DDL（12 表 + 索引）
- [x] `storage/db.py` / `repository.py` upsert/查询/水位
- [x] `ingest/universe.py` 目标板块与成分股
- [x] `ingest/history.py` 历史行情与资金流初始化（含逐股容错）
- [x] `cli.py` init/status + `seed`；存储层已实测通过
- [x] 取数路径：**腾讯(行情/快照) + 同花顺(板块资金流)** 实测可用（2026-10-14）
- [x] `ingest/mcp_loader.py` + `config/seed_members.csv`；MCP 种子已入库
- [x] **真实历史入库：7 股 × 2128 日 = 14851 行行情**（2018→2026）；板块资金流 2 条
- [x] **完整板块成分**：新浪「智能电网」38 只 + 「燃气生产和供应业」33 只
- [x] 个股资金流长历史：新浪分页回补 237,575 行（2010→2026）

## M2 因子与信号
- [x] `features/engine.py` 因子引擎（技术 12 + 资金流 4 = 16 个，v2 写入 `fact_factor`）
- [x] 技术因子：ret_5 / ret_20 / ma5_ratio / ma20_ratio / boll_pos / rsi14 / macd_hist / vol_20 / turnover_5 / ma60_ratio / amount_20d / max_ret_20 / turnover_std_20 / amihud_20
- [x] 资金流因子：main_net_5d / main_net_ratio_5d / inflow_days_5 / institution_trend
- [x] 板块内综合评分 + 分级 + 理由（`strategy/signal.py`）
- [x] 实测：因子 133019 行、信号输出正常（2026-10-14）
- [x] 无未来函数测试（`tests/test_no_lookahead.py`，2 passed）
- [ ] 尾盘资金流入强度（需盘中/分钟数据）
- [ ] 行业/市值中性化（待成分与市值数据完备）
- [ ] 拆分独立的 `intra_sector.py` / `persistence.py`（当前合并在 signal.py）

## M3 回测
- [x] `backtest/engine.py` 日频再平衡回测（T+1、涨跌停、换手成本、1 日滞后）
- [x] `backtest/metrics.py` 指标 + 因子 RankIC（免 scipy）
- [x] CLI `backtest` / `ic`；因子 IC 已跑通
- [x] 数据问题定位与修复：腾讯 qfq 负价 → 改用 **hfq** + 过滤 `close>0`
- [x] 重拉行情 → 重算因子 → 重跑回测
- [x] 数据源攻坚：新浪成分（电网设备 38 / 天然气 33）+ 新浪资金流长历史（237,575 行，2010→2026）
- [x] 基准修正为「等权买入持有（零成本）」，当前基准年化 3.68%
- [x] 全量样本回测（71 只）：top_k=3/weekly/buffer=1 → 年化 0.58%、夏普 0.18、回撤 -70.17%
- [x] 策略重构：`flow_rules.py`（me.md 规则）+ 市场择时（沪深300 MA200）
- [x] 回测对比：composite 0.58%/-70.17%；composite+择时 -2.38%/-54.82%；flow_rules -7.47%~-19.79%
- [x] 结论：均跑输等权买入持有（3.68%），择时只降回撤
- [x] **因子分年度/分 regime IC 与分层回测**（2026-10-19，15 因子 IC 全负且逐年一致）
- [x] 样本清洗：剔除 ST / 低流动性（20日成交额<1亿）/ 上市不足 60 日标的
- [x] 策略实验：反向因子 / 彩票效应 / 非流动性溢价 / 板块中性 / 加权方式 / 月频 / 波动率目标
- [x] 样本内(2018–2022) / 样本外(2023–2026) 双段评估与选优
- [x] 最终策略 `defensive` 落库（年化 8.26%、夏普 0.48、回撤 -33.59%、超额 +4.58%）
- [x] 研究结论：`docs/research_strategy_conclusion.md`
- [ ] 920010（北交所）等个别标的补数
- [ ] 可选：拆分 `backtest/cost.py` / `report.py`
- [x] 回测基准用例回归（`tests/test_backtest_regression.py`，4 项；全量 6 passed）
- [x] 因子衰减监控（`cli monitor --stage ic`，滚动 RankIC + 告警，2026-10-25）

## M4 服务与前端
- [x] `service/app.py` FastAPI 装配 + 静态资源（ECharts 本地化，离线可渲染）
- [x] REST：`/api/health`、`/api/overview`、`/api/stock/{symbol}`、`/api/status`
- [x] 单页仪表盘：KPI / 板块资金流 / 个股横向对比 / 信号清单 / 回测净值
- [x] 浏览器验证：3 个 ECharts、表格 7 行、无控制台报错（2026-10-19）
- [x] 个股详情下钻页（弹窗，含资金流柱状图 + 因子时序）
- [x] 因子热力图（Z-Score）
- [x] AI 日报页（DeepSeek）
- [x] 明暗主题切换

## M5 增量与实时
- [x] `ingest/incremental.py` 每日增量（幂等；实测 71 只成分，行情 67 成功 / 4 失败）
- [x] CLI `incremental --lookback`
- [x] `predict/intraday.py` 盘中快照与板块内实时排名（`/api/intraday`）
- [x] WebSocket `/ws/realtime`（60s 推送；前端实时徽标已连通）
- [ ] `scheduler/jobs.py`（当前以 CLI + cron 触发）
- [x] 交易日历精细化（新浪交易日历，覆盖至 2026 年末，超出回退工作日）
- [ ] 实时预测与收盘信号偏差跟踪

## M6 AI 日报与打磨
- [x] `report/daily.py` 规则模板日报 + DeepSeek 调用与降级
- [x] 前端 AI 日报卡片（实测 deepseek-flash 生成成功）
- [x] 视觉打磨：明暗主题、个股下钻、因子热力图
- [x] 移动端适配：表格滚动容器 + 窄屏隐藏次要列、KPI 两列、热力图窄屏布局、安全区与 theme-color
- [ ] 日报归档与往期查询
- [ ] 测试补齐

## M7 目标组合与实盘跟踪
- [x] `strategy/portfolio.py`：目标组合 / 1/vol 权重 / 波动率目标 / 调仓清单（与回测同口径）
- [x] 新建 `fact_target_run` / `fact_target_portfolio` 表
- [x] CLI `portfolio`、API `GET /api/portfolio`
- [x] 仪表盘「当前目标组合」卡片（状态、持仓、权重条、调仓变化）
- [x] 模拟跟踪表：按调仓快照 + 真实收盘价计算净值、超额、回撤、跟踪误差
- [x] `fact_tracking_nav` 表、CLI `tracking`、API `GET /api/tracking`
- [x] 仪表盘「模拟跟踪」卡片（等待行情 / 跟踪中两种状态）
- [x] 每日自动流水线：incremental → factors → signals → portfolio → tracking（`cli daily`）
- [x] 常驻调度器：工作日 16:10 自动运行（`cli scheduler`），task_log 审计
- [x] 修复增量只覆盖 7 只种子股的问题（改为 DB 全部 71 只成分）
- [x] 修复增量窗口首日 `pct_chg` 被写成空值、导致 `vol_20/max_ret_20` 最新日为 NaN 的问题（用库内前收盘补齐）
- [x] 流水线首次真实运行：2026-10-20 `ok`，单次约 5 分钟（71 只增量 ~4.5min + 因子 ~1min），目标组合 12 只 / 仓位 100% / 篮子波动 14.88%
- [x] 中断恢复命令 `cli recover --lookback N`：补行情 → 重算因子 → 重放遗漏调仓快照 → 重放跟踪（含 `--dry-run` / `--no-ingest`）
- [x] 运维文档 `docs/operations-runbook.md`（日常检查、中断恢复、备份、常见问题）
- [x] 部署文档 `docs/deployment-guide.md`（云服务器迁移、systemd、Nginx、备份、验收）
- [x] 节假日交易日历（新浪日历，覆盖至 2026 年末；超出回退工作日）
- [x] 滚动 IC 衰减告警（`cli monitor`，2026-10-25）
- [x] 策略回撤告警（`cli monitor --stage drawdown`，已接入 `cli daily`）
- [x] 可交易性清洗（ST 当前值直接剔除；不做历史 ST 时点还原）

## M8 防御增强版研究
- [x] 股票池显式化：`dim_universe` / `dim_universe_member`，C0 板块成分 + C1 用户 CSV（2026-10-24）
- [x] 统一剔除规则：ST/退市名称、连续停牌≥60交易日、上市<60交易日（2026-10-24）
- [x] 时点财务/分红/上市退市数据入库（Baostock + 新浪，32,725 财务行 / 519 分红）（2026-10-24）
- [x] 基本面因子 v3：ROE、现金流质量、估值、股息可持续等 12 个（2026-10-24）
- [x] 回测可信度：分板块涨跌停、A股历史税费模型、单股/行业权重上限（2026-10-24）
- [x] 因子消融 A1-A7（含流动性组合并与行业中性化）（2026-10-24）
- [x] 组合构建 P1-P14、风险参数 R0-R6、滚动前推（2026-10-24）
- [x] 候选策略 `defensive_valuation`（原策略 + 8% 估值）接入 CLI（2026-10-24）
- [x] C1 用户清单补数（8 只，含 3 只 ETF；2026-10-25）
  - 行情 8/8、资金流 8/8、财务 5/5（ETF 自动跳过）；因子已重算
  - 修正 `infer_market`：深市 ETF（1 开头）归 sz
  - 结论：C1 池小（<TopK），评分方案无差异；季度调仓成本最低、样本外最佳
- [x] C2/C3 行业池（电力公用事业、水务、交通、通信）（2026-10-24）
  - C2 170 只 / C3 205 只；新增 `config/universes.yaml` 与 `--prune`
  - 数据完整性：全部成员行情最新到 2026-10-23；财务/分红/因子已补齐
  - 对照结论：C0 最优；C2 低波低回撤（滚动夏普 0.58 > C0 0.50，DD -20.7%）；C3 跑平基准，不采用
  - C2/C3 组合（P1-P14）、风险（R0-R6）、滚动前推均已跑完并归档
- [x] ST 处理口径确认（2026-10-25）：**不做历史 ST 时点还原**；当前名称含 ST/退 的标的从 C0/C1/C2/C3 解析时直接剔除（C0 由 71 → 66 只）
- [x] 财务修订版本回补（2026-10-25）
  - 新表 `fact_financial_revision`（append-only，version 递增）
  - 回补 99,641 条 v1，与 `fact_financial` 完全一致；重新同步 0 修订（幂等）
  - 数据源限制：无法还原真实历史修订链，仅保证后续修订可追溯
- [x] 因子滚动 IC 监控（`cli monitor`）
- [x] 收益归因（年度/行业/个股，`cli attribution`）
- [x] 参数敏感性与 Deflated Sharpe（`experiments --stage sensitivity`，DSR 0.846）
- [x] 交易成本敏感性（`experiments --stage cost`；30bp 仍有 7.3%）
- [x] 市场状态分组（`experiments --stage regime`；牛市跑输、震荡/下行有超额）
- [x] 策略变体（`experiments --stage variants`；V5 动态波动目标小幅改善，V3 平滑降成本）
- [x] 候选策略纸面跟踪机制（2026-10-25）
  - `portfolio` / `tracking` / API 支持 `--strategy defensive_valuation`，独立 strategy 名 `defensive_valuation_top12_monthly`
  - `cli daily` 每日同时落库生产与候选策略的组合/跟踪，候选含 v3 估值因子
  - 基线已建：2026-10-23，12 只、仓位 100%、篮子波动 16.24%
- [ ] 候选策略跟踪满 1–2 个季度后复盘

---

## 已完成
- [x] 2026-10-14 项目启动：环境调研、`.venv` 创建、两份说明书与追踪文档落地。

## 已确认（原阻塞项已关闭）
- [x] LLM：DeepSeek `deepseek-flash`（OpenAI 兼容）。系统环境变量 `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`LLM_MODEL` 已配置并**验证连通**。
- [x] 回测再平衡频率：**日频**。
- [x] 云端远程访问：**暂不需要**。
