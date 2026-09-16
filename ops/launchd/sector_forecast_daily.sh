#!/bin/bash
# 板块预测系统 每日 runner(供 launchd 调用):工作日盘后 ~19:45(闭环 pullrefresh 之后、shadow 批 20:00 之前)
#   跑 sector_forecast → 产 sector_regime.json(29板块冷热)+ sector_roster/<板块>.json(四角色)
#   + sector_focus.json(大盘风险偏好+宏观情景+重点/规避板块池)+ sector_daily.md + market_daily.md(每日综合总报告)
#   + data/sector_news/<date>.json(宏观指标独立库)。**forward-shadow·non-gating·不接生产选股**(接入 gated 待 B' 覆盖验证)。
#
# ⚠️ 纪律:防未来只用 ≤date 已披露;akshare 单源失败优雅降级不阻断;产物供次日午盘/晚间选股 as-of 读取(EOD 口径)。
# 卫生:①常驻专用 dailyjob worktree 跑最新 origin/main(fetch+reset --hard·不碰主仓 HEAD·data 软链→主仓);
#   ②纯量价+akshare、**不走 LLM**;③单实例锁。
set -uo pipefail

WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "$(date) 警告:git fetch origin 失败,用现有 origin/main" >&2
git -C "$WORKTREE" reset --hard origin/main >/dev/null 2>&1 || echo "$(date) 警告:reset --hard 失败,用 worktree 当前代码" >&2
cd "$WORKTREE"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_SECTOR_FORECAST_LOG:-$HOME/.local/state/stock/sector_forecast.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/sector_forecast.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) sector_forecast ===================="
  # CLI 缺省 --date=今天;非交易日自跳过退 0。全A K线/主档自动探测主仓(data 软链)。
  "$PY" -m tools.analysis.sector_forecast ${STOCK_DATA_ROOT:+--data-root "$STOCK_DATA_ROOT"}
  RC=$?
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
