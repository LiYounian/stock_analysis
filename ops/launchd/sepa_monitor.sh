#!/bin/bash
# 本地 SEPA+VCP 监控(供 launchd / cron 调用)。
# 工作日 11:35 → session=午间;15:35 → session=收盘。也可显式传 午间|收盘。
# 会先 spot 增量当日 bar,再扫全 A(排除北交/B 股)。不跑 LLM、不绑全链路。
set -euo pipefail

ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
if [ ! -x "$PY" ]; then
  PY="${STOCK_PYTHON:-$REPO/.venv/bin/python3}"
fi

SESSION="${1:-}"
if [ -z "$SESSION" ]; then
  hour="$(date +%H)"
  if [ "$hour" -lt 13 ]; then
    SESSION="午间"
  else
    SESSION="收盘"
  fi
fi

mkdir -p "$REPO/logs"
exec "$PY" -m tools.run sepa --session "$SESSION"
