#!/bin/bash
# 午盘 Q 闸门+选股(供 launchd 调用):在 1430/1450/final 三段依次跑 gate → screen。
#
# ## 时序(为什么错开 1 分钟)
#
# launchd 没有 job 间依赖机制(M1 契约稿 §六.3 的既定结论:方案α 固定错峰),
# 且 StartCalendarInterval **不支持秒**,错峰粒度只能到分钟。
# 本 job 排在同 slot 快照之后 1 分钟,给「全A广度 + 焦点池126只快照」留出抓取时间
# (实测焦点池快照约 20-40s,加 breadth 全A聚合约 60s —— 余量偏紧,若日志频繁见
#  "上游 snapshot 缺失" 就把本 job 再推后 1 分钟)。
#
#   14:30  com.stock.midday_q_snapshot_1430   ← 快照(焦点池126)
#   14:30  com.stock.breadth_*(共用)          ← 全A广度(gate 的 G3/G4/G5 要)
#   14:31  本 job STAGE=1430                  ← gate + screen
#
# 若快照/广度尚未落盘,gate/screen 会 exit 1 报上游缺失(不静默出错结论),
# 下一个 slot 或次日重跑即可 —— 幂等设计不会重复写already-existing 的段。
#
# ## STAGE 语义(MIDDAY_Q_STAGE 环境变量,plist 里指定)
#   1430  → 首判:gate.stage1_1430 + screen.stage1_1430
#   1450  → 复核:gate.stage2_1450 + screen.stage2_1450(按 confirm_from 与首判交叉确认)
#   final → 收官:gate.final + screen.final(闸门翻转到弱势/崩盘则强制清空)
#
# 代码源/worktree 卫生/单实例锁 均照 intraday_snapshot.sh 同款。
#
# ⚠️ 测试环境研究用,非投资建议;只读行情、不下单。
set -uo pipefail

WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "$(date) 警告:git fetch origin 失败,用该 worktree 现有 origin/main" >&2
git -C "$WORKTREE" reset --hard origin/main >/dev/null 2>&1 || echo "$(date) 警告:reset --hard origin/main 失败,用 worktree 当前代码" >&2
cd "$WORKTREE"

PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
STAGE="${MIDDAY_Q_STAGE:?必须指定 MIDDAY_Q_STAGE(1430/1450/final)}"
LOG="${STOCK_MIDDAY_Q_LOG:-$HOME/.local/state/stock/midday_q_pipeline.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/midday_q_pipeline_${STAGE}.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) stage=$STAGE 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) midday_q pipeline stage=$STAGE ===================="
  # gate 先跑(screen 读 gate 的 allowed_strategies 决定跑哪几条策略)
  "$PY" -m tools.pipeline.midday_q_gate --stage "$STAGE"
  RC_GATE=$?
  echo "-- gate 退出码 $RC_GATE --"
  if [ "$RC_GATE" -ne 0 ]; then
    # gate 失败(上游快照/广度缺失)→ 不硬跑 screen(否则拿不到闸门状态,会出无效结论)
    echo "gate 非 0,跳过 screen(避免无闸门下出结论)"
    echo "-- 退出码 $RC_GATE --"
    exit $RC_GATE
  fi
  "$PY" -m tools.pipeline.midday_q_screen --stage "$STAGE"
  RC=$?
  echo "-- screen 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
