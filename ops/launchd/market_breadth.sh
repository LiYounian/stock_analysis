#!/bin/bash
# 全市场收盘口径节点(供 launchd 调用):收盘后 5 分钟(15:05)算当日全A等权/中位/广度/分位落盘,
# 供盘尾复盘直接读作 α 记分基准,不必再现场对 5000+ 只票临时复算。
#
# 为什么需要 OS 级定时:α 记分的基准是"当日收盘的全市场口径",而 market_forecast.json 要等
# 18:36 选股任务之后才有、且里面的广度是"当日之前"的特征;09-03 盘尾只能现场发约 105 个批请求
# 临时复算(耗时且不可复现)。把"收盘后取一次全市场口径"这件确定性的事下沉到代码 + launchd,
# 判断部分才留给会话。见 docs/计划/09-03复盘反哺排期.md §2 与 docs/计划/全市场收盘口径_确定性节点.md
#
# 与 pull_refresh.sh / intraday_snapshot.sh 同款约定:①从专用 dailyjob worktree 跑最新
#   origin/main(每轮 fetch + reset --hard origin/main),不碰主仓 HEAD/工作树——主仓常被
#   并发会话弄脏/切走,就地跑会用陈旧代码或 ModuleNotFound;②无需 LLM_*(纯行情抓取)。
set -uo pipefail

# —— 专用 worktree 卫生:强制常驻 worktree 更到最新 origin/main 再跑(照 pull_refresh.sh)——
# 可用 STOCK_DAILYJOB_WORKTREE 覆盖路径。fetch 走该 worktree 的 git(与主仓共享对象库,不动主仓)。
WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "!! ⓪ git fetch origin 失败,用该 worktree 现有 origin/main" >&2
git -C "$WORKTREE" reset --hard origin/main
REPO="$WORKTREE"
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
