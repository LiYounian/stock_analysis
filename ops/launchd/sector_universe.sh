#!/bin/bash
# S1 · 板块 universe 周度维护(供 launchd 调用):每周一盘后,在**主仓**跑 sector_universe_run。
#
# 产出(落主仓 data;周度重算派生量价过程文件,已 gitignore,本地可读):
#   data/analysis/sector_universe.json                全申万一级板块清单 + 热门标记 + 与上周 diff
#   data/analysis/sector_universe_history/<date>.json 周度归档(diff 基线留痕)
#
# 定位:程序化流水线 S1(纯量价、无 LLM)。周维护"当前该有哪些板块 + 哪些热门"。
# 卫生:① 研判/过程文件型 → 与 sector_daily 一样在**主仓**跑、写主仓 data;② 单实例锁;
#   ③ 非交易日由 runner 自动回退到 ≤当日最近交易日(不在此硬退);④ 无需 LLM。
#   **launchctl load 由用户本人(约法);本文件仅草案(enabled:false)。**
set -uo pipefail

MAIN="${STOCK_MAIN_REPO:-$HOME/Documents/projects/stock_analysis}"
cd "$MAIN" || { echo "$(date) 致命:主仓不存在 $MAIN" >&2; exit 3; }
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_SECTOR_UNIVERSE_LOG:-$HOME/.local/state/stock/sector_universe.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/sector_universe.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) sector_universe(S1) ===================="
  "$PY" -m tools.analysis.sector_forecast.sector_universe_run
  RC=$?
  echo "-- 退出码 $RC (S1 板块universe → data/analysis/sector_universe.json + history) --"
  exit $RC
} >> "$LOG" 2>&1
