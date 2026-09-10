# 部署卫生 ③b · launchd 人工装载 runbook

> 面向:统筹 / 用户(手动执行 launchctl 的人)。**agent 不执行本文任何 launchctl / provision 命令**——
> 本文是交人工照做的操作手册。
> 关联:设计 [../计划/2026-09-10_部署卫生根治_设计.md]、规格 [../计划/2026-09-10_部署卫生根治_实现规格.md]。
> 前提:本轮改动已合入 origin/main(bootstrap/provision/9 个 plist 均在 origin/main 上)。
> ⚠️ 非投资建议,研究模拟工程。

---

## 0. 这套机制在做什么(一句话)

launchd 触发 → 执行一个**装在 repo 外固定位置**的薄 bootstrap → bootstrap 把专用 **deploy worktree**
`reset --hard origin/main` → `exec` 该 worktree 内最新的 `ops/launchd/<wrapper>.sh`。
于是**被执行的 wrapper 本体恒等最新 origin/main**,主仓工作树脏不脏、卡不卡,都不再影响部署。

涉及路径(本机):
- deploy worktree:`~/Documents/projects/worktrees/stock_analysis/deploy`(可用 `STOCK_DEPLOY_WORKTREE` 覆盖)
- 已安装 bootstrap:`~/.local/state/stock/bin/stock-launchd-bootstrap.sh`(可用 `STOCK_LAUNCHD_BIN` 覆盖目录)
- plist:`~/Library/LaunchAgents/com.stock.*.plist`(装载态)← 源在 repo `ops/launchd/com.stock.*.plist`

---

## 1. 护栏:改任何 job 前先查它是否在跑

`launchctl unload` 会给正在跑的实例发 SIGTERM。**先查,避开运行窗口**:

```bash
launchctl list | grep com.stock
```

- 第 1 列是 PID:**非 `-` 表示正在跑**(如盘后 `com.stock.pullrefresh` 15:40 起可能跑 ~73min)。
- 对正在跑的 job:**等它跑完再 unload/load**,或改在该 job 的非触发时段操作。
- 各 job 触发时点见对应 plist 的 `StartCalendarInterval`(pullrefresh 15:40、breadth 15:05、
  intraday 10:30、intraday_noon 11:31、intraday_screen 11:32、intraday_watch 13:00、
  intraday_review 17:00、strong 20:00、commitdocs 工作日 11:20–22:20 每小时)。

---

## 2. 一次性:provision(建 deploy worktree + 安装 bootstrap)

```bash
bash ~/Documents/projects/stock_analysis/ops/launchd/provision_deploy.sh
```

幂等,可重复跑。成功后应看到:
- `~/Documents/projects/worktrees/stock_analysis/deploy/.git` 存在;
- `~/.local/state/stock/bin/stock-launchd-bootstrap.sh` 存在且可执行。

> provision 只对 deploy worktree 操作,**绝不碰主仓 HEAD/工作树/stash 栈**。

---

## 3. 逐 job 装载(unload 旧 → 覆盖 plist 源 → load 新)

对每个 job 重复以下步骤(先做第 1 节的"是否在跑"检查)。以 `pullrefresh` 为例:

```bash
LABEL=com.stock.pullrefresh
SRC=~/Documents/projects/stock_analysis/ops/launchd/$LABEL.plist
DST=~/Library/LaunchAgents/$LABEL.plist

# 3.1 若在跑,先等它跑完(见第 1 节)
# 3.2 卸载旧的(未装载则忽略报错)
launchctl unload "$DST" 2>/dev/null || true
# 3.3 用 repo 内最新 plist 覆盖装载态副本
cp -f "$SRC" "$DST"
# 3.4 校验并装载
plutil -lint "$DST"
launchctl load "$DST"
# 3.5 确认已装载
launchctl list | grep "$LABEL"
```

全部 9 个 job 的 LABEL:

```
com.stock.pullrefresh
com.stock.breadth
com.stock.strong
com.stock.commitdocs
com.stock.intraday
com.stock.intraday_noon
com.stock.intraday_review
com.stock.intraday_screen
com.stock.intraday_watch
```

> 说明:`com.stock.intraday` 与 `com.stock.intraday_noon` 复用同一 wrapper `intraday_snapshot.sh`,
> 靠各自 plist 的 `INTRADAY_SLOT`(1030 / 1145)区分,装载时互不影响。

---

## 4. 验收(装载后)

1. **静态**:`~/Library/LaunchAgents/com.stock.*.plist` 的 `ProgramArguments` 应为
   `[/bin/bash, ~/.local/state/stock/bin/stock-launchd-bootstrap.sh, <wrapper>.sh]`
   (无旧的主仓/worktree 直连路径、无归正前的多余第二路径)。
   ```bash
   for p in ~/Library/LaunchAgents/com.stock.*.plist; do echo "== $p =="; /usr/libexec/PlistBuddy -c "Print :ProgramArguments" "$p"; done
   ```
2. **手动触发一个低副作用 job 试跑**(如 commitdocs,幂等、无采集):
   ```bash
   launchctl start com.stock.commitdocs
   tail -n 40 ~/.local/state/stock/commitdocs.out.log ~/.local/state/stock/commitdocs.err.log
   ```
   预期:bootstrap 日志显示 reset deploy worktree 到最新 origin/main,随后真 wrapper 正常执行。
3. **部署闭环验证**(可选,确认"改 wrapper 即部署"):往 origin/main 合一个只加日志行的 wrapper 小改,
   下一次该 job 触发时日志应出现新行,**无需任何手动 checkout**。

---

## 5. 回滚

- **单 job 回滚**:把该 `com.stock.<x>.plist` 的 `ProgramArguments` 改回直连主仓 wrapper 路径
  (`[/bin/bash, ~/Documents/projects/stock_analysis/ops/launchd/<wrapper>.sh]`),`unload`→`cp`→`load`。
- **整体回滚**:9 个 plist 全部按上句改回并重载;deploy worktree 与已安装 bootstrap 可保留(不被引用即无副作用),
  或 `git -C <主仓> worktree remove <deploy 路径>` + `git worktree prune` 清掉。
- bootstrap fail-loud:deploy worktree 缺失→job 退 3、wrapper 缺失→退 4,**不会静默跑旧码**;
  看到退 3/4 先跑第 2 节 provision 修复。

---

## 6. 未纳入本轮(记待续)

- `com.stock.autopush`(数据签名上传,`__REPO__` 占位模板 + 当前未装载)、`com.stock.sepa`(已停用):
  本轮**未迁**。若将来重新启用 autopush,需一并迁到本 bootstrap 形态(它同样有"wrapper 本体从主仓读"的隐患)。
- **①a(分析会话不再往主仓工作树写未跟踪文档)** 下一轮做——本轮 ③b 已把 wrapper 部署与主仓 HEAD 解耦,
  部署不再依赖主仓 ff-only;主仓 ff-only 本身的治理留 ①a。
