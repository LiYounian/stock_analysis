#!/bin/bash
# 本地盘后闭环(供 launchd 调用):[①全A自采K线·默认关] → 全A多策略选股(screenall --no-fetch,
#   含 M2 财报) → 前瞻记分卡 → 签名上传。远端只负责展示(web 读上传的产物)。
# **①自采 K 线由 PULL_FETCH 控制(launchd plist 已设 PULL_FETCH=1 开启)**。
#   历史根因订正(2026-08-20 实测):ops.remote_fetch 崩溃**不是内存吃紧**,而是**多进程 fork + V8**——
#   mini_racer(V8)在 FETCH_WORKERS>1 的 fork 子进程里重复初始化,触发 PartitionAlloc 致命检查
#   (address_pool_manager.cc `!pool->IsInitialized()`,SIGTRAP/退出码133)。**FETCH_WORKERS=1 单进程即不崩**
#   (实测全A 5548 只跑通、退出码0、峰值414MB、耗时~73min,主档正常推进到当日)。故本脚本 ① 默认单进程。
#   spot 增量偶发网络断连会自动回退逐只(腾讯/新浪)推进主档,当日盘后即含收盘价。
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
  # ① 本地自采全A K线(PULL_FETCH=1 开启;plist 已设)。**必须单进程 FETCH_WORKERS=1**——
  #    多进程会触发 mini_racer/V8 的 PartitionAlloc 崩溃(见头部根因订正);单进程实测跑通不崩。
  #    ops.remote_fetch = spot增量→回退逐只(腾讯/新浪)→抓完 _advance_master_from_raw 推进主档。
  #    代价:串行 ~73min(盘后时间充裕,排在②前)。
  if [ "${PULL_FETCH:-0}" = "1" ]; then
    echo "-- ① 本地自采全A K线(PULL_FETCH=1,单进程 ~73min) --"
    FETCH_WORKERS="${FETCH_WORKERS:-1}" "$PY" -m ops.remote_fetch || echo "!! 本地全A采集失败(继续用本地已有)"
  else
    echo "-- ① 跳过全A自采(PULL_FETCH!=1)——用现有主档 --"
  fi
  echo "-- ② 全A多策略选股(策略0/1/2/3/4)+ 对(选出并集∪自选)做新闻/LLM/合议 + M2财报(数值+审计双闸门+LLM文本,仅news_subset) --"
  # --no-fetch:不触发 master_sync 回填/重采,直接用现有主档(近史护栏);财报三步在 run_screen_all 内对 news_subset 自然跑
  "$PY" -m tools.run screenall --no-fetch || echo "!! screenall 失败"
  echo "-- ②.5 前瞻记分卡(picks+预测+情绪 配到期实际收益,幂等滚存;消息面回测长期样本源) --"
  # 持久 --out:每天重跑把"新到期"的前瞻收益补进,累积几周后供 backtest_sentiment / PEAD 复验
  "$PY" -m tools.backtest.forward_scorecard --out "$REPO/data/analysis/backtest/forward_scorecard.csv" || echo "!! 记分卡(不阻断)"
  echo "-- ③ 上传远端(先不带 --force:只补未确认分片,规避 ingest 429 限速) --"
  "$PY" -m tools.sync.upload --date "$D" || echo "!! 上传第一轮"
  sleep 65   # 限速窗口(120/60s);分片>120 时首轮部分 429,等窗口重置补齐
  "$PY" -m tools.sync.upload --date "$D" || echo "!! 上传补齐"
  echo "==================== done $(date) ===================="
} >> "$LOG" 2>&1
