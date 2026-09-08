#!/bin/bash
# 盘中定时快照(供 launchd 调用):在真实 10:30 抓一次实时行情快照落盘,
# 使"早盘偏离核实"不依赖 Claude 定时会话何时醒。
#
# 为什么需要 OS 级定时:后台定时会话在桌面 App 非活动时工具调用会被挂起数小时才执行
# (实测盘中任务 10:34 触发、13:50 才跑第一条命令),取数时点因此严重漂移。把"在正确时点
# 取数"这件确定性的事下沉到代码 + launchd,判断部分才留给会话。见 docs/计划/定时任务时序治理.md
#
# 代码源:从常驻专用 worktree 跑最新 origin/main(与 pull_refresh.sh 同款卫生)。**不从主仓跑**——
#   主仓工作树被并发会话卡在旧 commit 时,intraday_snapshot.py/intraday_watch.py 等新模块会 ModuleNotFound。
#   专用 worktree 是独立工作副本、动它的 git 不碰主仓 HEAD/WIP,盘中也安全。②无需 LLM_*(纯行情抓取)。
# data/intraday 在 worktree 内是指向主仓的 symlink,快照落主仓共享目录、盘中观测/下游读同一份。
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
SLOT="${INTRADAY_SLOT:-1030}"
LOG="${STOCK_INTRADAY_LOG:-$HOME/.local/state/stock/intraday_snapshot.log}"
mkdir -p "$(dirname "$LOG")"

# 单实例锁:避免与上一轮重叠(与 pull_refresh.sh 同款 mkdir 原子锁)
LOCK="$HOME/.local/state/stock/intraday_snapshot.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) intraday_snapshot slot=$SLOT ===================="
  "$PY" -m tools.pipeline.intraday_snapshot --slot "$SLOT"
  RC=$?
  # 退出码语义:0=成功或非交易日跳过或幂等跳过;非0=全部标的抓取失败(此时不落文件,下游按缺快照降级)
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
