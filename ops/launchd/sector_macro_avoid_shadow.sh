#!/bin/bash
# C 宏观/板块级消息规避 forward-shadow 记录器(供 launchd 调用):工作日盘后(拟 20:45,情绪20:00·④20:30 之后)
#   把当日 sentiment_policy.json 里命中三主题(美联储加息/利率·汇率(人民币)·关税/出口管制)的消息按
#   industries rollup 到申万一级 → 板块级宏观净方向+强度 → 对宏观利空板块打规避标记(叠 thermometer
#   拥挤/动量确认)→ 落 data/shadow_forward/sector_macro_avoid/<日>.json。
#
# ⚠️ 纪律:纯记录、non-gating、forward-only——**绝不进任何生产选股决策**;攒 ≥120 样本 + 命中显著才谈 validate。
#   agent 只备本脚本 + plist,`launchctl load` 由用户本人(约法)。依据:选股规避能力整改诊断与回测设计 §2C。
#
# 依赖顺序:读当日 sentiment_policy.json,须排在盘后闭环(采集 sentiment_policy)之后;不走 LLM。
# 卫生:①常驻专用 dailyjob worktree 跑最新 origin/main;②纯本地计算(消息/板块指数/全A K线,不触网、不走 LLM);
#   ③单实例锁;④CLI 自带交易日守卫 + 幂等(已存在跳过);⑤sentiment_policy 与全A K线自动探测主仓 data。
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
LOG="${STOCK_SECTOR_MACRO_AVOID_LOG:-$HOME/.local/state/stock/sector_macro_avoid_shadow.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/sector_macro_avoid_shadow.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) sector_macro_avoid_shadow ===================="
  # CLI 缺省 --date=今天 / --out-dir=data/shadow_forward/sector_macro_avoid;自带交易日守卫(非交易日返0跳过)
  # + 幂等(当日已存在跳过)。sentiment_policy 与全A/板块 K线自动探测主仓 data。
  "$PY" -m tools.research.sector_macro_avoid ${STOCK_DATA_ROOT:+--data-root "$STOCK_DATA_ROOT"}
  RC=$?
  # 退出码:0=成功 / 非交易日跳过 / 幂等跳过;非0=当日无 sentiment_policy(数据根/闭环问题)。
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
