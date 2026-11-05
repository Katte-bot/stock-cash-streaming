# 运维手册：日常运行与中断恢复

适用版本：2026-10（`defensive_top12_monthly` 策略、SQLite 单机部署）。
配套文档：部署与迁移见 [deployment-guide.md](./deployment-guide.md)，系统设计见 [software-architecture.md](./software-architecture.md)。

---

## 1. 系统组成

| 组件 | 作用 | 启动方式 |
|---|---|---|
| Web 服务 | 仪表盘 + REST API + WebSocket | `.venv/bin/uvicorn stock_cash_streaming.service.app:app --host 0.0.0.0 --port 8000` |
| 每日调度器 | 工作日 16:10 触发流水线 | `.venv/bin/python -m stock_cash_streaming.cli scheduler --time 16:10` |
| SQLite | 全部行情/因子/组合/跟踪数据 | `data/stock_cash_streaming.sqlite3`（WAL 模式，含 `-wal`/`-shm` 文件） |
| 日志 | 运行日志，按日轮转保留 14 天 | `logs/app_YYYY-MM-DD.log`、`logs/scheduler.log` |

流水线顺序：**增量行情 → 因子 → 信号 → 目标组合 → 模拟跟踪**。所有写入均为 upsert，重复执行安全。

---

## 2. 每日检查（1 分钟）

```bash
cd /data02/stock-cash-streaming

tail -n 20 logs/scheduler.log                                   # 是否触发、是否成功
.venv/bin/python -m stock_cash_streaming.cli status             # 数据水位
.venv/bin/python -m stock_cash_streaming.cli tracking --no-persist   # 跟踪净值与调仓次数
curl -s http://localhost:8000/api/health                        # 服务存活
```

正常特征：

- `logs/scheduler.log` 出现 `每日流水线完成 YYYY-MM-DD: ok`
- `fact_quote` 水位等于最近一个交易日
- `tracking` 的 `days` 随交易日递增，`rebalances` 只在调仓日 +1
- `health` 返回 `{"status":"ok"}`

---

## 3. 中断恢复：一条命令

```bash
cd /data02/stock-cash-streaming

# 1) 预检：只读，告诉你缺哪些调仓日
.venv/bin/python -m stock_cash_streaming.cli recover --dry-run

# 2) 实际恢复：--lookback 必须大于中断天数（默认 30 天）
.venv/bin/python -m stock_cash_streaming.cli recover --lookback 30
```

`recover` 会依次完成：

1. 按 `--lookback` 补拉全部 71 只成分的日线（幂等，自动补缺口）
2. 全量重算 19 个因子
3. 重新生成板块内信号
4. 找出「最后快照之后、且落在 20 交易日调仓周期上」的日期，按时间升序重放目标组合快照
5. 从首个调仓快照完整重放模拟跟踪净值

其他参数：

| 参数 | 用途 |
|---|---|
| `--lookback N` | 行情回看天数，建议 ≥ 中断天数 × 1.5 |
| `--no-ingest` | 跳过联网补行情，只重算因子/补快照/重放跟踪（网络受限或刚补过数时用） |
| `--dry-run` | 只读预检，不写任何业务数据 |

耗时：联网补数约 4–5 分钟（71 只），纯重算约 1 分钟。执行记录写入 `task_log`（`recover_YYYYMMDD`）。

> 注意：`recover` 依赖行情序列完整。如果中断很久，先把 `--lookback` 设大（如 90），否则交易日序号错位会导致调仓日判断偏移。

---

## 4. 按场景处理

| 场景 | 推荐命令 | 说明 |
|---|---|---|
| 只漏了 1 天，且不是调仓日 | `cli daily --force` | 默认回看 10 天，顺带补齐 |
| 漏了多天，未跨调仓日 | `cli recover --lookback 30` | 自动补行情 + 重放跟踪 |
| 漏了调仓日（关键） | `cli recover --lookback 30` | 必须重放遗漏的调仓快照，否则跟踪会沿用旧权重 |
| 只有资金流断档 | `cli flowbackfill` | 新浪逐只回补四档资金流；不影响组合与跟踪 |
| 数据源临时失败 | `cli incremental --lookback 10` | 失败标的会自动重试 3 次，920010 为已知失败 |
| 自定义池需补数/重跑或任务失败 | 总览页该池卡片（或在池详情页顶部）点「启动/更新」 | 幂等；已最新/已回测/已开启的步骤自动跳过并说明原因 |
| 模拟持仓初始化/重置 | 任意池详情页「③ 模拟持仓」→ 重置账户；或 `cli paper --universe C2 --reset` | 首次按该池历史调仓回放（10–30 秒），之后每日自动延续；所有池（含自建）都启用 |
| 节假日/周末 | 无需处理 | 流水线返回 `skipped`，次日自动继续 |

判断是否跨调仓日：

```bash
.venv/bin/python -m stock_cash_streaming.cli recover --dry-run
# 输出「缺失调仓日: 2026-11-02」即表示跨了调仓日
```

池任务（自定义池）状态与重试：

- 「启动/更新」依次检查 数据 → 财务 → 因子 → 回测 → 组合 → 跟踪 → 调度，每步写明执行或跳过原因，页面下方实时轮询步骤结果。
- 所有池默认开启每日调度（`daily_enabled=1`，含内置 C1~C3）；C0 由全局流水线维护，其余池可在页面「关闭调度」。
- 每日流水线末尾会为所有已开启调度的池补数据、重算因子、按需重跑回测并更新组合/跟踪；回测终点落后最新行情时会显示「回测 X · 落后 N 日」。
- 状态写入 `task_log`（`task_id = pool_{池ID}_{YYYYMMDD}`）；若状态长时间停在 `running`（超过 2 小时）视为进程中断，可直接再次点击启动。
- 同一时间只允许一个池任务：提示 409/“已有池任务运行中”时等待当前任务结束（每日流水线运行期间也会占用该锁）。
- 停机多日恢复：先 `cli daily --force`（默认回看 10 天）把行情/因子/回测/组合/跟踪补齐，再确认调度器已常驻。

---

## 5. 手工兜底步骤

`recover` 不可用时（例如需要逐段排查），按下面顺序执行，效果等价：

```bash
# 1) 补行情：lookback 覆盖中断天数
.venv/bin/python -m stock_cash_streaming.cli incremental --lookback 30

# 2) 全量重算因子
.venv/bin/python -m stock_cash_streaming.cli factors

# 3) 重新生成信号
.venv/bin/python -m stock_cash_streaming.cli signals

# 4) 列出缺失调仓日（只读）
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('data/stock_cash_streaming.sqlite3')
dates = [r[0] for r in c.execute("select distinct trade_date from fact_quote order by trade_date")]
last = c.execute("select max(trade_date) from fact_target_portfolio "
                 "where strategy='defensive_top12_monthly' and is_rebalance=1").fetchone()[0]
print("最后调仓快照:", last)
for i, d in enumerate(dates):
    if d > last and i % 20 == 0:
        print("待补调仓日:", d)
PY

# 5) 按日期从早到晚重放（示例日期替换为上一步输出，顺序不能乱）
.venv/bin/python -m stock_cash_streaming.cli portfolio --date 2026-11-02

# 6) 补最新交易日的组合快照
.venv/bin/python -m stock_cash_streaming.cli portfolio

# 7) 从首个快照完整重放跟踪净值
.venv/bin/python -m stock_cash_streaming.cli tracking
```

说明：

- `portfolio --date` 只使用该日及之前的因子，不引入未来数据
- 重放顺序必须从早到晚，否则「持仓缓冲 1 名」和「相对上次调仓的变化」会算错
- `tracking` 每次从首个快照完整重算，不需要删除旧净值

### 5.1 研究平台（全 A + 策略库）数据维护

研究平台依赖额外数据底座，与每日池任务解耦，按需执行（首次部署或数据缺失时）：

```bash
# 1) 全 A 股票列表 + 行情回填（多线程、单票断点续跑；可后台跑）
nohup .venv/bin/python -m stock_cash_streaming.cli market \
      --sync-symbols --backfill --only-missing --workers 10 > logs/market_backfill.log 2>&1 &

# 2) 申万行业分类（一次请求，约 5500 只）
.venv/bin/python -m stock_cash_streaming.cli market --industry

# 3) 指数行情（沪深300/中证500/1000/全指）
.venv/bin/python -m stock_cash_streaming.cli market --index

# 4) 现金流量表 / 资产负债表关键科目（逐股请求，建议后台跑）
nohup .venv/bin/python -m stock_cash_streaming.cli market \
      --statements --only-missing --workers 20 > logs/statements.log 2>&1 &

# 4.1) 利润表关键科目（业绩报表，按报告期批量：47 个季度约 1.5–2 小时）
nohup .venv/bin/python -m stock_cash_streaming.cli market \
      --income --income-since 2015 > logs/income.log 2>&1 &

# 5) 查看覆盖度（命令末尾会自动打印；含 income_coverage）
.venv/bin/python -m stock_cash_streaming.cli market --index

# 6) 财报事件（SUE/营收惊喜/公告日收益）与池成员月度快照
.venv/bin/python -m stock_cash_streaming.cli market --events
.venv/bin/python -m stock_cash_streaming.cli market --members

# 7) 因子动量权重 + 市场状态表（建议每周/每月一次）
.venv/bin/python -m stock_cash_streaming.cli strat meta --universe C0

# 8) 研究因子缓存（按因子落盘，可中断续跑；含内存护栏）
.venv/bin/python -m stock_cash_streaming.cli research-cache --universe C0,C1,C2,C3
.venv/bin/python -m stock_cash_streaming.cli research-cache --universe A0
```

要点：

- 研究因子不写 SQLite 日频，缓存在 `data/research_cache/<key>_<数据版本戳>/`：`panel.pkl` + `factors/<因子名>.pkl` + `manifest.json`；缓存 key 含「财务最新公告日 + 财务行数 + 行情最新交易日」哈希，**财报/行情回填后会自动失效重建**；
- **重建请在 CLI 里做，不要在页面触发**：`cli research-cache` 逐因子落盘、可中断续跑，并带内存护栏（可用内存低于 `SCS_MIN_AVAIL_GB`，默认 6 GB 时主动中止并保留已落盘结果）。Web 端只读缓存，缓存缺失时研究页返回 503 并提示执行该命令；
- 每日数据更新后旧缓存会被标记为过期，Web 端会**降级读取上一版缓存**（响应里 `cache=stale`），不会阻塞页面；
- 夜间批量：`nohup bash scripts/overnight_research.sh > logs/overnight_research.nohup 2>&1 &`（小池缓存 → A0 缓存 → 首批 5 个策略重跑；外部看门狗在可用内存低于 3 GB 时终止子进程）；
- 研究池 `A0`（全 A）不参与每日池任务与模拟持仓，仅用于研究和回测；
- 研究回测走 `cli strat backtest --strategy value_quality --universe A0 --start 2018-01-01`，结果落 `fact_strategy_run`；`--capacity 0.01` 可单独指定容量约束（默认策略内 1%）；
- 因子 IC 走 `cli strat ic --universe A0`，落 `fact_factor_ic`；`cli strat meta` 会把权重写 `fact_strategy_weight`、市场状态写 `fact_regime_state`、成员快照写 `dim_universe_member_history`；
- 研究策略绑定到股票池后（池详情「策略配置条」），池任务会按 `{strategy_id}_{pool}` 写目标组合快照，并自动刷新跟踪净值与模拟账户；
- 日志：`logs/market_backfill.log`、`logs/statements.log`、`logs/overnight_research.log`；写库任务尽量避让 `daily/recover`。
- 财务口径补齐：利润表只入 `revenue/net_profit/eps/bps/roe/ocf_ps`（`src='em:yjbb'`，按报告期批量、`pub_date` 取法定披露截止日）；因子层再派生 `equity = total_assets - total_liabilities`、`shares = equity/bps`（缺失时用 `net_profit/eps`）、`ocf = ocf_ps × shares`，因此全 A 都能算估值与现金流类因子。`--income-force` 可忽略水位重拉全部报告期。

---

## 6. 恢复后校验

```bash
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('data/stock_cash_streaming.sqlite3')
q = lambda s: list(c.execute(s))
print("行情最新      ", q("select max(trade_date) from fact_quote"))
print("因子最新(v2)  ", q("select max(trade_date) from fact_factor where version='v2'"))
print("组合快照      ", q("select trade_date, is_rebalance from fact_target_run order by trade_date desc limit 5"))
print("跟踪净值天数  ", q("select count(*), max(trade_date) from fact_tracking_nav"))
print("最近任务      ", q("select task_id, status, ended_at from task_log order by ended_at desc limit 3"))
PY

.venv/bin/python -m stock_cash_streaming.cli tracking --no-persist
curl -s http://localhost:8000/api/tracking | head -c 400
```

通过标准：

1. 行情/因子最新日期等于最近交易日
2. `fact_target_run` 中缺失的调仓日已出现且 `is_rebalance=1`
3. `tracking` 的 `days` 与基线执行日以来的交易日数一致
4. `rebalances` 列表覆盖所有应有调仓日，`pending_rebalance` 为 `null`（除非行情只到信号日）

---

## 7. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 流水线 `skipped` | 非交易日，或当日行情还没出 | 16:10 后再跑 `cli daily`；必要时 `--force` |
| 日志出现 `920010` 失败 | 北交所标的接口不支持 | 预期行为，不影响其余 70 只 |
| `database is locked` | 同时跑了两个写任务 | 确认只有一个 `daily/recover/portfolio` 在跑；WAL 下读取不受影响 |
| 端口 8000 被占用 | 旧进程未退出 | `ss -lntp \| grep 8000`，结束旧进程后重启 uvicorn |
| 调度器没触发 | 进程被杀/机器重启 | 重新启动调度器（见第 8 节） |
| `tracking` 一直 `waiting` | 基线建立后还没有次一交易日行情 | 正常，等下一交易日 |
| 时间偏 8 小时 | 服务器时区不是 Asia/Shanghai | `sudo timedatectl set-timezone Asia/Shanghai` |
| 同一天重跑 `task_log` 时间没变 | `task_id=daily_YYYYMMDD` 只更新状态，保留首次 `started_at` | 预期行为 |
| 目标组合日期不是最新交易日 | 最新日滚动因子为空（历史 `pct_chg` 有缺口） | 跑 `cli recover --lookback 30` 补齐后重算 |
| 自然语言策略「解析失败」 | 未配置 LLM（`OPENAI_API_KEY`），或模型返回了白名单外的指标 | 展开「手动编辑 JSON」直接回测；或在服务器上用 `cli nl --spec-file spec.json --universe C0` |
| 策略回测提示「股票池为空 / 无行情数据」 | 池成员未拉取行情，或区间不足 30 个交易日 | 先在池详情点「启动/更新」，再重跑；缩短条件或放宽区间 |
| 进程「无报错直接消失」 | 被内核 OOM 杀掉（典型：全 A 级别的回测/因子一次性加载） | `journalctl -k --since "-2h" \| grep -i "out of memory"` 确认；用 `--universe` 限定池（回测加载已按池在 SQL 层过滤），研究因子改用 `cli research-cache` 分因子落盘；`common/mem.py` 会在可用内存低于 6 GB 时主动中止 |
| 研究页 503「缓存未就绪」 | Web 端不再在线重算全 A 因子（防止 OOM） | 执行 `cli research-cache --universe A0`（可中断续跑），完成后刷新页面 |
| 研究页数据比行情晚一天 | 数据源分批发布时，面板会剔除当日不完整行情（覆盖 < 近 20 日中位数一半） | 正常保护；等数据源发布完整后重跑 `cli research-cache` |
| `pgrep -f "cli daily"` 显示在跑但日志已停 | `pgrep -f` 会匹配到自己那条含关键字的命令 | 用 `ps -eo pid,etime,args \| grep "cli daily"` 或看日志末行/`task_log` 判断 |

---

## 8. 进程重启

```bash
cd /data02/stock-cash-streaming

# Web 服务（前台调试）
.venv/bin/uvicorn stock_cash_streaming.service.app:app --host 0.0.0.0 --port 8000

# Web 服务（后台）
nohup .venv/bin/uvicorn stock_cash_streaming.service.app:app --host 0.0.0.0 --port 8000 \
  > logs/web.log 2>&1 &

# 每日调度器（后台）
nohup .venv/bin/python -m stock_cash_streaming.cli scheduler --time 16:10 \
  > logs/scheduler.log 2>&1 &
```

生产环境建议改用 systemd 托管，避免重启后失效，配置见 [deployment-guide.md](./deployment-guide.md) 第 6–7 节。

---

## 9. 数据备份与恢复

数据库是唯一需要备份的资产（代码与配置可从仓库恢复）。

**备份**（建议每周一次，保留最近 2 份；库已 13 GB 级别，不宜每日全量）：

```bash
cd /data02/stock-cash-streaming
.venv/bin/python -m stock_cash_streaming.cli backup            # 在线备份 + 保留 2 份 + 打印体积/行数
# crontab 示例（每周日 03:00）
# 0 3 * * 0 cd /data02/stock-cash-streaming && .venv/bin/python -m stock_cash_streaming.cli backup >> logs/backup.log 2>&1
```

需要自定义目录或保留份数时用 `--dir` / `--keep`；当天重复执行会跳过（`--force` 覆盖）。

**恢复**：

1. 停 Web 服务与调度器
2. 删除 `data/stock_cash_streaming.sqlite3` 及其 `-wal`、`-shm`
3. 把备份文件复制为 `data/stock_cash_streaming.sqlite3`
4. 跑 `cli status` 与 `cli tracking --no-persist` 校验，再启动服务与调度器

**迁移到新机器**：见 [deployment-guide.md](./deployment-guide.md) 第 5 节。

---

## 10. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-10-20 | 首版：新增 `cli recover`、中断恢复流程、备份与常见问题 |
| 2026-10-29 | 补充池任务（自定义股票池）状态查看与重试说明 |
| 2026-11-02 | 模拟持仓（`/api/paper`）与自然语言策略（`/api/nl`）的排查条目 |
| 2026-11-04 | 新增 5.1 研究平台数据维护（全 A 行情/行业/指数/财报、因子缓存、`cli market` / `cli strat`） |
| 2026-11-05 | 研究缓存改为按因子落盘 + 懒加载；新增 `cli research-cache`、内存护栏与夜间脚本；每日流水线增加行情发布完整性护栏 |
