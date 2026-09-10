#!/bin/bash
# 午盘选股复盘(供 launchd 调用):工作日 15:12 触发,对当日午盘选出的票记「下午 α」。
#   读午盘节点落盘的 11:30 快照 + 主档当日收盘 → 下午涨跌%/下午等权基准 → 买入组/规避组逐票 α
#   → 产 docs/每日分析/复盘/午盘_<date>.md + 追加结论到经验 inbox。**纯确定性、不跑 LLM、不触网。**
#
# 时点:15:12 定在 breadth(15:05)之后、且需主档已含当日收盘(由收盘数据同步 job 落盘)。
#   若主档当日收盘未就绪,复盘会**优雅降级**(基准 NaN、票记「—」),不崩;届时把本 job 往后挪即可。
# 代码源:从常驻专用 worktree 跑最新 origin/main(与 intraday_screen.sh 同款卫生、同一 worktree
#   → 复盘读到的 data/intraday/<date>/ 快照与午盘选股写的是同一份)。**不从主仓跑**。
# 单实例锁避免与上一轮重叠。退出码:0=成功或非交易日/幂等跳过;非0=pipeline 失败。
set -uo pipefail

# —— 专用 worktree 卫生:强制常驻 worktree 更到最新 origin/main 再跑(照 intraday_screen.sh)——
WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "$(date) 警告:git fetch origin 失败,用该 worktree 现有 origin/main" >&2
git -C "$WORKTREE" reset --hard origin/main >/dev/null 2>&1 || echo "$(date) 警告:reset --hard origin/main 失败,用 worktree 当前代码" >&2
REPO="$WORKTREE"
cd "$REPO"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_INTRADAY_REVIEW_LOG:-$HOME/.local/state/stock/intraday_review.log}"
mkdir -p "$(dirname "$LOG")"

# 单实例锁:避免与上一轮重叠(与 intraday_screen.sh 同款 mkdir 原子锁)
LOCK="$HOME/.local/state/stock/intraday_review.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) intraday_review ===================="
  "$PY" -m tools.run intraday_review "$@"
  RC=$?
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
