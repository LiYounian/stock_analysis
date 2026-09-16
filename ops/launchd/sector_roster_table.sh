#!/bin/bash
# S2 · 角色关系表 周度维护(供 launchd 调用):每周一盘后(排在 S1 之后),在**主仓**跑 sector_roster_table_run。
#
# 产出(落主仓 data;周度重算派生量价过程文件,已 gitignore,本地可读):
#   data/sector_roster_table/<板块>.json                  五角色(龙头/中军/先锋/弹性/主力)+ 变更历史
#   data/sector_roster_table/changes/roster_changes_<周>.md 周度变更过程文件
#
# 定位:程序化流水线 S2(纯量价 + 龙虎榜净买 proxy;无 LLM)。读 S1 sector_universe.json 全板块清单。
# 主力口径:东财主力净流入被墙/北向停披露 → LHB 龙虎榜净买(PIT·T+1)作大资金 proxy,诚实标覆盖度。
# 卫生:① 主仓跑写主仓 data;② 单实例锁;③ 排在 S1(18:00)之后(18:30),确保读到本周 universe;
#   ④ 非交易日由 runner 回退最近交易日;⑤ 无需 LLM;⑥ 保留每日 roles 版(data/sector_roster/)不动。
#   **launchctl load 由用户本人(约法);本文件仅草案(enabled:false)。**
set -uo pipefail

MAIN="${STOCK_MAIN_REPO:-$HOME/Documents/projects/stock_analysis}"
cd "$MAIN" || { echo "$(date) 致命:主仓不存在 $MAIN" >&2; exit 3; }
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_SECTOR_ROSTER_TABLE_LOG:-$HOME/.local/state/stock/sector_roster_table.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/sector_roster_table.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) sector_roster_table(S2) ===================="
  "$PY" -m tools.analysis.sector_forecast.sector_roster_table_run
  RC=$?
  echo "-- 退出码 $RC (S2 角色关系表 → data/sector_roster_table/ + changes/) --"
  exit $RC
} >> "$LOG" 2>&1
