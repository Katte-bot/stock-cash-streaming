#!/usr/bin/env bash
# 夜间研究任务：预构建研究因子缓存 → 重跑首批策略（结果落库）。
#
# 特点：
# - 逐因子落盘、可中断续跑（重跑同一命令即可）；
# - 带内存看门狗：系统可用内存低于 MIN_AVAIL_GB 时立即终止当前子进程，
#   已写入的因子保留，不会把整机拖进 swap 或被内核 OOM kill；
# - 全部输出追加到 logs/overnight_research.log。
#
# 用法：
#   nohup bash scripts/overnight_research.sh > logs/overnight_research.nohup 2>&1 &
#   MIN_AVAIL_GB=4 bash scripts/overnight_research.sh          # 自定义看门狗阈值
set -u

cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
LOG=logs/overnight_research.log
MIN_AVAIL_GB=${MIN_AVAIL_GB:-3}
STRATEGIES=${STRATEGIES:-"value_quality residual_momentum conservative_investment pead factor_momentum"}
mkdir -p logs

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

_avail_gb() { awk '/MemAvailable/{printf "%.1f", $2/1048576}' /proc/meminfo; }
_rss_gb() { ps -o rss= -p "$1" 2>/dev/null | awk '{printf "%.1f", $1/1048576}'; }

# run_monitored <名称> <命令...>
run_monitored() {
  local name=$1; shift
  log "START ${name} :: $*"
  "$@" >>"$LOG" 2>&1 &
  local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    local avail rss
    avail=$(_avail_gb); rss=$(_rss_gb "$pid")
    log "  ${name} pid=${pid} rss=${rss}GB avail=${avail}GB"
    if awk -v a="$avail" -v m="$MIN_AVAIL_GB" 'BEGIN{exit !(a<m)}'; then
      log "  !! 可用内存 ${avail}GB < ${MIN_AVAIL_GB}GB，终止 ${name}（已落盘结果保留）"
      kill -TERM "$pid" 2>/dev/null
      sleep 5
      kill -KILL "$pid" 2>/dev/null
      log "END ${name}: guard-abort"
      return 124
    fi
    sleep 20
  done
  wait "$pid"; local rc=$?
  log "END ${name}: rc=${rc}"
  return $rc
}

export SCS_MIN_AVAIL_GB=${SCS_MIN_AVAIL_GB:-6}

log "===== 夜间研究任务开始（看门狗阈值 ${MIN_AVAIL_GB}GB，进程阈值 ${SCS_MIN_AVAIL_GB}GB）====="

# 1) 小池缓存（分钟级）
run_monitored "cache-C0-C3" $PY -m stock_cash_streaming.cli research-cache \
  --universe C0,C1,C2,C3 || log "小池缓存未全部成功，继续执行"

# 2) 全 A 缓存（最重的步骤）
run_monitored "cache-A0" $PY -m stock_cash_streaming.cli research-cache --universe A0 \
  || log "A0 缓存未完成，策略回测将自动跳过（可在修复后重跑本脚本续算）"

# 3) 首批策略重跑（结果持久化到 fact_strategy_run，页面直接读取）
for sid in $STRATEGIES; do
  run_monitored "strat-${sid}" $PY -m stock_cash_streaming.cli strat backtest \
    --strategy "$sid" --universe A0 || log "策略 ${sid} 未完成"
done

log "===== 夜间研究任务结束 ====="
