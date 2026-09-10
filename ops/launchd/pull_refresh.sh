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
# 代码源:**不从主仓工作树跑**——主仓常被并发会话卡在旧 commit(WIP 挡住 ff-only 自更),
#   从主仓跑会漏当天新合并的字段/节点。改为像 autopush 一样,从一个常驻 detached worktree
#   跑最新 origin/main:每轮 fetch + reset --hard origin/main,全程不碰主仓 HEAD/工作树。
set -uo pipefail

ENV_FILE="${STOCK_SYNC_ENV:-$HOME/.config/stock/sync.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

# LLM_* 兜底:launchd 读不到 ~/.zshrc,而 LLM_* 常是别名(间接引用),让用户登录 shell 解析整条链喂进来。
if [ -z "${LLM_API_KEY:-}" ]; then
  _LOGIN_SHELL="${STOCK_LOGIN_SHELL:-/bin/zsh}"
  eval "$("$_LOGIN_SHELL" -ic 'printf "export LLM_BASE_URL=%q\nexport LLM_API_KEY=%q\nexport LLM_MODEL=%q\n" "${LLM_BASE_URL:-}" "${LLM_API_KEY:-}" "${LLM_MODEL:-}"' 2>/dev/null || true)"
fi
export LLM_BASE_URL="${LLM_BASE_URL:-}" LLM_API_KEY="${LLM_API_KEY:-}" LLM_MODEL="${LLM_MODEL:-}"

# —— 专用 worktree 卫生:强制常驻 worktree 更到最新 origin/main 再跑(照 autopush.sh 选项A)——
# data/raw|master|backtest_local|intraday 在该 worktree 内是指向主仓的 symlink(共享大缓存/盘中状态),
# data/analysis 由 worktree 自己滚存(部署时已 seed 历史日期目录,记分卡多周样本不断)。
# 可用 STOCK_DAILYJOB_WORKTREE 覆盖路径。fetch 走该 worktree 的 git(与主仓共享对象库,不动主仓)。
WORKTREE="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
if [ ! -e "$WORKTREE/.git" ]; then
  echo "$(date) 致命:专用 worktree 不存在:$WORKTREE(请先 git worktree add --detach \"$WORKTREE\" origin/main)" >&2
  exit 3
fi
git -C "$WORKTREE" fetch --quiet origin || echo "!! ⓪ git fetch origin 失败,用该 worktree 现有 origin/main" >&2
_OLD_HEAD="$(git -C "$WORKTREE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
git -C "$WORKTREE" reset --hard origin/main
_NEW_HEAD="$(git -C "$WORKTREE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
REPO="$WORKTREE"
cd "$REPO"
PY="${STOCK_PYTHON:-$HOME/.conda/envs/stock_analysis/bin/python}"
D="$(date +%Y-%m-%d)"
LOG="${STOCK_PULL_LOG:-$HOME/.local/state/stock/pull_refresh.log}"
mkdir -p "$(dirname "$LOG")"
# 单实例锁:避免与上一轮重叠。A3 陈旧锁抢占(诊断 §6.2):旧 `mkdir+trap EXIT rmdir` 在
#   SIGKILL/断电下 trap 不执行→锁残留→之后每个交易日永久跳过。改用公共库:锁内写 PID+启动时间,
#   死持有者/超龄(>STOCK_LOCK_STALE_SEC 默认6h)自动清理抢占,活实例不误抢。lock.sh 随 $REPO
#   (已 reset 到 origin/main)自动流转;lib 缺失(半部署态)退回旧 mkdir 锁不硬失败。
LOCK="$HOME/.local/state/stock/pull_refresh.lock"
if [ -f "$REPO/ops/launchd/lib/lock.sh" ]; then
  . "$REPO/ops/launchd/lib/lock.sh"
  acquire_lock_or_exit "$LOCK" "$LOG"
else
  if ! mkdir "$LOCK" 2>/dev/null; then echo "$(date) 已有实例在跑,跳过" >> "$LOG"; exit 0; fi
  trap 'rmdir "$LOCK" 2>/dev/null' EXIT
fi

# A2 电源护栏(诊断 §6.1 RC-3.a):整轮进程级防空闲/系统睡眠,盘后长跑合盖/断电不被挂起强杀。
#   caffeinate -w 盯本进程 PID、随本进程退出自动结束;**仅进程级,不改系统电源设置、不碰 launchd**。
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -i -m -s -w "$$" &
fi

{
  echo "==================== $(date) pull_refresh $D ===================="
  # ⓪ 代码已在脚本头部由专用 worktree 卫生更到最新 origin/main(fetch + reset --hard),这里只记账:
  #    forward_scorecard 的多周滚存样本靠 data/analysis/<日期>/ 逐日累积——本 worktree 常驻不删,
  #    reset --hard 只重置 tracked 文件、不动未跟踪日期目录,故滚存在本 worktree 内照常累积(部署已 seed 历史)。
  if [ "$_OLD_HEAD" = "$_NEW_HEAD" ]; then
    echo "-- ⓪ 专用 worktree 已是最新 origin/main($_NEW_HEAD),无需更新 --"
  else
    echo "-- ⓪ 专用 worktree 已更到最新 origin/main:$_OLD_HEAD -> $_NEW_HEAD --"
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
  echo "-- ①.7 turnover 每日兜底(volume 自证回填 fallback_advance 落下的 NaN,~45s 纯本地读盘) --"
  # 必须排在①主档推进之后、②screenall之前:①的回退补齐网(baostock)best-effort,失败时当日
  #   turnover 静默落 NaN → S04 单日放量哑火 / 筹码集中度·成本降级。这里用本票自身近端正常行的
  #   ratio=turnover%/volume 中位数 × 当日 volume 还原 turnover(恒等式、无外部源、无未来函数:
  #   当日行是主档最新行,参考全取历史行;参考不足/流通股阶跃/volume 缺一律 refuse 留 NaN)。
  # 幂等:只填 turnover 为 NaN 的行,填过即非 NaN、下轮跳过;refused 行下轮再试仍 refuse(无副作用)。
  #   单实例由 wrapper 顶部 LOCK 目录保证,不会与上一轮重叠。best-effort:失败不阻断②选股。
  # --alert-marker:回填量冲高=当日补齐网大面积失效被 volume 救回 → 落主动告警(非埋进大日志)。
  "$PY" -m ops.backfill_turnover --apply \
      --alert-marker "$REPO/data/analysis/$D/_TURNOVER_BACKFILL_ALARM.json" \
      || echo "!! turnover 每日兜底失败(不阻断,下游现算兜底+下轮重试)"
  echo "-- ② 全A多策略选股(策略0/1/2/3/4)+ 对(选出并集∪自选)做新闻/LLM/合议 + M2财报(数值+审计双闸门+LLM文本,仅news_subset) --"
  # --no-fetch:不触发 master_sync 回填/重采,直接用现有主档(近史护栏);财报三步在 run_screen_all 内对 news_subset 自然跑
  "$PY" -m tools.run screenall --no-fetch || echo "!! screenall 失败"
  # -- ②.6 SEPA+VCP 监控 —— 已停用(2026-09-04) --
  #   停用理由:回测(replay_sepa,250日/748票)显示 SEPA 合格池**无 alpha**——胜率 44-46%、5日超额
  #   -0.21pp、完全不优于随机;且它**不喂生产选股并集**(独立监控视图,picks 不进 union→深采)。
  #   保留代码(screen_sepa_vcp / analysis.sepa_vcp / backtest 脚本),仅摘掉日常调度。将来若换 regime
  #   重启趋势口径,取消下面注释即可(注意 --date "$D" 防日期漂移,勿删该经验)。见开发日志 2026-09-04。
  # "$PY" -m tools.run sepa --no-fetch --date "$D" || echo "!! sepa 失败(不阻断)"
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
  # -- ②.8 产出缺口护栏 —— 随 SEPA 停用一并关闭(2026-09-04) --
  #   SEPA 停用后"无 SEPA 视图"是**预期状态、非缺口**,再跑护栏会天天误告警。output_guard 代码保留,
  #   与 ②.6 一同恢复即可。
  # echo "-- ②.8 产出缺口护栏(SEPA 三视图当日必达,否则告警) --"
  # "$PY" -m tools.ops.output_guard --date "$D" --marker \
  #   || echo "!!! 护栏告警:当日 $D 趋势跟随口径(SEPA)视图缺失,见上方明细;marker=data/analysis/$D/_GAP_ALARM.json"
  echo "-- ③ 上传远端(先不带 --force:只补未确认分片,规避 ingest 429 限速) --"
  "$PY" -m tools.sync.upload --date "$D" || echo "!! 上传第一轮"
  sleep 65   # 限速窗口(120/60s);分片>120 时首轮部分 429,等窗口重置补齐
  "$PY" -m tools.sync.upload --date "$D" --pool-ack || echo "!! 上传补齐"
  echo "==================== done $(date) ===================="
} >> "$LOG" 2>&1
