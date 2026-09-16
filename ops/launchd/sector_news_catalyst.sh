#!/bin/bash
# 消息驱动板块研判 · 每日增量(供 launchd 调用):工作日**下午 14:00**(不冲突时段)在主仓跑。
#
# 用户口径(2026-09-16):下午不冲突、每天增量——定向抓各板块龙头+板块近1-2周新闻,LLM 按 rubric
#   文字分级研判(不打分)、去重增量(旧新闻不重复分析)。产出 data/sector_news/catalyst_<date>.json
#   + 扩当日 sector_focus.json 的「消息驱动」块,供选股"利好板块→筛形态好的+反选剔除→深度分析"支撑路径。
#
# 卫生:① 研判输出型 → 主仓跑、写主仓 data(区别于 shadow worktree);② 真调 LLM → 解析网关凭证
#   (照 sentiment_shadow.sh:sync.env + 登录 shell 兜底);③ 单实例锁;④ 交易日守卫。
# **launchctl load 由用户本人(约法);本文件仅草案。** 前置:消息驱动代码需已在主仓 main。
set -uo pipefail

# —— ① LLM 凭证(rubric 文字研判需真 LLM)——
ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a
if [ -z "${LLM_API_KEY:-}" ]; then
  _LOGIN_SHELL="${STOCK_LOGIN_SHELL:-/bin/zsh}"
  eval "$("$_LOGIN_SHELL" -ic 'printf "export LLM_BASE_URL=%q\nexport LLM_API_KEY=%q\nexport LLM_MODEL=%q\n" "${LLM_BASE_URL:-}" "${LLM_API_KEY:-}" "${LLM_MODEL:-}"' 2>/dev/null || true)"
fi
export LLM_BASE_URL="${LLM_BASE_URL:-}" LLM_API_KEY="${LLM_API_KEY:-}" LLM_MODEL="${LLM_MODEL:-}"

MAIN="${STOCK_MAIN_REPO:-$HOME/Documents/projects/stock_analysis}"
cd "$MAIN" || { echo "$(date) 致命:主仓不存在 $MAIN" >&2; exit 3; }
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_SECTOR_NEWS_LOG:-$HOME/.local/state/stock/sector_news_catalyst.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/sector_news_catalyst.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"; exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

TODAY="$(date +%F)"
{
  echo "==================== $(date) sector_news_catalyst $TODAY ===================="
  if ! "$PY" -c "from tools.collectors import calendar as cal; import sys; sys.exit(0 if cal.is_trading_day('$TODAY') else 9)"; then
    echo "$TODAY 非交易日,跳过"; exit 0
  fi
  if [ -z "${LLM_API_KEY:-}" ]; then
    echo "!! LLM_API_KEY 为空(网关凭证未解析),rubric 研判无法跑;检查 $ENV_FILE 或登录 shell LLM_*" >&2
    exit 4
  fi
  "$PY" -m tools.analysis.sector_forecast.news_catalyst_run --date "$TODAY"
  RC=$?
  echo "-- 退出码 $RC (板块消息研判 → data/sector_news/catalyst_$TODAY.json) --"
  exit $RC
} >> "$LOG" 2>&1
