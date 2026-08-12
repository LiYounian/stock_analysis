#!/bin/bash
# 本地盘后闭环(供 launchd 调用):本地自采全A K线 → 全A多策略选股(screenall)→ 签名上传。
# 改为**本地自采**(ops.remote_fetch,fqkline ~5min)而非 pull 远端:远端 tencent 限速~2h、
# 且 pull 会抢在远端采集完成前 → 拿到旧/半截数据。本地 fqkline 当日盘后即含收盘价,快且稳。
# 远端只负责展示(web 读上传的产物)。
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
  echo "-- ① 本地自采全A K线(fqkline,~5min;不依赖慢远端 pull,规避时序竞争) --"
  # ops.remote_fetch = 全A主档同步(spot增量→失败回退 fqkline 逐只 + >= 推进主档;含交易日守卫)。
  # 本地跑=本地全A自采:fqkline 当日盘后即含收盘价,比"pull 远端(远端 tencent 限速~2h)"快且稳。
  FETCH_WORKERS="${FETCH_WORKERS:-10}" "$PY" -m ops.remote_fetch || echo "!! 本地全A采集失败(继续用本地已有)"
  echo "-- ② 全A多策略选股(策略0/1/2/3/4)+ 对(选出并集∪自选)做新闻/LLM/合议 --"
  # --no-fetch:pull 已把全A落主档,screenall 不再触发 master_sync 回填/重采
  "$PY" -m tools.run screenall --no-fetch || echo "!! screenall 失败"
  echo "-- ③ 上传远端(先不带 --force:只补未确认分片,规避 ingest 429 限速) --"
  "$PY" -m tools.sync.upload --date "$D" || echo "!! 上传第一轮"
  sleep 65   # 限速窗口(120/60s);分片>120 时首轮部分 429,等窗口重置补齐
  "$PY" -m tools.sync.upload --date "$D" || echo "!! 上传补齐"
  echo "==================== done $(date) ===================="
} >> "$LOG" 2>&1
