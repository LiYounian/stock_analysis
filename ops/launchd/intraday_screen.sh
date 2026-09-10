#!/bin/bash
# 午盘全A选股(供 launchd 调用):工作日 11:32 触发,午休(11:30 冻结口径)对全A现挑票。
#   拉全A → 内存注入午盘 bar → 数据初筛(裁策略11)→ shortlist → 消息面三段式(阶段2 走 LLM)
#   → 产 docs/每日分析/选股/日内全A_<date>.md(与「盯已选票」的盯盘研判并存,不覆盖 日内_<date>.md)。
#
# 代码源:从常驻专用 worktree 跑最新 origin/main(与 intraday_snapshot.sh / pull_refresh.sh 同款卫生)。
#   **不从主仓跑**——主仓工作树被并发会话卡在旧 commit 时,新模块会 ModuleNotFound;
#   专用 worktree 是独立工作副本、动它的 git 不碰主仓 HEAD/WIP,午休时点也安全。
# 阶段2 消息面精选需 LLM:密钥只放本机受限文件、不进 git,从 $HOME/.config/stock/sync.env 读(chmod 600)。
#   launchd 读不到 ~/.zshrc,LLM_* 常是别名(间接引用),故加登录 shell 兜底解析整条链(照 pull_refresh.sh)。
# 单实例锁避免与上一轮重叠。退出码:0=成功或非交易日/幂等跳过;非0=pipeline 失败。
set -uo pipefail

# —— ① LLM 凭证(阶段2 消息面精选需要;纯数据段不需要,但调度态默认带 LLM)——
ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
if [ -z "${LLM_API_KEY:-}" ]; then
  _LOGIN_SHELL="${STOCK_LOGIN_SHELL:-/bin/zsh}"
  eval "$("$_LOGIN_SHELL" -ic 'printf "export LLM_BASE_URL=%q\nexport LLM_API_KEY=%q\nexport LLM_MODEL=%q\n" "${LLM_BASE_URL:-}" "${LLM_API_KEY:-}" "${LLM_MODEL:-}"' 2>/dev/null || true)"
fi
export LLM_BASE_URL="${LLM_BASE_URL:-}" LLM_API_KEY="${LLM_API_KEY:-}" LLM_MODEL="${LLM_MODEL:-}"

# —— ② 专用 worktree 卫生:强制常驻 worktree 更到最新 origin/main 再跑(照 intraday_snapshot.sh)——
WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "$(date) 警告:git fetch origin 失败,用该 worktree 现有 origin/main" >&2
git -C "$WORKTREE" reset --hard origin/main >/dev/null 2>&1 || echo "$(date) 警告:reset --hard origin/main 失败,用 worktree 当前代码" >&2
REPO="$WORKTREE"
cd "$REPO"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_INTRADAY_SCREEN_LOG:-$HOME/.local/state/stock/intraday_screen.log}"
mkdir -p "$(dirname "$LOG")"

# 单实例锁:避免与上一轮重叠。A3 陈旧锁抢占(诊断 §6.2,同 pull_refresh):锁内写 PID+启动时间,
#   死持有者/超龄自动清理抢占,活实例不误抢;lib 缺失退回旧 mkdir 锁不硬失败。
LOCK="$HOME/.local/state/stock/intraday_screen.lock"
if [ -f "$REPO/ops/launchd/lib/lock.sh" ]; then
  . "$REPO/ops/launchd/lib/lock.sh"
  acquire_lock_or_exit "$LOCK" "$LOG"
else
  if ! mkdir "$LOCK" 2>/dev/null; then
    echo "$(date) 已有实例在跑,跳过" >> "$LOG"
    exit 0
  fi
  trap 'rmdir "$LOCK" 2>/dev/null' EXIT
fi

# A2 电源护栏(诊断 §6.1 RC-3.a):进程级防空闲/系统睡眠;仅进程级,不改系统电源设置、不碰 launchd。
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -m -s -w "$$" &
fi

{
  echo "==================== $(date) intraday_screen ===================="
  "$PY" -m tools.run intraday_screen "$@"
  RC=$?
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
