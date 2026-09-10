# 诊断:market_forecast(大盘预测)"停更" — 2026-09-10

> 结论先行:**大盘预测没有真正崩,一直在产**;09-08 起产物被**写进部署 worktree 的
> `data/analysis/`,不再落主仓**,而复盘核查看的是主仓 `data/analysis/`,故"看起来停更"。
> 叠加一层次要问题:某些天(09-04、09-08)pull_refresh 整轮在触及预测步之前就异常终止,
> 那几天连 worktree 里也没产出。**纯诊断,未改任何生产代码/调度。**

## 一、market_forecast 产出链路

- **Writer**:`tools/analysis/market_forecast/forecast.py`(`build_forecast` → `--write-analysis`
  落 `data/analysis/<T>/market_forecast.json`,schema `market_forecast/v1`)。
- **触发**:launchd `com.stock.pullrefresh`(工作日 15:40)→ `ops/launchd/pull_refresh.sh`
  **步骤 ②.7**:`$PY -m tools.analysis.market_forecast.forecast --as-of "$D" --write-analysis`。
  该步**非阻断**(`|| echo "!! 大盘预测(不阻断)"`),排在整条盘后闭环较后位置(选股/记分卡之后)。
- **数据根解析**:`tools/analysis/market_forecast/dataroot.py`,产出目录 = `<data_root>/analysis/<T>/`。
- **消费方**(都从 worktree 跑,见下):选股 β 基准 / 复盘 `intraday_review` / 远端 `sync.upload`
  上传 `__view__:market_forecast` 供 web 展示。**注:`data/analysis/*.json` 不入 git**
  (`git ls-files` 为空、也未 ignore),是**本地产物**,靠人/脚本直接看目录。

## 二、根因(主因):部署 worktree 改造把 analysis 产出路径挪走了

**引入改动**:`c96b10e`(2026-09-08 11:38)"fix(日更job): 三个 launchd wrapper 改从专用
worktree 跑最新 origin/main"。此后 `pull_refresh.sh` 强制 `cd` 到部署 worktree
`~/Documents/projects/worktrees/stock_analysis/dailyjob` 跑,`REPO=$WORKTREE`。

该 worktree 的 `data/` 布局(实测):

| 子目录 | 形态 | 指向 |
|---|---|---|
| `raw` / `master` / `intraday` / `backtest_local` | **symlink** | → 主仓 `data/...`(共享大缓存/盘中态) |
| `analysis` | **真实目录(非 symlink)** | worktree 自己滚存 |

`analysis` 被**故意**做成 worktree-local(脚本注释原意:让 forward_scorecard 多周滚存样本
在 worktree 内累积、不被 `reset --hard` 影响)。**副作用**:`forecast.py` 相对 `data/analysis/`
写入,落到 **worktree** 而非主仓。

**证据(pull_refresh.log 里 `[saved]` 路径,时间线完全对齐)**:
- `2026-09-03` → `/Users/.../stock_analysis/data/analysis/...`(**主仓**,改造前)
- `2026-09-07` → `/Users/.../stock_analysis/data/analysis/...`(**主仓**,改造前,09-07 15:40 早于 c96b10e)
- `2026-09-09` → `/Users/.../worktrees/stock_analysis/dailyjob/data/analysis/...`(**worktree**,改造后)

主仓 `data/analysis/` 最后一天有 `market_forecast.json` = **2026-09-07**;之后主仓再没进新的。
worktree `data/analysis/` 则有 09-09 的产出。→ **复盘核查看主仓 → 判定"09-08 起停更"。**

> 关键结论:**改造后连成功产出的 09-09 也不在主仓**。所以"停更"本质是
> **生产/消费路径错位**,不是预测器本身故障。选股/复盘 job 自身也从同一 worktree 跑
> (`intraday_review.sh` 同样 `cd $WORKTREE`),它们读的是 worktree analysis,**并未降级**;
> 受影响的是"读主仓目录"的那一侧(人工复盘核查、以及任何以主仓为根的工具/校对)。
> 另:`commit_analysis_docs.sh` 只把 **`*.md`** 从 worktree 回拷主仓,**不含 `market_forecast.json`
> 等 per-day analysis JSON**,故主仓永远等不到这份产物回流。

## 三、次因(次要):某些天整轮在预测步之前异常终止

把 `pull_refresh.log` 按运行头切分,`②.7` 只在 **09-03 / 09-07 / 09-09** 三轮出现;
**09-04、09-08 两轮根本没跑到 ②.7**(既无 `②.7` 行也无 `!! 大盘预测` 兜底行),说明整轮
在触及预测步之前就结束了 → 那两天**主仓和 worktree 都没有** market_forecast:

- **09-04 run**:跑到"全A多策略选股完成 → …/2026-09-04/"后,进程以
  `resource_tracker: leaked semaphore … at shutdown` 收尾即终止,**未到 ②.7**。
- **09-08 run(15:40)**:死在 ①自采 K 线 `[1198/5558]`(极早),同样 semaphore 收尾。
  当天 18:25 有一次**人工重跑**,又死在基本面采集途中,仍未到 ②.7。
- 09-04 早于部署改造,属**独立的、更早就存在的整轮可靠性问题**(与路径无关)。

共同特征:进程被**提前终止**(非正常 shutdown 的 semaphore 泄漏告警),而非预测代码报错。
可能诱因(需进一步确认,未下定论):机器休眠/被杀、采集阶段崩溃、单轮耗时过长。
**旁证**:09-09 那轮 15:40 启动、次日 07:07 才 upload(≈15h),若单轮常态 12–15h,会逼近
次日 15:40 的单实例锁("已有实例在跑,跳过")→ 可能整轮被跳过。这条是可靠性隐患,建议后续单查。

### 次因旁证补充(2026-09-10 晚,统筹确认主因后追加;纯读日志)

- **"锁饿死"目前尚未真实发生(修正上文措辞)**:`grep "已有实例在跑,跳过" pull_refresh.log`
  = **0 次**。即虽然 09-09 轮耗时长,但每轮都在次日 15:40 前完成、下一轮正常拿到锁。
  单实例锁饿死是**潜在风险、尚未触发**,不是这几天早死的原因。上文"可能整轮被跳过"应据此收敛。
- **09-08 轮1 是"启动即猝死",非"长轮拖死"**:15:40:07 起、**15:56:56** 就死在 ①自采 K线
  `[1198/5560]`(仅 ~16min)。属**采集阶段崩溃/被外部杀**,与耗时无关。18:25 人工重跑亦在
  采集途中再死。→ 次因的两天(09-04 死于选股后 shutdown、09-08 死于采集早段)**死法不同**,
  共性只是"非正常 shutdown 的 semaphore 泄漏 + 未及 ②.7",不是单一诱因。
- **15h 长轮里 forecast 不是瓶颈**:09-09 轮 ②.7 在轮内较早段即完成落盘,之后才进 ③上传;
  ③上传首条时间戳 = **09-10 07:05**。长尾集中在 ② 选股(LLM/合议,那些 `run INFO` 行无时间戳,
  未逐步计时)及 forecast 之后到上传之间的环节,**与预测步无关**。→ 若做次因治理,重点在
  采集稳健性 + ②/③长尾,而非 forecast 本身。

## 四、09-10 现状(今天)

pull_refresh(PID 10853,15:40 启动)**仍在运行**(18:38 仍在 collectors 阶段,尚未到 ②.7)。
故 09-10 目前两处都没有 market_forecast 属**正常(还没跑到)**,不是故障;若本轮不中途夭折,
应会产出到 **worktree** analysis(仍不会进主仓——即主因问题依旧)。

## 五、修复方案(建议,待统筹拍板;涉及生产调度/数据布局,未擅动)

### 主因(路径错位)—— 三个候选,推荐 A

- **A(推荐,最小侵入、语义正确)**:在 `pull_refresh.sh` ②.7 之后加一个**回流步**,把当日
  `worktree/data/analysis/<D>/market_forecast.json`(可含其它需在主仓可见的 per-day 产物)
  **copy 回主仓 `data/analysis/<D>/`**;或直接扩 `commit_analysis_docs.sh` 白名单纳入该 JSON。
  - 预期效果:复盘核查看主仓即见当日 market_forecast;worktree 仍保留滚存,互不影响。
- **B**:把 worktree 的 `data/analysis` 也做成 **symlink → 主仓**(与 raw/master 一致)。
  - 风险:与"analysis 要 worktree-local 供 scorecard 滚存"的原设计冲突;需先确认 scorecard
    CSV(`data/analysis/backtest/…`)在 symlink 下是否仍按预期累积、`reset --hard` 是否触及。
    改动面比 A 大,不建议先动。
- **C**:改**消费/核查侧读取根**到 worktree(治标)。不推荐——主仓被多处默认当"权威副本",
  改读点发散、易漏。

### 次因(整轮提前终止)—— 建议先只诊断/加护栏,不动调度

- 复现并定位 semaphore 泄漏/提前终止的诱因(采集阶段崩溃 or 外部 kill);
- 给 ②.7 加"若当天无产出则重试一次"或把预测步前移/独立成小 job(不依赖整条重链跑通);
- 排查单轮 12–15h 耗时是否会与单实例锁互相饿死(09-09 次日才完成的现象)。
- 以上均涉及生产调度,**按守则先出方案、统筹审过再动;改 launchd 前先查 job 是否在跑**。

## 六、次因收敛:根因分级 + 可排期方案(2026-09-10 晚,统筹指令)

> 本节用两条并行深挖(耗时爬日志 + 采集/锁代码审查)的量化证据,把"次因"从旁证收敛为
> 可排期方案。**仍纯诊断,未改任何代码/调度。** 证据来源见每条括注。

### 6.1 根因分级(带证据)

**RC-1(主导·"盘后选股迟/15h长轮"的真因)——② 阶段串行 LLM 调用线性放大**
- ② 阶段(全A多策略选股 + LLM 合议 + M2 财报 LLM 文本 + 资金流采集)占整轮 **~90%+ 时长**:
  09-09 轮 ② ≈ **13h59min**(总轮 15h27min)。证据:该区间统计到 **~11,173 次** LLM chat
  completion 请求、打向**内部 LLM 网关(qwen 模型)**,**0 条重试告警**(全部一次成功),
  50,362s ÷ 11,173 ≈ **4.5s/次**,足以完全解释时长——是"调用量大 × 单线程串行",不是卡死。
- 代码根因(已核实):`tools/pipeline/screen_council.py` 主循环单层 `for code in codes:`、
  `tools/llm/client.py` 同步 `openai.OpenAI(...).chat.completions.create(...)` 一次一个,
  **全链路零并发**。
- **今天(09-10)同一现象**:① 批量 spot 成功、68s 跑完,②于 15:41 起,到 19:22 才 ~35%
  (3918/11173 次调用),按 ~3.4s/次外推预计 **09-11 凌晨才结束②**。→ 今晚选股迟 = RC-1,非锁、非崩溃。

**RC-2(某些天"整轮采集早崩→当天无任何产出")——① 单票多源采集无可靠超时上界**
- ① 逐只兜底路径(`tools/collectors/master_sync.py:440-453` → `tools/collectors/market.py`)
  多源 fallback `tencent→sina→eastmoney`(`market.py:34`)超时防护**严重不对称**:
  tencent 显式 15s 硬超时;**sina(akshare 内部 `requests.get` 未传 timeout)、eastmoney
  (`timeout=None` 未传)均无上界**;`socket.setdefaulttimeout(10)` 对"显式 timeout=None"
  不生效(requests 已知坑)。
- 纯串行、**无单票看门狗/无熔断** → 只要一只票在 sina/eastmoney 的 read 阶段挂起,**整个 ①
  步可无限期挂起**,直到被外部强杀。与 **09-08 run1"仅 16min、死在 K线 1198/5558"** 吻合。

**RC-3(semaphore 泄漏 = 外部强杀的次生症状,非代码 bug)**
- 全仓唯一 `multiprocessing.Pool` 在独立回测 CLI `tools/backtest/validate_adaptive_rr.py:110`,
  **不在日更调用链**;链路内并发全是 `ThreadPoolExecutor`(不经 resource_tracker);
  `py_mini_racer` 同进程 ctypes、不 fork。→ `leaked semaphore at shutdown` 是**某 python 子进程
  被 SIGKILL、未走正常 shutdown** 的伴生告警。脚本 `set -uo pipefail`(无 `-e`)、每步都有
  `|| echo` 兜底、plist 无 ExitTimeOut/资源限额 → **脚本自身不会因单步失败提前退出**,唯一能解释
  "死在①/死在②后"的路径是 **bash+python 进程树被外部不可拦截信号(SIGKILL)整体杀掉**。

**外部强杀来源(未完全确证,需 host 侧确认)**:
- (a) **电源/睡眠**(latent,部分命中):`pmset` 设 `sleep 1`(1min 空闲即睡)+ 历史多次
  clamshell-on-battery 睡眠。09-08 run2(18:25 起、死在 ② 基本面 ~603xxx)时段命中 **19:49
  Clamshell Sleep(Using Batt)**,疑似被它杀;09-04 亦有 17:25 clamshell sleep 候选。
  **但** 09-08 run1(15:56,无 sleep)与 09-09 的 15h(傍晚到次晨**无** sleep 事件)**不是睡眠**。
- (b) **①单票无限挂起被外部(人工/监控/OS)强杀**(见 RC-2),对应 09-08 run1。
- (c) 09-08 run1 死后 **18:25 有人工重跑**(worktree HEAD 从 c70d2b3→8b16490,有新 commit)。

### 6.2 明确排除(统筹要求)

- ❌ **锁饿死**:`grep "已有实例在跑,跳过"` = **0 次**,从未发生。**但**锁用 `mkdir`+`trap EXIT
  rmdir`,SIGKILL 下 trap 不执行 → **锁目录残留会让后续每个交易日永久"跳过"直到人工 rmdir**。
  这是**尚未触发的严重 latent 隐患**(一旦 RC-2/RC-3 的强杀发生且锁残留,日更整体停摆)。
- ❌ **forecast 步(②.7)**:纯本地、`<1–2min`、0 次网络调用,**不是瓶颈**;它"停更"是被前面
  RC-1 长尾/RC-2 早崩**带累跳过**(非阻断且排在链路末段),不是自身问题。
- ❌ **mini_racer/V8 多进程崩溃**:历史根因,已由 `FETCH_WORKERS=1` 消除,与本周失败无关。

### 6.3 分级方案 + 预期效果 + 工作量(agent 视角)

> 工作量按"预估 token(input+output 量级)/ agent 执行工时(含工具往返+审阅)"给。

**A 级——低风险护栏(不改调度拓扑;改代码/wrapper 参数;仍需统筹审后由实现窗做)**

| 项 | 做什么 | 预期效果 | 工作量 |
|---|---|---|---|
| **A1 单票硬超时看门狗** | 给 `_fetch_sina`/`_fetch_eastmoney` 显式传 `timeout=`,或在 `_fetch_one_with_source` 外层套 per-source `ThreadPoolExecutor(1).submit().result(timeout=X)` | 根治 RC-2:单票最多卡 X 秒即降级换源,① 不再被一只票拖到无限挂起→被杀 | ~150–250K token / ~1–2h(改 market.py + 测试) |
| **A2 caffeinate 包整轮** | wrapper 用 `caffeinate -imsu` 起(或内部 `caffeinate -w $$`),仅进程级防睡、不改系统电源设置 | 堵电源/睡眠强杀(RC-3.a):盘后合盖/电池也不挂起不被杀 | ~30K token / ~0.5h(改 1 行 + 验证,**不碰 launchd**) |
| **A3 锁陈旧检测** | 锁目录写 PID+启动时间;拿不到锁时检查 PID 存活/超龄(如 >6h)则判陈旧锁、自动清理抢占 | 根治 latent 锁残留:即使某轮被 SIGKILL 残留锁,次日自动清不空转 | ~100K token / ~1h(改锁段 + 测试) |
| A4 semaphore 泄漏 | 无需单独治——是 RC-3 强杀的症状,A1/A2 消除强杀后自然消失 | — | 0 |

**B 级——需审的调度/架构改造(面较大,先审再动;改 launchd 前查 job 在不在跑)**

| 项 | 做什么 | 预期效果 | 工作量 |
|---|---|---|---|
| **B2(推荐优先)forecast 解耦成独立小 job** | 把 ②.7 大盘预测拆出巨型链路,盘后单独定时跑(只依赖本地 master+analysis,<2min);产出位置并进「部署卫生①a」一并解 | 直接治"market_forecast 停更"结构病根:即便整轮采集崩/长尾拖到次日,forecast 当天照常出 | ~200–350K token / ~2–3h(拆脚本+新 plist,需装载 runbook + 统筹审) |
| **B1 ② 的 LLM 调用并发化** | `screen_council` 合议/财报/情绪 LLM 调用改线程池或异步批量(需处理网关 429 限速、结果一致性、成本) | 唯一能**数量级**缩短 15h→(并发 N≈8 时约 2h 量级)的方向,根治 RC-1/选股迟 | ~400–800K token / ~4–8h + 回测,**需专窗先出设计文档** |
| B3 ③上传/② 独立化 | 次要,同 B2 思路解耦上传 | 长尾互不阻断 | 视 B1/B2 一并评估 |

### 6.4 推荐落地顺序

1. **立即(A 级,低风险高收益)**:A2 caffeinate（最省、堵电源杀）+ A1 单票超时看门狗（堵 RC-2 挂死）+ A3 锁陈旧检测（堵 latent 停摆）。三项都不改调度拓扑。
2. **优先排期(B2)**:forecast 解耦独立 job,**并进「部署卫生①a」**(它同时解主因的产出可见性),
   一次性把"每天必出 market_forecast + 落到主仓可见"都收掉。
3. **专窗排期(B1)**:② LLM 并发化,根治 15h/选股迟——改动最大、需设计+回测,单开窗。

## 七、本次动作

- **纯诊断**,未改任何生产代码、未碰 launchd、未碰 `data/`。两条深挖为并行只读 subagent
  (耗时爬日志 + 采集/锁代码审查),结论已交叉核验并入第七节。
- 诊断文档即本文件。分支 `claude/bold-bhabha-080f7e`(诊断 worktree),不合 main。
