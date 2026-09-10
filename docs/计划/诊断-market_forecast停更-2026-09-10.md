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

## 六、本次动作

- **纯诊断**,未改任何生产代码、未碰 launchd、未碰 `data/`。
- 诊断文档即本文件。分支 `claude/bold-bhabha-080f7e`(诊断 worktree),不合 main。
