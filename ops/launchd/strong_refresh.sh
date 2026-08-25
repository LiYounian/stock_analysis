#!/bin/bash
# 傍晚补跑(供 launchd 调用,工作日 20:00):Tushare 筹码 cyq_perf 傍晚才发布,daily(15:40)跑策略9 时
#   取不到→当天不出。此任务在筹码发布后单独重跑 S05最强选股 + 只补传「最强选股」这一个 view 分片
#   (不重传其它 300+ 分片,零外溢、省流量)。纯数值筹码,无 LLM,故不做 pull_refresh 的 LLM_* 兜底。
# 密钥只放本机受限文件、不进 git:从 $HOME/.config/stock/sync.env 读(chmod 600)。
set -uo pipefail

ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
D="$(date +%Y-%m-%d)"
LOG="${STOCK_STRONG_LOG:-$HOME/.local/state/stock/strong_refresh.log}"
mkdir -p "$(dirname "$LOG")"
# 单实例锁:避免与上一轮重叠
LOCK="$HOME/.local/state/stock/strong_refresh.lock"
if ! mkdir "$LOCK" 2>/dev/null; then echo "$(date) 已有实例在跑,跳过" >> "$LOG"; exit 0; fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) strong_refresh $D ===================="
  # ⓪ ff-only 自更到最新 main(与 pull_refresh 同策略):确保用最新代码(strong 子命令 / --only-view 补传)。
  #    只快进代码,data/analysis 滚存原样不动;仅 HEAD==main 时才动;拿不到就打 WARNING 照跑当前代码。
  _CUR_BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  git -C "$REPO" fetch --quiet origin 2>/dev/null || echo "!! ⓪ git fetch origin 失败"
  if [ "$_CUR_BRANCH" = "main" ]; then
    if git -C "$REPO" merge --ff-only origin/main >/dev/null 2>&1; then
      echo "-- ⓪ 代码同步到 main($(git -C "$REPO" rev-parse --short HEAD)) --"
    else
      echo "!! ⓪ WARNING:ff-only 快进失败——用当前代码($(git -C "$REPO" rev-parse --short HEAD))跑"
    fi
  else
    echo "!! ⓪ WARNING:主仓 HEAD 不在 main(当前=$_CUR_BRANCH)——用当前代码跑"
  fi
  # ① 重跑 S05 最强选股(全A,--no-fetch 读 daily 已落的主档;此时 Tushare 筹码 cyq_perf 已发布)。
  #    未配 TUSHARE_TOKEN / 仍取不到 → 写"需 Tushare"占位 view、不出(不崩)。
  echo "-- ① 重跑 S05 最强选股(全A,--no-fetch) --"
  "$PY" -m tools.run strong --no-fetch || echo "!! strong 失败"
  # ② 只补传「最强选股」单个 view 分片(不重传其它分片)。record 层不动、其它 view 不动,幂等替换该 view。
  echo "-- ② 只补传「最强选股」view 分片 --"
  "$PY" -m tools.sync.upload --date "$D" --only-view 最强选股 || echo "!! 补传失败"
  echo "==================== done $(date) ===================="
} >> "$LOG" 2>&1
