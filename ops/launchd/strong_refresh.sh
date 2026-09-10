#!/bin/bash
# 傍晚补跑(供 launchd 调用,工作日 20:00):Tushare 筹码 cyq_perf 傍晚才发布,daily(15:40)跑策略9 时
#   取不到→当天不出。此任务在筹码发布后单独重跑 S05最强选股 + 只补传「最强选股」这一个 view 分片
#   (不重传其它 300+ 分片,零外溢、省流量)。纯数值筹码,无 LLM,故不做 pull_refresh 的 LLM_* 兜底。
# 密钥只放本机受限文件、不进 git:从 $HOME/.config/stock/sync.env 读(chmod 600)。
set -uo pipefail

ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

# —— 专用 worktree 卫生:强制常驻 worktree 更到最新 origin/main 再跑(照 pull_refresh.sh)——
# 主仓常被并发会话弄脏/切走,就地跑会用陈旧代码或 ModuleNotFound;从专用 worktree 跑最新
# origin/main(每轮 fetch + reset --hard),全程不碰主仓 HEAD/工作树。可用 STOCK_DAILYJOB_WORKTREE 覆盖。
WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "!! ⓪ git fetch origin 失败,用该 worktree 现有 origin/main" >&2
_OLD_HEAD="$(git -C "$WORKTREE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
git -C "$WORKTREE" reset --hard origin/main
_NEW_HEAD="$(git -C "$WORKTREE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
REPO="$WORKTREE"
cd "$REPO"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
D="$(date +%Y-%m-%d)"
LOG="${STOCK_STRONG_LOG:-$HOME/.local/state/stock/strong_refresh.log}"
mkdir -p "$(dirname "$LOG")"
# 单实例锁:避免与上一轮重叠。A3 陈旧锁抢占(诊断 §6.2,同 pull_refresh):锁内写 PID+启动时间,
#   死持有者/超龄自动清理抢占,活实例不误抢;lib 缺失退回旧 mkdir 锁不硬失败。
LOCK="$HOME/.local/state/stock/strong_refresh.lock"
if [ -f "$REPO/ops/launchd/lib/lock.sh" ]; then
  . "$REPO/ops/launchd/lib/lock.sh"
  acquire_lock_or_exit "$LOCK" "$LOG"
else
  if ! mkdir "$LOCK" 2>/dev/null; then echo "$(date) 已有实例在跑,跳过" >> "$LOG"; exit 0; fi
  trap 'rmdir "$LOCK" 2>/dev/null' EXIT
fi

# A2 电源护栏(诊断 §6.1 RC-3.a):进程级防空闲/系统睡眠;仅进程级,不改系统电源设置、不碰 launchd。
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -m -s -w "$$" &
fi

{
  echo "==================== $(date) strong_refresh $D ===================="
  # ⓪ 代码已在脚本头部由专用 dailyjob worktree 卫生更到最新 origin/main(fetch + reset --hard),这里只记账:
  if [ "$_OLD_HEAD" = "$_NEW_HEAD" ]; then
    echo "-- ⓪ 专用 worktree 已是最新 origin/main($_NEW_HEAD),无需更新 --"
  else
    echo "-- ⓪ 专用 worktree 已更到最新 origin/main:$_OLD_HEAD -> $_NEW_HEAD --"
  fi
  # ① 重跑 S05 最强选股(全A,--no-fetch 读 daily 已落的主档;此时 Tushare 筹码 cyq_perf 已发布)。
  #    未配 TUSHARE_TOKEN / 仍取不到 → 写"需 Tushare"占位 view、不出(不崩)。
  echo "-- ① 重跑 S05 最强选股(全A,--no-fetch) --"
  "$PY" -m tools.run strong --no-fetch || echo "!! strong 失败"
  # ② 只补传「最强选股」单个 view 分片(不重传其它分片)。record 层不动、其它 view 不动,幂等替换该 view。
  echo "-- ② 只补传「最强选股」view 分片 --"
  "$PY" -m tools.sync.upload --date "$D" --only-view 最强选股 || echo "!! 补传失败"
  echo "==================== done $(date) ===================="
} >> "$LOG" 2>&1
