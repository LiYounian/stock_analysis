#!/bin/bash
# ④ 消息驱动板块拐点 forward-shadow 记录器(供 launchd 调用):工作日盘后(拟 20:30,情绪 shadow 之后)
#   在 regime 板块层序列上检测价量拐点 + 情绪拐点 → 给拐点板块成员候选 → 落
#   data/shadow_forward/sector_inflection/<日>.json。读同处的情绪 shadow(sentiment_shadow.sh 先跑)。
#
# ⚠️ 纪律:纯记录、non-gating、forward-only——**绝不进任何生产选股决策**;攒 ≥120 样本 + 命中显著才谈 validate。
#   agent 只备本脚本 + plist,`launchctl load` 由用户本人(约法)。依据:消息驱动板块拐点策略计划 §8b。
#
# 卫生:①常驻专用 dailyjob worktree 跑最新 origin/main;②纯行情/本地计算,**不走 LLM**(情绪 shadow 已由
#   sentiment_shadow.sh 落盘、本脚本只读);③单实例锁;④CLI 自带交易日守卫 + 幂等(已存在跳过)。
#   全A K线自动探测主仓 data(worktree data/master 软链→主仓)。
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
LOG="${STOCK_SECTOR_INFLECTION_LOG:-$HOME/.local/state/stock/sector_inflection_shadow.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/sector_inflection_shadow.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) sector_inflection_shadow ===================="
  # CLI 缺省 --date=今天 / --out-dir=data/shadow_forward/sector_inflection / --shadow-dir=data/shadow_forward/sentiment;
  # 自带交易日守卫(非交易日返 0 跳过)+ 幂等(当日已存在跳过)。全A K线自动探测主仓 data。
  "$PY" -m tools.research.sector_inflection ${STOCK_DATA_ROOT:+--data-root "$STOCK_DATA_ROOT"}
  RC=$?
  # 退出码:0=成功 / 非交易日跳过 / 幂等跳过;非0=全A K线为空(数据根/主档问题)。
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
