# 统一定时任务框架 · 使用与 launchd→APScheduler 切换 runbook

> 架构⑥。框架代码见 `tools/scheduling/`,设计见
> `docs/计划/2026-09-13_选股系统架构设计_程序化与模型迁移.md` §6。
> **本文写给用户本人**:P1-P2 只建框架 + 影子验证,已随 git 合入;
> **真正切换 launchd → APScheduler 是 P3 纯人工 gated 步骤**,agent 不代为执行任何
> launchctl / 装载 / 卸载 / 改生产调度动作——下面每条 launchctl 命令都由你本人在终端跑。

---

## 0. 现在能安全做什么(零生产副作用)

以下命令**只读/影子**,不碰 live launchd、不起真子进程,随便跑(项目 conda 环境):

```bash
cd ~/Documents/projects/stock_analysis
PY=~/.conda/envs/stock_analysis/bin/python

$PY -m tools.scheduling validate           # 校验注册表(坏配置即报错)
$PY -m tools.scheduling list               # 列出全部任务
$PY -m tools.scheduling next --count 3     # 各任务未来 3 次触发时刻
$PY -m tools.scheduling run                # 启动常驻调度(默认 dry-run 影子:到点只打印"本该跑什么")
```

> `run` **不带 `--live` 就是影子模式**——到点只 log `[dry-run] 到点会执行 ...`,
> 绝不真跑命令、绝不动 launchd。可与现有 9 个 launchd 任务并行开着观察一致性。
> Ctrl-C 退出。要带告警:`STOCK_ALERT_CHANNEL=log $PY -m tools.scheduling run --heartbeat-min 30`。

---

## 1. 概念:注册表 = 唯一事实源(SSOT)

- 所有定时任务声明在 `tools/scheduling/tasks.yaml`,每条:
  `{id, cmd, cron, timeout_sec, retries, notify_on, enabled, depends_on, tags}`。
- **加任务** = 加一条 → 注册即被调度。**改时间** = 改 cron → 热生效(常驻进程开了
  `--reload-min` 轮询即自动重载,无需 launchctl、无需重启)。
- cron 周字段务必用 `mon-fri`(不是数字 `1-5`:APScheduler 数字周 0=周一,`1-5` 会错成周二~周六)。
- SEPA 旧在 launchd(停用)/systemd/进程内 APScheduler **三处**,已在注册表收敛为**单条**
  `id: sepa`(默认 `enabled: false`);盘后全量二处亦以 `pullrefresh` 单条为准。

## 2. 告警(填补全仓零告警的最大空白)

- 渠道可插拔:`LogNotifier`(默认,始终在)+ `WebhookNotifier`(飞书/钉钉/通用)。
- **凭证走 env 变量名,值绝不入库**:
  - `STOCK_ALERT_CHANNEL` = `log` | `feishu` | `dingtalk` | `generic`(可逗号并联,如 `log,feishu`)
  - `STOCK_ALERT_WEBHOOK_ENV` = 存放真正 webhook url 的**变量名**(缺省 `STOCK_ALERT_WEBHOOK`)
  - 真正的 url 放本机受限文件(如 `~/.config/stock/sync.env`,chmod 600),不进 git。
- env 缺失 → fail-soft:只记日志提示去哪配、不外发、不崩,也不会打印 url 明文。
- 触发时机由每任务 `notify_on` 声明:`failure`(失败/超时)、`misfire`(漏跑)、`success`。
- 心跳:`run --heartbeat-min N` 定期发"存活",守护"常驻进程崩了没人知道"。

## 3. 稳健性

- misfire 补偿:`misfire_grace_time`(缺省 3600s)+ coalesce(睡眠/卡顿唤醒后补跑一次)。
- job 级重试:`retries` 次指数退避(把采集层 `_retry` 思想上提到调度层)。
- 单实例锁:mkdir 锁在 `~/.local/state/stock/scheduling_locks/<id>.lock`,防重叠(与 launchd 锁分目录、互不冲突)。

---

## 4. P3 切换 runbook(★ 人工 gated,现在**不要**做;条件成熟后照此逐步走)

> 原则:**灰度、可回退、单点有兜底**。切换动的是全部 9 个生产定时任务,风险高、
> 不完全可逆。每步先在安静窗口(深夜/周末,`launchctl list | grep com.stock` 全 `-`)做。

### 阶段 A — 并行影子(已可做,零风险)
1. 开一个常驻影子:`$PY -m tools.scheduling run --heartbeat-min 30`(dry-run,不 `--live`)。
2. 连续观察数个交易日:比对影子日志"本该跑什么"与 launchd 实际产物/时刻是否一致
   (cron 时刻、slot、命令 argv)。发现口径差异 → 只改 `tasks.yaml`,不动 launchd。
3. **退出条件**:影子排期与 launchd 现状连续 N 日一致 → 具备切换资格。**回退**:关掉影子进程即可,launchd 全程未动。

### 阶段 B — 部署守护单 job(把 APScheduler 常驻起来,仍不停 launchd)
> 这一步需要新增 1 个 launchd 守护 plist(KeepAlive 拉起 `tools.scheduling run --live ...`)。
> 该 plist **尚未创建**(P3 交付物);创建后仍是**你本人 launchctl load**。此时 launchd 9 job 与
> APScheduler 会**双跑**——所以务必先把要交给 APScheduler 的任务在 `tasks.yaml` 里逐个
> 由 `enabled: false` 起步,避免重复执行产生冲突。

### 阶段 C — 逐 job 灰度切换(一次只切一个,盯一个交易日)
对**每一个**任务,按顺序(建议从低频、幂等、非重的先切,如 `strong`/`breadth`):
1. 安静窗口确认该 job 没在跑:`launchctl list | grep com.stock.<label>`(PID 列为 `-`)。
2. **停 launchd 侧**(卸载会 SIGTERM 杀正在跑的实例,故必须先确认没在跑):
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.stock.<label>.plist
   ```
3. **开 APScheduler 侧**:把 `tasks.yaml` 里对应任务改 `enabled: true`,常驻进程热重载生效。
4. 盯一个交易日:产物是否按时、正确、无重复;告警是否正常。
5. 有问题 → **回退**:`tasks.yaml` 改回 `enabled: false` + `launchctl load` 复原该 job。

### 阶段 D — 收编完成
- 全部 9 job 切完并稳定 → launchd 只剩 1 个守护单 job;下线冗余 plist(**保留文件存档**,不删,便于回滚)。
- 远端 systemd 同理:或复用同一 `tasks.yaml`(适配器分发本地/远端执行器),或各自维护——见待拍板点。

---

## 5. 待用户拍板点(P3 前需定)

1. 告警渠道选哪个:飞书 / 钉钉 / mail / Bark?(webhook url 走 env,放 chmod 600 文件)
2. Mac 睡眠保活:守护 plist 里加 `caffeinate`/`pmset` 常醒,还是接受"睡眠期不跑、唤醒 misfire 补跑"?
3. 本地与远端是否统一到一套 `tasks.yaml`(适配器分发),还是本地/远端各自维护?
4. 关键任务(如盘后全量 `pullrefresh`)切换后是否保留 launchd **双保险**一段时间?

## 6. 安全边界(本框架的硬约束)

- `run` 默认 dry-run;真执行必须显式 `--live`——防手滑触发生产。
- 框架**不含任何 launchctl / load / unload / 改 plist 代码**;切换全程人工。
- 凭证只存 env 变量名,值不入库;webhook url 不落日志。
- 与旧 `tools/scheduler.py`、live launchd **并存不冲突**(锁目录分离、默认影子)。
