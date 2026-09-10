# 开发日志 · 2026-09-10 · commitdocs 白名单补入「策略建议」

> 实现窗(worktree youthful-nobel-ad711a,分支 fix/commitdocs-strategy-whitelist)。
> 权威设计:[2026-09-07_分析文档自动提交_设计.md](2026-09-07_分析文档自动提交_设计.md)。
> ⚠️ 测试环境研究模拟,非投资建议。

## 本轮做了什么(为什么)

自动提交脚本 `ops/launchd/commit_analysis_docs.sh`(launchd `com.stock.commitdocs`,工作日每小时跑)
把分析文档 commit+push 到 main。白名单原为选股/复盘/经验沉淀三目录。

今天统筹先修了一个缺口(午盘全A产出 `日内全A_*` 因写在 dailyjob worktree、主仓扫不到而从不入库;
已改成 dailyjob+主仓两来源合并,见脚本第 3 步)。排查中发现**同型姊妹缺口**:
`docs/每日分析/策略建议/*.md`(盘后复盘任务产出的策略建议)**不在白名单**,17 份从未入库、
只躺主仓工作树未跟踪。

### 修复
1. `ops/launchd/commit_analysis_docs.sh` —— `PATHS` 数组补入 `docs/每日分析/策略建议`。
   下一次 commitdocs 跑到,积压 + 后续策略建议自动入库。
2. 脱敏扫描(脚本第 5 步 `git diff --cached -U0 | grep -aEi ...`)扫的是**全部暂存内容**,
   天然覆盖新目录 → 无需额外改动(已确认)。
3. `tests/test_commitdocs_sources.sh` —— 加断言③:策略建议 `*.md`(fake 源)也被纳入暂存;
   被测拷贝/add 逻辑改为遍历全部白名单目录(与脚本第 3、4 步一致)。三条断言全绿。
4. 设计文档白名单条目同步补入策略建议目录。

## 预期效果
下一次 commitdocs 跑到,积压的策略建议 md + 后续产出自动入库,不再漏。

## 交付
- 改动:`ops/launchd/commit_analysis_docs.sh`、`tests/test_commitdocs_sources.sh`、
  `docs/计划/2026-09-07_分析文档自动提交_设计.md`、本日志。
- 分步 commit,留分支等统筹核验后合(未自合 main)。
