#!/bin/bash
# 板块预测系统 · 每日研判产出(供 launchd 调用):工作日盘后,在**主仓**跑 P1+P2 全流水。
#
# 产出(落主仓 data,供人工/选股研判辅助 —— 非 shadow、要能被看到):
#   data/analysis/<date>/sector_regime.json   板块环境面板(过冷/过热/拐点 + 拥挤/动量/广度)
#   data/sector_roster/<板块>.json            四角色主表(龙头/中军/补涨/弹性)
#   data/analysis/<date>/sector_focus.json    大盘→板块两步(重点/规避板块池 + 宏观情景)
#   data/analysis/<date>/sector_daily.md      每日板块定向分析文档
#   data/analysis/<date>/market_daily.md      每日市场综合研判(宏观+大盘+重点板块)
#   data/sector_news/<date>.json              独立新闻库(宏观指标真采 + 净方向/情景)
#
# ⚠️ 定位(2026-09-16 定案):板块系统 = **人工/研判辅助**,不机械接入生产选股(双跑证伪粗暴自动接)。
#   本任务纯产出研判文档,不 gate 任何选股决策。
# 卫生:① 研判输出型 → 跟 market_breadth 一样在**主仓**跑、写主仓 data(区别于 shadow 的 worktree 模式);
#   前置依赖 pullrefresh(收盘刷数据 + sentiment_policy)已完成,故排在其后;② 单实例锁;③ 交易日守卫;
#   ④ 无需 LLM(纯 Python + akshare 宏观取数)。**launchctl load 由用户本人(约法);本文件仅草案。**
set -uo pipefail

MAIN="${STOCK_MAIN_REPO:-$HOME/Documents/projects/stock_analysis}"
cd "$MAIN" || { echo "$(date) 致命:主仓不存在 $MAIN" >&2; exit 3; }
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_SECTOR_DAILY_LOG:-$HOME/.local/state/stock/sector_daily.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/sector_daily.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

TODAY="$(date +%F)"
{
  echo "==================== $(date) sector_daily $TODAY ===================="
  if ! "$PY" -c "from tools.collectors import calendar as cal; import sys; sys.exit(0 if cal.is_trading_day('$TODAY') else 9)"; then
    echo "$TODAY 非交易日,跳过"; exit 0
  fi
  "$PY" -m tools.analysis.sector_forecast --date "$TODAY"
  RC=$?
  echo "-- 退出码 $RC (板块研判 → data/analysis/$TODAY/ + data/sector_roster/ + data/sector_news/) --"
  exit $RC
} >> "$LOG" 2>&1
