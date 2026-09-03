#!/bin/bash
# 全市场收盘口径节点(供 launchd 调用):收盘后 5 分钟(15:05)算当日全A等权/中位/广度/分位落盘,
# 供盘尾复盘直接读作 α 记分基准,不必再现场对 5000+ 只票临时复算。
#
# 为什么需要 OS 级定时:α 记分的基准是"当日收盘的全市场口径",而 market_forecast.json 要等
# 18:36 选股任务之后才有、且里面的广度是"当日之前"的特征;09-03 盘尾只能现场发约 105 个批请求
# 临时复算(耗时且不可复现)。把"收盘后取一次全市场口径"这件确定性的事下沉到代码 + launchd,
# 判断部分才留给会话。见 docs/计划/09-03复盘反哺排期.md §2 与 docs/计划/全市场收盘口径_确定性节点.md
#
# 与 intraday_snapshot.sh 同款约定:①不做 git ff-only 自更(主仓工作树可能正被其它会话使用);
# ②无需 LLM_*(纯行情抓取);③仓库路径由脚本自身位置推出(ops/launchd/ 上两级),不硬编用户名。
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
SLOT="${BREADTH_SLOT:-1505}"
LOG="${STOCK_BREADTH_LOG:-$HOME/.local/state/stock/market_breadth.log}"
mkdir -p "$(dirname "$LOG")"

# 单实例锁:避免与上一轮重叠(与 pull_refresh.sh / intraday_snapshot.sh 同款 mkdir 原子锁)
LOCK="$HOME/.local/state/stock/market_breadth.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) market_breadth slot=$SLOT ===================="
  "$PY" -m tools.pipeline.market_breadth --slot "$SLOT"
  RC=$?
  # 退出码语义:0=成功 / 非交易日跳过 / 幂等跳过(文件已存在);
  #             非0=票池为空或全部标的取数失败(此时不落文件,盘尾复盘按缺文件降级为现场复算)
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
