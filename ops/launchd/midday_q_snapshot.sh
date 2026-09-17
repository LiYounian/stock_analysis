#!/bin/bash
# 午盘 Q 专用快照(供 launchd 调用):在指定 slot 抓**焦点池 126 只**的实时行情快照。
#
# ## 为什么不直接用 intraday_snapshot.sh(2026-09-16 定案)
#
# 共用的 `intraday_snapshot.sh` 是裸调 `--slot`,不传 `--codes` → 落到
# `intraday_snapshot.resolve_targets()` 的默认标的:「上一交易日选股 ∪ 自选池」(约 18 只)。
# 那是"跟踪已知标的"的设计,而午盘 Q 需要的是"扫固定焦点池找新候选",两者语义不同。
#
# 实证(2026-09-16):不传 --codes 的 1430 快照只覆盖焦点池 6/126(4.8%)→ 选股结果无效,
# 且当时首判的空集会顺着 confirm_from 锁死 1450 复核(已在 midday_q_screen 侧加覆盖率闸门兜底)。
#
# 按 M1 契约稿 §一的既定分工:**共用采集脚本零改**,午盘 Q 只新增自己的 launchd 触发/wrapper,
# 用参数(--codes)把标的说清楚。故有本文件。
#
# ## slot 语义(INTRADAY_SLOT 环境变量,plist 里指定)
#   1030 → Q1 AmPmRatio 的**上午量能基准**(midday_q_screen._build_extras 读 T1030.json)
#          缺这份时 am_pm_vol_ratio() 降级用行情源 vol_ratio,实测 94% 的票都能过阈值 → 形同虚设
#   1430 → 首判(gate.stage1_1430 + screen.stage1_1430 的数据源)
#   1450 → 复核(gate.stage2_1450 + screen.stage2_1450 的数据源)
#
# 代码源/worktree 卫生/单实例锁 均照 intraday_snapshot.sh 同款(见该文件注释)。
# data/intraday 在 worktree 内是指向主仓的 symlink,快照落主仓共享目录。
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
SLOT="${INTRADAY_SLOT:?必须指定 INTRADAY_SLOT(1030/1430/1450)}"
LOG="${STOCK_MIDDAY_Q_SNAPSHOT_LOG:-$HOME/.local/state/stock/midday_q_snapshot.log}"
mkdir -p "$(dirname "$LOG")"

# 单实例锁按 slot 分开:1430/1450 相隔 20 分钟,互不该挤掉对方
LOCK="$HOME/.local/state/stock/midday_q_snapshot_${SLOT}.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) slot=$SLOT 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) midday_q_snapshot slot=$SLOT ===================="
  # 焦点池代码从**单一真源** config/midday_q_universe.json 现取(不在 wrapper 里硬编码 126 个代码,
  # 否则票池调整后 wrapper 会静默用旧池)。取不到 → 直接失败,不退化成默认 18 只标的。
  CODES="$("$PY" -c 'from tools.config import midday_q_universe as U; print(",".join(U.get_focus_codes()))')"
  if [ -z "$CODES" ]; then
    echo "致命:焦点池代码取空(config/midday_q_universe.json 缺失或格式变更)" >&2
    echo "-- 退出码 4 --"
    exit 4
  fi
  echo "焦点池 $(echo "$CODES" | tr ',' '\n' | wc -l | tr -d ' ') 只"
  "$PY" -m tools.pipeline.intraday_snapshot --slot "$SLOT" --codes "$CODES"
  RC=$?
  # 退出码:0=成功/非交易日跳过/幂等跳过;非0=全部标的抓取失败(下游按缺快照降级)
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
