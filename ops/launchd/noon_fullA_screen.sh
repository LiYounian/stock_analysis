#!/bin/bash
# 午盘全A重筛(供 launchd 调用):工作日午休 ~11:35(抢 13:00 开盘前)
#   拉全A午盘快照(gtimg~17s) + 昨收/集合竞价/上午波动 量价轻筛 + 涨停不可买剔除
#   → 产 docs/每日分析/选股/午盘全A选股_<date>.md + data/intraday/<date>/noon_fullA_screen.json。
#   **正式午盘选股产出**(供尾盘 14:30~14:57 回踩限价参与)。踏空根治=盘中全A够得着动量爆发。
#
# ⚠️ 纪律:防未来只用 ≤11:30 快照;涨停不可买;md/JSON 标 provisional·尾盘复核·非投资建议。
# 卫生:①常驻专用 dailyjob worktree 跑最新 origin/main(fetch+reset --hard·不碰主仓 HEAD;data/master 软链→主仓);
#   ②纯行情/本地量价计算,**不走 LLM**;③单实例锁。
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
LOG="${STOCK_NOON_FULLA_LOG:-$HOME/.local/state/stock/noon_fullA_screen.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/noon_fullA_screen.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) noon_fullA_screen ===================="
  # CLI 缺省 --date=今天;非交易日自跳过退 0;幂等(快照存在不重抓,除非 --force)。全A K线/主档自动探测主仓(data/master 软链)。
  "$PY" -m tools.pipeline.noon_fullA_screen ${STOCK_DATA_ROOT:+--data-root "$STOCK_DATA_ROOT"}
  RC=$?
  # 退出码:0=成功/非交易日跳过/幂等跳过;非0=全A快照为空(网络/票池问题,下游按缺文件降级)。
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
