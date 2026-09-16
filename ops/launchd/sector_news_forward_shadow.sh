#!/bin/bash
# 消息驱动板块选股 M3 forward 记分器(供 launchd 调用):工作日盘后 21:15
#   (排在 veto_track 21:00 之后、闭环末端)。对当日信号日 D 读 sector_focus.json 的
#   「消息驱动」块(缺则回退真 catalyst_<D>.json 官方转换)→ 预注册回踩限价入场(D 收盘价、
#   次日 D+1 回踩才成交、不追高开)→ 记 D+1/D+2 绝对收益(未到期留 null)→ 落
#   data/shadow_forward/sector_news/<D>.json;龙头 / 跟涨(联动观察)严格分开、分层。
#   收尾 best-effort --backfill 回填历史 advisory 已到期收益(今日收盘价可得后给昨日信号打分)。
#
# 时点说明:21:15 既保证当日 17:30 sector_daily 已把「消息驱动」块写进 sector_focus(信号可读),
#   又保证当日 EOD 全A K 线已刷新(回填昨日信号的 D+1 收益);不走 LLM、纯本地量价+读文件。
#
# ⚠️ 纪律:纯记录、non-gating、forward-only——**绝不动 live 选股、绝不写 sector_focus.json**;
#   龙头(个股催化)与跟涨(补涨联动观察)分列验;样本 <120 只报 N、不下结论;判据预注册、不对个例调参。
#   agent 只备本脚本 + plist(enabled:false 草案),`launchctl load` 由用户本人(约法)。
#   依据:docs/计划/2026-09-16_选股侧消费板块利好标签_接口设计.md §3/§4。
#
# 依赖顺序:读当日 sector_focus 消息驱动块(17:30 sector_daily 产出)/ catalyst(14:00)+ 全A K线,
#   须排在盘后闭环之后;不走 LLM。
# 卫生:①常驻专用 dailyjob worktree 跑最新 origin/main;②纯本地计算(读块/K线,不触网、不走 LLM);
#   ③单实例锁;④CLI 自带交易日守卫 + 幂等;⑤块与全A K线自动探测主仓 data。
set -uo pipefail

WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "$(date) 警告:git fetch origin 失败,用现有 origin/main" >&2
git -C "$WORKTREE" reset --hard origin/main >/dev/null 2>&1 || echo "$(date) 警告:reset --hard 失败,用 worktree 当前代码" >&2
cd "$WORKTREE"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
LOG="${STOCK_SECTOR_NEWS_FORWARD_LOG:-$HOME/.local/state/stock/sector_news_forward_shadow.log}"
mkdir -p "$(dirname "$LOG")"

LOCK="$HOME/.local/state/stock/sector_news_forward_shadow.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date) 已有实例在跑,跳过" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

{
  echo "==================== $(date) sector_news_forward_shadow ===================="
  # CLI 缺省 --date=今天 / --out-dir=data/shadow_forward/sector_news;交易日守卫 + 幂等 + 收尾自带 backfill。
  "$PY" -m tools.research.sector_news_forward ${STOCK_DATA_ROOT:+--data-root "$STOCK_DATA_ROOT"}
  RC=$?
  echo "-- 退出码 $RC --"
  exit $RC
} >> "$LOG" 2>&1
