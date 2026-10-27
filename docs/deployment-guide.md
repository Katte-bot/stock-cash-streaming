# 部署指南：迁移到云服务器

适用版本：2026-10（Python 3.12 + FastAPI + SQLite 单机）。
配套文档：日常运维与中断恢复见 [operations-runbook.md](./operations-runbook.md)。
自用/开发调试可参考更简单的 [开发模式部署](./dev-deployment.md)（tmux + `uvicorn --reload`，无需 systemd/Nginx）。

> 本系统是**单机、单写者**架构：SQLite 数据库由「Web 服务（只读为主）+ 每日流水线（写入）」共用。
> 不要把同一份数据库挂到多台机器同时写，也不要开多个 uvicorn worker。

---

## 1. 资源与系统要求

| 项目 | 建议 |
|---|---|
| 操作系统 | Ubuntu 22.04 / 24.04 LTS（本文以 24.04 为例） |
| CPU / 内存 | 2 vCPU / 4 GB 起（因子重算峰值内存约 1–2 GB） |
| 磁盘 | 40 GB SSD 起（当前库约数百 MB，留出备份与增长空间） |
| 时区 | `Asia/Shanghai`（否则 16:10 的调度会偏 8 小时） |
| Python | 3.12（项目 `requires-python >= 3.12`） |
| 网络 | 需可访问腾讯/新浪/同花顺行情接口；可选访问 DeepSeek API |

---

## 2. 系统准备

```bash
sudo apt update
sudo apt install -y git curl rsync tzdata python3.12 python3.12-venv python3-pip
sudo timedatectl set-timezone Asia/Shanghai
date   # 确认时区为 CST
```

> Ubuntu 22.04 默认仓库没有 3.12，需要先加 deadsnakes：
> ```bash
> sudo apt install -y software-properties-common
> sudo add-apt-repository -y ppa:deadsnakes/ppa
> sudo apt update
> sudo apt install -y python3.12 python3.12-venv
> ```

创建运行用户与目录：

```bash
sudo useradd -m -s /bin/bash stock
sudo mkdir -p /opt/stock-cash-streaming
sudo chown -R stock:stock /opt/stock-cash-streaming
```

---

## 3. 获取代码与安装依赖

方式 A：从 Git 仓库拉取（有远端时）

```bash
sudo -u stock git clone <你的仓库地址> /opt/stock-cash-streaming
```

方式 B：从旧机器同步（无远端时）

```bash
# 在旧机器执行
rsync -av --exclude='.venv' --exclude='data' --exclude='logs' --exclude='__pycache__' \
  /data02/stock-cash-streaming/ <user>@<新服务器IP>:/opt/stock-cash-streaming/
sudo chown -R stock:stock /opt/stock-cash-streaming   # 在新服务器执行
```

安装：

```bash
cd /opt/stock-cash-streaming
sudo -u stock python3.12 -m venv .venv
sudo -u stock .venv/bin/pip install -U pip
sudo -u stock .venv/bin/pip install -r requirements.txt
sudo -u stock .venv/bin/pip install -e .
sudo -u stock mkdir -p logs data
```

验证：

```bash
cd /opt/stock-cash-streaming
.venv/bin/python -m stock_cash_streaming.cli --help
```

---

## 4. 配置

```bash
cd /opt/stock-cash-streaming
sudo -u stock cp .env.example .env
sudo -u stock chmod 600 .env
sudo -u stock vim .env
```

关键配置：

```dotenv
DB_PATH=/opt/stock-cash-streaming/data/stock_cash_streaming.sqlite3
HOST=127.0.0.1
PORT=8000
TARGET_SECTORS=电网设备,天然气

# DeepSeek（OpenAI 兼容）
OPENAI_API_KEY=sk-xxxx
OPENAI_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
```

说明：

- 配置读取顺序：**系统环境变量 > `.env`**；`.env` 从进程工作目录读取，systemd 中需设置 `WorkingDirectory`
- `DB_PATH` 建议写绝对路径，避免工作目录变化导致连到新库
- `.env` 含密钥，权限 600，且不要提交到 Git

---

## 5. 数据迁移

### 方式 A：拷贝旧库（推荐，保留全部历史与跟踪）

```bash
# 1) 旧机器：停调度器，做在线一致性备份
cd /data02/stock-cash-streaming
.venv/bin/python - <<'PY'
import sqlite3
src = sqlite3.connect('data/stock_cash_streaming.sqlite3')
dst = sqlite3.connect('/tmp/stock_cash_streaming.sqlite3')
src.backup(dst)
dst.close(); src.close()
PY

# 2) 传输
rsync -av /tmp/stock_cash_streaming.sqlite3 \
  <user>@<新服务器IP>:/opt/stock-cash-streaming/data/stock_cash_streaming.sqlite3

# 3) 新机器：权限与校验
sudo chown stock:stock /opt/stock-cash-streaming/data/stock_cash_streaming.sqlite3
cd /opt/stock-cash-streaming
.venv/bin/python -m stock_cash_streaming.cli status
```

> 只需要主库文件；`-wal` / `-shm` 不复制（备份 API 已保证一致性）。

### 方式 B：全新初始化

```bash
cd /opt/stock-cash-streaming

# 1) 板块成分 + 历史行情（2018 年起，约几分钟）
.venv/bin/python -m stock_cash_streaming.cli init --start 20180101

# 2) 回补个股资金流长历史（新浪，逐只，较慢）
.venv/bin/python -m stock_cash_streaming.cli flowbackfill

# 3) 计算因子
.venv/bin/python -m stock_cash_streaming.cli factors

# 4) 生成信号、目标组合与跟踪基线
.venv/bin/python -m stock_cash_streaming.cli signals
.venv/bin/python -m stock_cash_streaming.cli portfolio
.venv/bin/python -m stock_cash_streaming.cli tracking
```

### 可选：历史回测

```bash
.venv/bin/python -m stock_cash_streaming.cli backtest
```

结果会落库，仪表盘自动读取最新一次回测。

---

## 6. Web 服务（systemd）

创建 `/etc/systemd/system/stock-web.service`：

```ini
[Unit]
Description=Stock Cash Streaming Web
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=stock
Group=stock
WorkingDirectory=/opt/stock-cash-streaming
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/stock-cash-streaming/.venv/bin/uvicorn \
    stock_cash_streaming.service.app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
# 只允许写数据与日志目录
NoNewPrivileges=true
PrivateTmp=true
ReadWritePaths=/opt/stock-cash-streaming/data /opt/stock-cash-streaming/logs

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stock-web
sudo systemctl status stock-web --no-pager
curl -s http://127.0.0.1:8000/api/health
```

> **只起 1 个进程**：SQLite 写入靠单进程串行，`--workers` 不要大于 1。

---

## 7. 定时流水线

### 方案 A：systemd timer（推荐）

`/etc/systemd/system/stock-daily.service`：

```ini
[Unit]
Description=Stock daily pipeline
After=network-online.target

[Service]
Type=oneshot
User=stock
Group=stock
WorkingDirectory=/opt/stock-cash-streaming
ExecStart=/opt/stock-cash-streaming/.venv/bin/python -m stock_cash_streaming.cli daily
Nice=10
```

`/etc/systemd/system/stock-daily.timer`：

```ini
[Unit]
Description=Run stock daily pipeline on weekdays 16:10

[Timer]
OnCalendar=Mon..Fri 16:10
Persistent=true
Unit=stock-daily.service

[Install]
WantedBy=timers.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stock-daily.timer
systemctl list-timers stock-daily.timer --no-pager
```

说明：

- `Persistent=true`：机器重启或停机错过触发时间后，**开机补跑一次**
- 补跑只跑一次。若停机超过 1 个交易日，仍建议手工执行 `cli recover --lookback N`（见运维手册）
- 执行日志同时进入 `journalctl -u stock-daily` 与 `logs/app_*.log`

### 方案 B：常驻调度器

`/etc/systemd/system/stock-scheduler.service`：

```ini
[Unit]
Description=Stock daily scheduler (resident)
After=network-online.target

[Service]
Type=simple
User=stock
Group=stock
WorkingDirectory=/opt/stock-cash-streaming
ExecStart=/opt/stock-cash-streaming/.venv/bin/python -m stock_cash_streaming.cli scheduler --time 16:10
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stock-scheduler
```

两种方案**只选一种**，不要同时启用，避免重复执行（虽有幂等保护，但会浪费一次全量计算）。

### 方案 C：cron

```cron
10 16 * * 1-5 cd /opt/stock-cash-streaming && .venv/bin/python -m stock_cash_streaming.cli daily >> logs/daily.log 2>&1
```

cron 不补跑停机期间的任务，需要人工 `recover`。

---

## 8. Nginx 反向代理 + HTTPS（可选）

应用自带页面与 API，没有登录鉴权。对外暴露时建议至少加一层访问控制。

```bash
sudo apt install -y nginx
sudo apt install -y certbot python3-certbot-nginx   # 有域名时
```

`/etc/nginx/sites-available/stock`：

```nginx
server {
    listen 80;
    server_name stock.example.com;

    # 可选：基础认证
    # auth_basic "restricted";
    # auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;   # WebSocket
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/stock /etc/nginx/sites-enabled/stock
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d stock.example.com          # 自动配置 HTTPS
```

基础认证（如需）：

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd youruser
sudo systemctl reload nginx
```

---

## 9. 防火墙与安全

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```

要点：

- uvicorn 绑定 `127.0.0.1`，只通过 Nginx 暴露
- `.env` 权限 600，不要提交到 Git、不要写进镜像
- 应用无用户体系，公网部署务必加 Basic Auth / IP 白名单 / VPN 之一
- 定期 `sudo apt update && sudo apt upgrade -y`

---

## 10. 备份

每日收盘后备份数据库（保留 14 天）：

```bash
sudo -u stock crontab -e
```

```cron
0 17 * * 1-5 cd /opt/stock-cash-streaming && .venv/bin/python - <<'PY' >> logs/backup.log 2>&1
import sqlite3, datetime, pathlib
src = sqlite3.connect('data/stock_cash_streaming.sqlite3')
out = pathlib.Path('data/backups') / f"stock-{datetime.date.today():%Y%m%d}.sqlite3"
out.parent.mkdir(exist_ok=True)
dst = sqlite3.connect(out)
src.backup(dst)
dst.close(); src.close()
print('backup ->', out)
PY
```

清理旧备份：

```cron
30 17 * * 1-5 find /opt/stock-cash-streaming/data/backups -name 'stock-*.sqlite3' -mtime +14 -delete
```

恢复步骤见 [operations-runbook.md](./operations-runbook.md) 第 9 节。

---

## 11. 迁移验收清单

```bash
cd /opt/stock-cash-streaming

# 1) 数据完整
.venv/bin/python -m stock_cash_streaming.cli status
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('data/stock_cash_streaming.sqlite3')
q = lambda s: list(c.execute(s))
print('行情', q("select min(trade_date), max(trade_date), count(*) from fact_quote"))
print('因子', q("select max(trade_date), count(*) from fact_factor where version='v2'"))
print('成分', q("select count(*) from dim_sector_member"))
print('组合', q("select trade_date, count(*) from fact_target_portfolio group by trade_date order by trade_date desc limit 3"))
print('跟踪', q("select count(*), max(trade_date) from fact_tracking_nav"))
PY

# 2) 服务
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/portfolio | head -c 300
curl -s http://127.0.0.1:8000/api/tracking | head -c 300

# 3) 定时任务
systemctl list-timers 'stock-*' --no-pager

# 4) 手工演练一次流水线（收盘后执行）
.venv/bin/python -m stock_cash_streaming.cli daily
```

通过标准：

1. 行情最新日期 = 迁移前最新交易日，行数与旧库一致
2. `/api/portfolio`、`/api/tracking` 返回的数据与旧环境一致
3. `daily` 返回 `ok`，`task_log` 新增记录
4. 次日 16:10 后 `daily` 自动执行，仪表盘数据更新

---

## 12. 常见问题

| 现象 | 处理 |
|---|---|
| `python3.12: command not found` | Ubuntu 22.04 需加 deadsnakes PPA（见第 2 节） |
| `pip install` 很慢或失败 | 换源：`pip install -i https://pypi.org/simple -r requirements.txt` |
| `ModuleNotFoundError: stock_cash_streaming` | 未执行 `pip install -e .`，或不在项目根目录 |
| 服务 502 | uvicorn 未启动或端口不是 8000；`journalctl -u stock-web -n 50` |
| 16:10 没有执行 | 时区不对；`timedatectl` 确认 Asia/Shanghai |
| 流水线 `skipped` | 节假日或当日行情未发布；收盘后重跑 `daily` |
| 磁盘增长快 | 定期清理 `logs/` 与 `data/backups/`；数据库可 `VACUUM`（需停机） |
| 内存不足 OOM | 因子重算峰值较高，升级到 4 GB 以上，或避免与回测同时运行 |
| 行情接口全部失败 | 云厂商网络/DNS 限制，检查出网与 DNS；必要时切换 `DATA_SOURCE_PRIMARY` |

---

## 13. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-10-20 | 首版：systemd 部署、数据迁移、定时任务、Nginx/HTTPS、备份与验收清单 |
