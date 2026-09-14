# 已退役的 launchd 任务(代码存档)

本目录存放已退役但保留留痕、可回滚的 launchd 任务 plist 及其 wrapper 脚本。
文件从 `ops/launchd/` `git mv` 到此,历史可追溯。

## com.stock.strong(20:00 最强选股补跑)——退役于 2026-09-14

- **文件**:`com.stock.strong.plist`(工作日 20:00,Weekday 1-5)+ `strong_refresh.sh`(wrapper)。
- **原职责**:傍晚 Tushare 筹码 `cyq_perf` 发布后,重跑 S05「最强选股」并只补传该单个 view 分片。
- **为什么退役**:
  1. `STRONG_CHIP_SOURCE` 默认已切 `local`(本地 `chip.py` 推演),15:46 盘后主闭环当场即算出
     S05「最强选股」,不再需要等傍晚 Tushare 发布筹码。
  2. 20:00 补跑对本地源而言纯冗余;更糟的是它会 **overwrite** 用户 18:36 已读的「最强选股」视图
     (覆盖 bug)。退役后此覆盖 bug 一并消除。
  3. `tushare` 源仍作为可选兜底保留(`STRONG_CHIP_SOURCE=tushare`),需要时一键回退。
- **退役边界**:本目录只做 repo 侧留痕归档;实际 `launchctl unload` + 从 `~/Library/LaunchAgents`
  移除 plist 由用户本人手动完成(agent 不碰 launchctl / 不碰 `~/Library`)。
- **如需恢复**:`git mv` 回 `ops/launchd/`,去掉顶部"已退役"横幅,再由用户手动重新装载。
