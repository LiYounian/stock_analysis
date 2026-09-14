#!/bin/bash
# Live capitulation 信号节点(供 launchd 调用):收盘后(拟 15:20,在 market_breadth 15:05 之后)
# 算当日全A广度是否触发"极端底部"(β 择时用),落 data/signals/capitulation/<日>.json,
# 供当晚 18:36 选股 / 次日环境判断读——识别到 capitulation → 放松整体降级、给市场 β 敞口。
#
# ⚠️ 草案:由统筹审阅后 load(本实现线不自换线)。依据:回测报告(H1-β 24/24 坐实,合入 main afd660a)。
# 纪律:收盘确定性信号,只门控 t+1,绝不盘中重算;判据 trailing-500 分位因果无未来。
#
# 与 market_breadth.sh 同款约定:①从专用 dailyjob worktree 跑最新 origin/main(每轮 fetch +
#   reset --hard),不碰主仓 HEAD;②纯行情/本地计算,无需 LLM_*;③单实例锁避免重叠。
set -uo pipefail

WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "!! ⓪ git fetch origin 失败,用现有 origin/main" >&2
git -C "$WORKTREE" reset --hard origin/main
REPO="$WORKTREE"
cd "$REPO"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_CAPITULATION_LOG:-$HOME/.local/state/stock/capitulation_signal.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/capitulation_signal.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) capitulation_signal ===================="
  # --data-root 缺省自动探测主仓(读全A K线);首跑自建 trailing 广度缓存(有界,秒~分钟级)。
  "$PY" -m tools.pipeline.capitulation_signal ${STOCK_DATA_ROOT:+--data-root "$STOCK_DATA_ROOT"}
  RC=$?
  # 退出码:0=成功 / 非交易日跳过 / 幂等跳过;非0=广度序列为空(数据根/票池问题,下游按缺文件降级)
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
