#!/bin/bash
# P-B 情绪判官 forward-shadow 记录器(供 launchd 调用):工作日盘后(拟 20:00,闭环 pullrefresh 之后)
#   LLM 判每个申万一级行业情绪 A/B + 净A度 → 落 data/shadow_forward/sentiment/<日>.json。
#
# ⚠️ 纪律:纯记录、non-gating、forward-only——**绝不进任何生产选股决策**;攒 ≥120 独立样本才谈 validate。
#   agent 只备本脚本 + plist,`launchctl load` 由用户本人(约法)。依据:整合换线pass计划 §2D。
#
# 卫生:①常驻专用 dailyjob worktree 跑最新 origin/main(fetch + reset --hard),不碰主仓 HEAD;
#   ②情绪判官走 LLM 网关(照 intraday_deep.sh:sync.env + 登录 shell 兜底解析 LLM_*);
#   ③单实例锁避免重叠。shadow 落 dailyjob worktree 本地 data/shadow_forward(gitignored,reset 不动)。
set -uo pipefail

# —— ① LLM 凭证(情绪判官逐行业 LLM 判定需)——
ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
if [ -z "${LLM_API_KEY:-}" ]; then
  _LOGIN_SHELL="${STOCK_LOGIN_SHELL:-/bin/zsh}"
  eval "$("$_LOGIN_SHELL" -ic 'printf "export LLM_BASE_URL=%q\nexport LLM_API_KEY=%q\nexport LLM_MODEL=%q\n" "${LLM_BASE_URL:-}" "${LLM_API_KEY:-}" "${LLM_MODEL:-}"' 2>/dev/null || true)"
fi
export LLM_BASE_URL="${LLM_BASE_URL:-}" LLM_API_KEY="${LLM_API_KEY:-}" LLM_MODEL="${LLM_MODEL:-}"

# —— ② 专用 worktree 卫生 ——
WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "$(date) 警告:git fetch origin 失败,用现有 origin/main" >&2
git -C "$WORKTREE" reset --hard origin/main >/dev/null 2>&1 || echo "$(date) 警告:reset --hard 失败,用 worktree 当前代码" >&2
cd "$WORKTREE"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_SENTIMENT_SHADOW_LOG:-$HOME/.local/state/stock/sentiment_shadow.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/sentiment_shadow.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

TODAY="$(date +%F)"
OUT_DIR="data/shadow_forward/sentiment"

{
  echo "==================== $(date) sentiment_shadow $TODAY ===================="
  # 交易日守卫:非交易日跳过(不浪费 LLM 调用、不落陈旧文本 shadow)。
  if ! "$PY" -c "from tools.collectors import calendar as cal; import sys; sys.exit(0 if cal.is_trading_day('$TODAY') else 9)"; then
    echo "$TODAY 非交易日,跳过"; exit 0
  fi
  if [ -z "${LLM_API_KEY:-}" ]; then
    echo "!! LLM_API_KEY 为空(网关凭证未解析),情绪判官无法跑;检查 $ENV_FILE 或登录 shell LLM_*" >&2
    exit 4
  fi
  "$PY" -m tools.analysis.industry_temp.sentiment_judge --date "$TODAY" --out-dir "$OUT_DIR"
  RC=$?
  echo "-- 退出码 $RC (shadow → $OUT_DIR/$TODAY.json) --"
  exit $RC
} >> "$LOG" 2>&1
