# 开发模式部署（腾讯云）— 极简版

> 目标：**3 条命令启动**；更新代码只需 `git pull` + 重启调度器。不装 Nginx、不配 systemd。
> 下文以 `/opt/stock-cash-streaming` 为例，请按你的实际路径替换。

---

## 0. 已经按正式指南部署过？

开发模式不需要 systemd。先停掉旧服务，避免和 tmux 里的进程抢 8000 端口：

```bash
systemctl list-units --type=service --all | grep -i stock
sudo systemctl disable --now stock-web stock-scheduler stock-daily.timer
```

`data/`、`.env`、`logs/` 保留不动，直接按第 2 节启动即可。

---

## 1. 首次部署（只做一次）

### 1.1 系统依赖

```bash
sudo apt update
sudo apt install -y git curl tmux python3.12 python3.12-venv
sudo timedatectl set-timezone Asia/Shanghai
date   # 应为 CST
```

Ubuntu 22.04 默认没有 Python 3.12，先加源：

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv
```

### 1.2 代码与依赖

```bash
sudo mkdir -p /opt/stock-cash-streaming
sudo chown -R $USER:$USER /opt/stock-cash-streaming

git clone https://github.com/Katte-bot/stock-cash-streaming.git /opt/stock-cash-streaming
cd /opt/stock-cash-streaming

python3.12 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .        # 让 python -m stock_cash_streaming.cli 可用
mkdir -p logs data/backups
```

如果目录已有代码但没有 `.git`：

```bash
cd /opt/stock-cash-streaming
git init
git remote add origin https://github.com/Katte-bot/stock-cash-streaming.git
git fetch origin
git checkout -B main origin/main
```

> `data/`、`logs/`、`.env` 都在 `.gitignore` 里，更新代码不会覆盖。

### 1.3 配置

```bash
cd /opt/stock-cash-streaming
cp .env.example .env
chmod 600 .env
vim .env
```

```dotenv
DB_PATH=data/stock_cash_streaming.sqlite3
HOST=0.0.0.0
PORT=8000

# AI 日报可选；不填会自动降级为规则模板
OPENAI_API_KEY=sk-xxxx
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
```

### 1.4 数据

**方式 A：GitHub 快照（推荐，服务器直接拉取）**

仓库内已提交精简快照 `deploy/stock_snapshot.sqlite3.gz`（约 40 MB）：
包含行情、资金流、财务、分红、回测、目标组合、跟踪；已剔除可重算的 `fact_factor` 和 `fact_financial_revision`。

```bash
cd /opt/stock-cash-streaming

# 先停掉 Web 与调度器，避免覆盖时仍有连接
# 备份旧库（如果已有）
cp data/stock_cash_streaming.sqlite3 data/backups/stock-before-restore.sqlite3 2>/dev/null || true

# 拉取快照（已 git clone 的话，git pull 即可）
git pull --ff-only
# 或者不 pull，直接下载：
# curl -L -o deploy/stock_snapshot.sqlite3.gz \
#   https://raw.githubusercontent.com/Katte-bot/stock-cash-streaming/main/deploy/stock_snapshot.sqlite3.gz

# 覆盖并清理旧 WAL
gunzip -c deploy/stock_snapshot.sqlite3.gz > data/stock_cash_streaming.sqlite3
rm -f data/stock_cash_streaming.sqlite3-wal data/stock_cash_streaming.sqlite3-shm

# 重建因子（v2 技术/资金流 + v3 基本面，约 2–3 分钟）
.venv/bin/python -m stock_cash_streaming.cli factors

.venv/bin/python -m stock_cash_streaming.cli status
```

> 快照日期：2026-10-26；SHA256：`36343fa915fbd6671fa5a142b7d0dd66e6009a6597aee16d67cdfd427d19e8b2`
> 快照不含 `fact_factor` / `fact_financial_revision`，恢复后必须跑一次 `cli factors`；修订表可选 `cli revisions --backfill`。

**方式 B：从开发机直接 rsync（不走 GitHub）**

```bash
# 开发机
cd /data02/stock-cash-streaming
.venv/bin/python - <<'PY'
import sqlite3
src = sqlite3.connect('data/stock_cash_streaming.sqlite3')
dst = sqlite3.connect('/tmp/stock_cash_streaming.sqlite3')
src.backup(dst)
dst.close(); src.close()
PY

rsync -av /tmp/stock_cash_streaming.sqlite3 \
  <user>@<云服务器IP>:/opt/stock-cash-streaming/data/

# 云服务器
cd /opt/stock-cash-streaming
rm -f data/stock_cash_streaming.sqlite3-wal data/stock_cash_streaming.sqlite3-shm
.venv/bin/python -m stock_cash_streaming.cli status
```

**方式 C：全新库**

先按 [deployment-guide.md](./deployment-guide.md) §5 方式 B 初始化，再执行：

```bash
cd /opt/stock-cash-streaming
.venv/bin/python -m stock_cash_streaming.cli universe --id C1 --refresh --sync --fundamentals --factors
.venv/bin/python -m stock_cash_streaming.cli universe --id C2 --refresh
.venv/bin/python -m stock_cash_streaming.cli universe --id C3 --refresh --sync --fundamentals --factors
.venv/bin/python -m stock_cash_streaming.cli factors
for u in C0 C1 C2 C3; do
  .venv/bin/python -m stock_cash_streaming.cli backtest \
    --universe $u --exclude-untradable --cost-model a_share --limit-model board
done
```

### 1.5 放行端口

- 腾讯云控制台 → 安全组 → 入站规则 → 放行 **TCP 8000**；
- 系统没有登录鉴权，建议来源限制为你自己的公网 IP；
- 若开了 ufw：`sudo ufw allow 8000/tcp`。

---

## 2. 启动（每次重启机器后）

```bash
cd /opt/stock-cash-streaming
tmux new -s scs
```

**窗口 0：Web（开发模式）**

```bash
.venv/bin/uvicorn stock_cash_streaming.service.app:app \
  --host 0.0.0.0 --port 8000 --reload
```

**窗口 1：每日调度器**（`Ctrl+b c` 新建窗口）

```bash
.venv/bin/python -m stock_cash_streaming.cli scheduler --time 16:10
```

- 退出但保持运行：`Ctrl+b d`；回来：`tmux a -t scs`
- 停止服务：在对应窗口按 `Ctrl+C`
- `--reload` 只监听代码文件；调度器不支持热重载

验证：

```bash
curl -s http://127.0.0.1:8000/api/health     # {"status":"ok"}
.venv/bin/python -m stock_cash_streaming.cli status
```

浏览器打开 `http://<云服务器公网IP>:8000`。

不想用 tmux：

```bash
nohup .venv/bin/uvicorn stock_cash_streaming.service.app:app \
  --host 0.0.0.0 --port 8000 --reload > logs/web.log 2>&1 &
nohup .venv/bin/python -m stock_cash_streaming.cli scheduler --time 16:10 \
  > logs/scheduler.log 2>&1 &
```

---

## 3. 更新代码（最常用）

```bash
cd /opt/stock-cash-streaming

git status --short          # 1) 确认没有未提交的本地修改
git pull --ff-only          # 2) 拉取
.venv/bin/pip install -r requirements.txt   # 3) 依赖有变化时
.venv/bin/pip install -e .
```

- **Web**：`--reload` 会自动加载新代码，无需操作；
- **调度器**：到 tmux 窗口 1 按 `Ctrl+C`，重新执行启动命令；
- **数据库**：启动时自动执行 `init_schema`，新增表/列自动迁移，无需手工 SQL。

更新前备份（建议）：

```bash
cd /opt/stock-cash-streaming
.venv/bin/python - <<'PY'
import datetime, pathlib, sqlite3
p = pathlib.Path('data/backups') / f"stock-{datetime.date.today():%Y%m%d}.sqlite3"
p.parent.mkdir(parents=True, exist_ok=True)
src = sqlite3.connect('data/stock_cash_streaming.sqlite3')
dst = sqlite3.connect(p); src.backup(dst); dst.close(); src.close()
print('backup ->', p)
PY
```

> 当前库约 1.8 GB，每份备份同体积；保留最近 3–5 份即可。

如果 `git pull` 提示本地修改冲突：

```bash
git stash push -m "server-local"
git pull --ff-only
git stash pop
```

更推荐在本地改完并 push 到 GitHub，再在服务器 `git pull`。

---

## 4. 常用命令

```bash
cd /opt/stock-cash-streaming

.venv/bin/python -m stock_cash_streaming.cli status          # 数据水位
.venv/bin/python -m stock_cash_streaming.cli daily --force   # 手动跑流水线
.venv/bin/python -m stock_cash_streaming.cli recover --dry-run
.venv/bin/python -m stock_cash_streaming.cli recover --lookback 30
.venv/bin/python -m stock_cash_streaming.cli fundamentals    # 财务/分红（C0+C1）
.venv/bin/python -m pytest -q                                # 测试

tail -f logs/web.log
tail -f logs/scheduler.log
tail -f logs/app_$(date +%F).log
```

---

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| 公网打不开 | 安全组放行 8000；`--host 0.0.0.0`；`sudo ufw status` |
| 8000 被占用 | `ss -lntp \| grep 8000`，`kill <PID>` 后重启 |
| 时间偏 8 小时 | `sudo timedatectl set-timezone Asia/Shanghai` |
| `database is locked` | 同时跑了两个写任务；只运行一个 `daily/recover/portfolio` |
| 改代码页面没变 | Web 是否带 `--reload`；调度器需手动重启；浏览器硬刷新 |
| 调度器没触发 | `tmux a -t scs` 看进程；确认时区 |
| 920010 拉取失败 | 北交所接口不支持，属预期 |
| 磁盘不足 | 当前库约 1.8 GB，备份同体积，建议 40 GB+ |

---

## 附录：一键启动 / 更新（可选）

```bash
# 启动（已存在则跳过）
cd /opt/stock-cash-streaming && tmux has-session -t scs 2>/dev/null || {
  tmux new-session -d -s scs -n web \
    'cd /opt/stock-cash-streaming && .venv/bin/uvicorn stock_cash_streaming.service.app:app --host 0.0.0.0 --port 8000 --reload'
  tmux new-window -t scs -n scheduler \
    'cd /opt/stock-cash-streaming && .venv/bin/python -m stock_cash_streaming.cli scheduler --time 16:10'
  echo "started: tmux a -t scs"
}
```

```bash
# 更新
cd /opt/stock-cash-streaming && git pull --ff-only && \
  .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e . && \
  tmux send-keys -t scs:scheduler C-c && sleep 1 && \
  tmux send-keys -t scs:scheduler \
    'cd /opt/stock-cash-streaming && .venv/bin/python -m stock_cash_streaming.cli scheduler --time 16:10' Enter && \
  echo "updated: web auto-reloads, scheduler restarted"
```
