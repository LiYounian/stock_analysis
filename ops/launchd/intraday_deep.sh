#!/bin/bash
# headless 午盘深度选股(供 launchd 调用):工作日 ~11:50 触发,读 intraday_screen 阶段1早产候选
#   → 自采 Top-N 消息面(≤11:30 防未来)→ deep_analysis 逐票 DeepSeek 深度研判 → write_picks 校验
#   → 产 docs/每日分析/选股/日内深度_<date>.md + data/analysis/<date>/日内深度选股.json
#     (灰度独立名,不碰 日内_<date>.md / 每日选股.json / intraday_watch)。**13:00 开盘前出、免漂移。**
#
# 方案b(纯 Python orchestrator,无 Claude 窗口)。设计:
#   docs/计划/2026-09-14_headless午盘深度选股_launchd免漂移_设计.md
# 代码源:常驻专用 worktree 跑最新 origin/main(与 intraday_screen.sh 同款卫生、同 dailyjob worktree,
#   故读同一 data/intraday/<date>/ 候选)。深度研判需 LLM:密钥只放本机受限文件、不进 git,
#   从 $HOME/.config/stock/sync.env 读(chmod 600);launchd 读不到 ~/.zshrc,LLM_* 常是别名,
#   故加登录 shell 兜底解析整条链(照 intraday_screen.sh,DeepSeek 网关凭证唯一途径)。
# 单实例锁避免与上一轮重叠。退出码:0=成功或非交易日/幂等跳过/无买入候选;非0=pipeline 失败。
set -uo pipefail

# —— ① LLM 凭证(deep_analysis 逐票 DeepSeek + 自采三层情绪均需)——
ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
if [ -z "${LLM_API_KEY:-}" ]; then
  _LOGIN_SHELL="${STOCK_LOGIN_SHELL:-/bin/zsh}"
  eval "$("$_LOGIN_SHELL" -ic 'printf "export LLM_BASE_URL=%q\nexport LLM_API_KEY=%q\nexport LLM_MODEL=%q\n" "${LLM_BASE_URL:-}" "${LLM_API_KEY:-}" "${LLM_MODEL:-}"' 2>/dev/null || true)"
fi
export LLM_BASE_URL="${LLM_BASE_URL:-}" LLM_API_KEY="${LLM_API_KEY:-}" LLM_MODEL="${LLM_MODEL:-}"

# —— ② 专用 worktree 卫生:与 intraday_screen.sh 共用同一 dailyjob worktree(读同一 data/intraday)——
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
LOG="${STOCK_INTRADAY_DEEP_LOG:-$HOME/.local/state/stock/intraday_deep.log}"
mkdir -p "$(dirname "$LOG")"

# 单实例锁:避免与上一轮重叠(陈旧锁抢占,同 intraday_screen);lib 缺失退回旧 mkdir 锁不硬失败。
LOCK="$HOME/.local/state/stock/intraday_deep.lock"
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

# 电源护栏:进程级防空闲/系统睡眠(仅进程级,不改系统电源/不碰 launchd)。
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -m -s -w "$$" &
fi

# 生产口径:自采 Top-N 消息面(默认开)、≤11:30 防未来 cutoff(默认开)、think 关(省时延)、Top-5、stage1。
# 灰度期产 日内深度_<date>.md(不接 watch)。参数可经 plist 追加覆盖(经 "$@" 透传)。
{
  echo "==================== $(date) intraday_deep ===================="
  "$PY" -m tools.run intraday_deep "$@"
  RC=$?
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
