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
| 节假日/周末 | 无需处理 | 流水线返回 `skipped`，次日自动继续 |

判断是否跨调仓日：

```bash
.venv/bin/python -m stock_cash_streaming.cli recover --dry-run
# 输出「缺失调仓日: 2026-11-02」即表示跨了调仓日
```

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

**备份**（建议每日收盘后，保留 14 天）：

```bash
cd /data02/stock-cash-streaming
.venv/bin/python - <<'PY'
import sqlite3, datetime, pathlib
src = sqlite3.connect('data/stock_cash_streaming.sqlite3')
dst_path = pathlib.Path('data/backups') / f"stock-{datetime.date.today():%Y%m%d}.sqlite3"
dst_path.parent.mkdir(exist_ok=True)
dst = sqlite3.connect(dst_path)
src.backup(dst)          # 在线一致性备份，无需停服务
dst.close(); src.close()
print("backup ->", dst_path)
PY
```

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
