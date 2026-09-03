#!/bin/bash
# 盘中定时快照(供 launchd 调用):在真实 10:30 抓一次实时行情快照落盘,
# 使"早盘偏离核实"不依赖 Claude 定时会话何时醒。
#
# 为什么需要 OS 级定时:后台定时会话在桌面 App 非活动时工具调用会被挂起数小时才执行
# (实测盘中任务 10:34 触发、13:50 才跑第一条命令),取数时点因此严重漂移。把"在正确时点
# 取数"这件确定性的事下沉到代码 + launchd,判断部分才留给会话。见 docs/计划/定时任务时序治理.md
#
# 与 pull_refresh.sh 的差异:①本脚本**不做 git ff-only 自更**——10:30 是盘中,主仓工作树
# 可能正被其它会话使用,盘中动 git 风险大于收益;快照逻辑刻意保持简单稳定。②无需 LLM_*
# (纯行情抓取,不调 LLM),故不解析登录 shell 的密钥链。
# 仓库路径由脚本自身位置推出(ops/launchd/ 上两级),不硬编用户名/绝对路径。
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
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
