#!/bin/bash
# 本地盘后闭环(供 launchd 调用):从远端 pull 全A K线 → 全A策略0/1扫描 + 自选池流水线 → 签名上传。
# 依赖远端 stock-fetch 已在盘后把全A采到当天(见 ops/systemd/stock-fetch.*)。
# 密钥只放本机受限文件、不进 git:默认从 $HOME/.config/stock/sync.env 读(chmod 600)。
# 仓库路径由脚本自身位置推出(ops/launchd/ 上两级),无需硬编用户名/绝对路径。
set -uo pipefail

ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

# LLM_* 兜底:launchd 读不到 ~/.zshrc,而 LLM_* 常是别名(间接引用),让用户登录 shell 解析整条链喂进来。
if [ -z "${LLM_API_KEY:-}" ]; then
  _LOGIN_SHELL="${STOCK_LOGIN_SHELL:-/bin/zsh}"
  eval "$("$_LOGIN_SHELL" -ic 'printf "export LLM_BASE_URL=%q\nexport LLM_API_KEY=%q\nexport LLM_MODEL=%q\n" "${LLM_BASE_URL:-}" "${LLM_API_KEY:-}" "${LLM_MODEL:-}"' 2>/dev/null || true)"
fi
export LLM_BASE_URL="${LLM_BASE_URL:-}" LLM_API_KEY="${LLM_API_KEY:-}" LLM_MODEL="${LLM_MODEL:-}"

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
D="$(date +%Y-%m-%d)"
LOG="${STOCK_PULL_LOG:-$HOME/.local/state/stock/pull_refresh.log}"
mkdir -p "$(dirname "$LOG")"
# 单实例锁:避免与上一轮重叠
LOCK="$HOME/.local/state/stock/pull_refresh.lock"
if ! mkdir "$LOCK" 2>/dev/null; then echo "$(date) 已有实例在跑,跳过" >> "$LOG"; exit 0; fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) pull_refresh $D ===================="
  echo "-- ① pull 全A K线(远端增量) --"
  "$PY" -m tools.sync.pull --kind kline || echo "!! pull 失败(继续用本地已有)"
  echo "-- ② 策略0 全A合议 --"
  "$PY" -m tools.pipeline.screen_council --date "$D" --no-fetch || echo "!! 策略0 失败"
  echo "-- ③ 策略1 全A深跌反包 --"
  "$PY" -m tools.pipeline.screen_s01 --date "$D" --no-fetch || echo "!! 策略1 失败"
  echo "-- ④ 自选池全链路(新闻/情绪/合议/策略2-4数据) --"
  "$PY" -m tools.run all --all || echo "!! 自选池流水线 失败"
  echo "-- ⑤ 上传远端 --"
  "$PY" -m tools.sync.upload --date "$D" --force || echo "!! 上传 失败"
  echo "==================== done $(date) ===================="
} >> "$LOG" 2>&1
