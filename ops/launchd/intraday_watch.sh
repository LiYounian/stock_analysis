#!/bin/bash
# 日内实时观测循环(供 launchd 调用):13:00 起动,盯"当日午盘候选∪当前持仓",
# 每 4s 轮询 gtimg、阈值触发买/卖倾向信号落 watch_events.jsonl,到 14:57 收盘前自停。
#
# 候选来源:当日 docs/每日分析/选股/日内_<date>.md(由分析师午盘产出)+ 持仓账本;
# 二者都空 → 无事可盯、直接退 0。急拉/急跌参考价读午休快照 T1145.json。
#
# 与 intraday_snapshot.sh 一致:从常驻专用 worktree 跑最新 origin/main(动 worktree 的 git 不碰主仓 HEAD/WIP);
#   否则主仓卡旧 commit 时 intraday_watch.py 会 ModuleNotFound(本任务修复动机之一)。纯行情、不调 LLM。
# 单实例锁避免与上一轮重叠(本进程会跑约 2 小时;reset 只在启动时做一次,运行期不再动 git)。
set -uo pipefail

# —— 专用 worktree 卫生:强制常驻 worktree 更到最新 origin/main 再跑(照 autopush.sh 选项A)——
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
UNTIL="${INTRADAY_WATCH_UNTIL:-14:57}"
INTERVAL="${INTRADAY_WATCH_INTERVAL:-4}"
LOG="${STOCK_INTRADAY_WATCH_LOG:-$HOME/.local/state/stock/intraday_watch.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/intraday_watch.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有观测实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) intraday_watch until=$UNTIL interval=$INTERVAL ===================="
  "$PY" -m tools.pipeline.intraday_watch --until "$UNTIL" --interval "$INTERVAL"
  RC=$?
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
