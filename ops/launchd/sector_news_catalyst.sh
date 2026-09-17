#!/bin/bash
# 消息驱动板块研判 · 每日(供 launchd 调用):工作日**下午 14:00**(不冲突时段)在主仓跑。
#
# 【2026-09-16 升级·程序化流水线 S3→S4 链】(统筹调度选 A·重指向本 sh·plist 路径不变→下个14:00自动跑升级链):
#   S3 board_news_collect_run:读 S2 角色关系表(全板块含主力·缺回退每日 roster)→ 逐板块定向抓
#     龙头/中军/主力新闻 + 板块政策 + 国际对标 + LHB资金流 → data/sector_news/raw/<date>/<板块>.json。**只抓不判·无 LLM**。
#   S4 board_verdict_run:读 S3 raw → 逐板块 DeepSeek-v4-pro(思考关·统筹A/B择优)按 rubric 文字分级研判(不打分)+
#     去重增量 → data/sector_news/catalyst_<date>.json(全板块)+ 扩当日 sector_focus.json「消息驱动」块,
#     供选股"利好板块→筛形态好的+反选剔除→深度分析"支撑路径。
#   (retire 内部耦合的 news_catalyst_run:采集与研判解耦为 S3/S4 两阶段·全板块覆盖。)
#
# 卫生:① 研判输出型 → 主仓跑、写主仓 data(区别于 shadow worktree);② 真调 LLM(S4)→ 解析网关凭证
#   (照 sentiment_shadow.sh:sync.env + 登录 shell 兜底);③ 单实例锁;④ 交易日守卫。
# **launchctl load 由用户本人(约法)。** 前置:S1-S4 代码已在主仓 main。
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
    echo "!! LLM_API_KEY 为空(网关凭证未解析),S4 rubric 研判无法跑;检查 $ENV_FILE 或登录 shell LLM_*" >&2
    exit 4
  fi
  # —— S3 采集(无 LLM):读 S2 表 → raw 过程文件 ——
  "$PY" -m tools.analysis.sector_forecast.board_news_collect_run --date "$TODAY"
  RC3=$?
  echo "-- S3 退出码 $RC3 (定向采集 → data/sector_news/raw/$TODAY/) --"
  # —— S4 研判(DeepSeek):读 S3 raw → catalyst + 扩 sector_focus。S3 未产 raw 时 S4 自会非0退出 ——
  "$PY" -m tools.analysis.sector_forecast.board_verdict_run --date "$TODAY"
  RC4=$?
  echo "-- S4 退出码 $RC4 (DeepSeek研判 → data/sector_news/catalyst_$TODAY.json + 扩 sector_focus) --"
  # 整体退出码:S4 为准(最终产物),S3 失败但 S4 靠既有 raw 仍可成时不误杀
  exit $RC4
} >> "$LOG" 2>&1
