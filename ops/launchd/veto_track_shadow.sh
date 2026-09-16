#!/bin/bash
# B 基本面 veto 误杀 forward-shadow 记录器(供 launchd 调用):工作日盘后(拟 21:00,情绪20:00·④20:30·C20:45 之后)
#   记录当日"价量强(命中量价放量/最强选股)但被基本面 veto/超买规避"的票 → 落
#   data/shadow_forward/veto_track/<日>.json,并记其入场→D+1/D+2 绝对收益(未到期留 null)。
#   收尾 best-effort 跑 --backfill 回填历史 advisory 已到期收益。
#
# ⚠️ 纪律:纯记录、non-gating、forward-only——**绝不动 live veto、绝不进任何生产选股决策**;数据仅 6 天历史标签,
#   只能 forward 攒,≥120 样本才谈 validate。agent 只备本脚本 + plist,`launchctl load` 由用户本人(约法)。
#   依据:选股规避能力整改诊断与回测设计 §2B。
#
# 依赖顺序:读当日 量价放量/最强选股/多策略命中闸门 视图,须排在盘后闭环(产出这些视图)之后;不走 LLM。
# 卫生:①常驻专用 dailyjob worktree 跑最新 origin/main;②纯本地计算(视图/闸门/全A K线,不触网、不走 LLM);
#   ③单实例锁;④CLI 自带交易日守卫 + 幂等;⑤视图与全A K线自动探测主仓 data。
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
LOG="${STOCK_VETO_TRACK_LOG:-$HOME/.local/state/stock/veto_track_shadow.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/veto_track_shadow.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) veto_track_shadow ===================="
  # CLI 缺省 --date=今天 / --out-dir=data/shadow_forward/veto_track;交易日守卫 + 幂等。
  "$PY" -m tools.research.veto_track ${STOCK_DATA_ROOT:+--data-root "$STOCK_DATA_ROOT"}
  RC=$?
  echo "-- 当日 shadow 退出码 $RC --"
  # best-effort 回填历史 advisory 已到期的 D+1/D+2 绝对收益(幂等·只填 None cell),不影响主退出码。
  "$PY" -m tools.research.veto_track --backfill ${STOCK_DATA_ROOT:+--data-root "$STOCK_DATA_ROOT"} || echo "$(date) 警告:backfill 失败(不阻塞)"
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
