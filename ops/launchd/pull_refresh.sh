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
  # ⓪ 跑最新已合并 main 代码(选项B:主仓内 ff-only 自更,不破坏数据流)——
  #    背景:本项目多窗口并发,主仓可能落后 origin/main(别的 worktree 合并并 push 后,主仓没同步)。
  #    若不自更,当天会用旧代码产数据、新字段漏。这里在主仓内直接 fetch + ff-only 快进到 origin/main。
  #    为什么不切到专用 worktree(选项A):本任务的数据流强依赖"在同一仓库里滚存"——
  #      · forward_scorecard 每次 build_scorecard() 从 store.list_dates() 全量重扫 data/analysis/<日期>/ 重建 CSV;
  #      · 而这些每日日期目录是**未跟踪产物**,靠常年跑在同一主仓才逐日累积(git status 里一片 ?? data/analysis/2026-08-*)。
  #    worktree 每次 reset --hard 只会保留 origin/main 已提交的日期目录 + 当天新产,历史未跟踪日期目录不累积,
  #    记分卡的多周滚存样本会被打断。故选 ff-only 自更:只快进代码,data/analysis 滚存原样不动。
  #    安全性:fetch 只碰共享对象库;merge --ff-only 绝不产生合并提交/改写历史,冲突即中止;仅当 HEAD==main 时才动,
  #    不触碰其它 worktree/feature 分支。拿不到最新时**打 WARNING 照跑当前代码**(不静默,便于事后定位漏字段)。
  _CUR_BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  git -C "$REPO" fetch --quiet origin 2>/dev/null || echo "!! ⓪ git fetch origin 失败,用主仓现有 origin/main 尝试快进"
  if [ "$_CUR_BRANCH" = "main" ]; then
    _OLD_HEAD="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    if git -C "$REPO" merge --ff-only origin/main >/dev/null 2>&1; then
      _NEW_HEAD="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
      if [ "$_OLD_HEAD" = "$_NEW_HEAD" ]; then
        echo "-- ⓪ 代码已是最新 main($_NEW_HEAD),无需更新 --"
      else
        echo "-- ⓪ 已快进到最新 main:$_OLD_HEAD -> $_NEW_HEAD --"
      fi
    else
      echo "!! ⓪ WARNING:ff-only 快进失败(主仓可能领先/有对同一跟踪文件的本地改动/与 origin/main 分叉)——用当前代码($_OLD_HEAD)跑,可能漏新字段"
    fi
  else
    echo "!! ⓪ WARNING:主仓 HEAD 不在 main 分支(当前=$_CUR_BRANCH)——不自更,用当前代码跑,可能漏新字段。请把主仓切回 main"
  fi
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
  echo "-- ①.4 消化远端自选提案(方案2:pull→裁决→采集/清理) --"
  # 远端网页(POOL_WRITE_MODE=enqueue)把加/删写 pool_pending 表;这里拉下来裁决→add_and_collect/
  # remove_and_cleanup(本地有 raw,采集+重建 panel 不塌),产出 consumed_ids→data/sync_receipts/pool_ack.json,
  # 随③末轮 upload --pool-ack 回推。排在①主档推进后、①.5建池前:新票先进主档→当天 state_pool/screenall 即纳入。
  # 远端不可达/无提案时优雅跳过(consume 内部 ok:False),不阻断闭环。
  "$PY" -m ops.consume_pool_pending || echo "!! 消化远端提案失败(不阻断,下轮重试)"
  echo "-- ①.5 建状态池(策略11 指标条件化状态排序依赖;全A主档,实测~68s) --"
  # 必须排在①主档推进之后、②screenall之前:screenall 里的策略11 screener 只读 state_pool 索引、不建池;
  # 缺池则策略11 优雅出空(view 带 note、不崩)。落 data/backtest_local/state_pool.parquet(gitignore)。
  "$PY" -c "from tools.analysis import conditional_predict as cp; from tools.store import repo as s; cp.build_state_pool(sorted(s.list_master_codes()), save=True)" || echo "!! 建状态池失败(策略11 将出空,不阻断)"
  echo "-- ①.6 个股两融采集(沪深北融资买入额/融资余额;供 expert_资金流 做'主力净流入vs融资盘'背离甄别) --"
  # 必须排在②screenall之前:合议 expert_资金流 命中背离→看多降级,需当日两融在 store。akshare 单日增量,快;失败不阻断(甄别缺数据→保守no-op)。
  "$PY" -c "from tools.collectors import margin; margin.fetch_margin(start='$D', end='$D')" || echo "!! 两融采集失败(背离甄别当日 no-op,不阻断)"
  echo "-- ② 全A多策略选股(策略0/1/2/3/4)+ 对(选出并集∪自选)做新闻/LLM/合议 + M2财报(数值+审计双闸门+LLM文本,仅news_subset) --"
  # --no-fetch:不触发 master_sync 回填/重采,直接用现有主档(近史护栏);财报三步在 run_screen_all 内对 news_subset 自然跑
  "$PY" -m tools.run screenall --no-fetch || echo "!! screenall 失败"
  echo "-- ②.6 SEPA+VCP 监控(全A,--no-fetch 读①已推进的主档;纯日K判定,无傍晚发布依赖,15:40就绪) --"
  # SEPA 三均线合格池落 view(全量),展示层取趋势最强前10;view 随③ upload 自动上传(枚举当天目录)
  "$PY" -m tools.run sepa --no-fetch || echo "!! sepa 失败(不阻断)"
  echo "-- ②.5 前瞻记分卡(picks+预测+情绪 配到期实际收益,幂等滚存;消息面回测长期样本源) --"
  # 持久 --out:每天重跑把"新到期"的前瞻收益补进,累积几周后供 backtest_sentiment / PEAD 复验
  "$PY" -m tools.backtest.forward_scorecard --out "$REPO/data/analysis/backtest/forward_scorecard.csv" || echo "!! 记分卡(不阻断)"
  echo "-- ②.7 大盘预测v1(技术+广度+消息面[+资金流维暂关攒数据]→沪深300涨跌;产出 market_forecast.json 供选股定β背景) --"
  # 排在②screenall(sentiment_policy已写)+①主档之后、③上传之前:让预测吃全新数据、且随当日分片上传远端。非投资建议,β环境信号非交易门控。
  # 先把指数更到当日(否则预测的技术维滞后);baostock/新浪源,失败不阻断(预测回退全A代理)。
  "$PY" -c "from tools.collectors import index; index.fetch_index(['000300'], end='$D')" || echo "!! 指数更新(不阻断,预测用代理)"
  # 市场级两融采集(SSE,akshare 一次拉2022→今、增量幂等):供大盘预测资金流维**攒数据+快照**。
  # ⚠️ 资金流维当前权重=0(暂关,不参与预测判别),但仍每日采集、forecast 仍写 fundflow_snapshot,
  #   让真资金流历史随时间累积;待 hs300 样本够、判别力显著再把权重翻 0.3 激活(见 config['大盘预测']['因子权重'])。
  "$PY" -c "from tools.collectors import market_fundflow as m; m.collect()" || echo "!! 市场级两融采集(资金流维当日缺,不阻断)"
  "$PY" -m tools.analysis.market_forecast.forecast --as-of "$D" --write-analysis || echo "!! 大盘预测(不阻断)"
  echo "-- ③ 上传远端(先不带 --force:只补未确认分片,规避 ingest 429 限速) --"
  "$PY" -m tools.sync.upload --date "$D" || echo "!! 上传第一轮"
  sleep 65   # 限速窗口(120/60s);分片>120 时首轮部分 429,等窗口重置补齐
  "$PY" -m tools.sync.upload --date "$D" --pool-ack || echo "!! 上传补齐"
  echo "==================== done $(date) ===================="
} >> "$LOG" 2>&1
