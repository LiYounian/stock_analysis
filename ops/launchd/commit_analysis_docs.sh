#!/bin/bash
# 分析文档自动提交(选股/复盘/经验沉淀)——供 launchd 调用,工作日每小时扫,幂等。
# 设计:docs/计划/2026-09-07_分析文档自动提交_设计.md
# 硬约束:①只提交白名单 md(绝不 git add -A) ②独立 worktree 提交、不碰主仓 HEAD
#         ③脱敏扫描 ④无改动跳过 ⑤无 AI 署名 ⑥git 失败只记日志不外溢。
# ⚠️ 非投资建议,测试环境研究模拟。
set -uo pipefail   # 不用 -e:git 失败要自己兜、不外溢

MAIN_REPO="$(cd "$(dirname "$0")/../.." && pwd)"
# dailyjob 生产 worktree:launchd 午盘/盘后 wrapper 在此写产出(如午盘全A 日内全A_*.md),
# 它不在主仓工作树里 → 若不把这里也作为白名单 md 来源,这些产出永远进不了库。
DAILYJOB="${STOCK_DAILYJOB_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/dailyjob}"
WT="${STOCK_COMMITDOCS_WORKTREE:-$HOME/Documents/projects/worktrees/stock_analysis/commitdocs}"
LOG="${STOCK_COMMITDOCS_LOG:-$HOME/.local/state/stock/commitdocs.log}"
mkdir -p "$(dirname "$LOG")"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

PATHS=("docs/每日分析/选股" "docs/每日分析/复盘" "docs/每日分析/经验沉淀" "docs/每日分析/策略建议")

log "==== commit_analysis_docs 开始 ===="

# 1) fetch(共享对象库,不碰主仓分支/工作树)
git -C "$MAIN_REPO" fetch --quiet origin 2>/dev/null || log "!! git fetch 失败,用现有 origin/main"

# 2) 常驻 worktree 更到 origin/main(不存在则建)
if [ ! -e "$WT/.git" ]; then
  git -C "$MAIN_REPO" worktree add --detach "$WT" origin/main >>"$LOG" 2>&1 || { log "!! worktree 创建失败,中止"; exit 3; }
else
  git -C "$WT" reset --hard origin/main >>"$LOG" 2>&1 || { log "!! worktree reset 失败,中止"; exit 3; }
fi

# 3) 把两个产出来源(dailyjob 生产 worktree + 主仓工作树)白名单三目录的 *.md 覆盖 copy 进 worktree。
#    顺序:dailyjob 先拷、主仓后拷 → 同名文件以主仓为准(主仓是人/定时任务的权威副本,
#    行为与旧版字节一致);dailyjob-only 的产出(如午盘 日内全A_*)得以纳入,修复其从不入库的缺口。
for p in "${PATHS[@]}"; do
  mkdir -p "$WT/$p"
  [ -d "$DAILYJOB/$p" ] && cp -f "$DAILYJOB/$p/"*.md "$WT/$p/" 2>/dev/null || true
  [ -d "$MAIN_REPO/$p" ] && cp -f "$MAIN_REPO/$p/"*.md "$WT/$p/" 2>/dev/null || true
done

# 4) 只 add 白名单(绝不 -A)
cd "$WT" || { log "!! cd worktree 失败,中止"; exit 3; }
git add -- "${PATHS[@]}" 2>>"$LOG"
if git diff --cached --quiet; then
  log "无新增/改动分析文档,跳过"
  exit 0
fi
CHANGED=$(git diff --cached --name-only | tr '\n' ' ')
log "待提交: $CHANGED"

# 5) 脱敏扫描(labeled 敏感字段 + 正规身份证 + API key;不用裸11位数字避免误判财务数字)
if git diff --cached -U0 | grep -aEi \
  'sk-[A-Za-z0-9]{20,}|(api[_-]?key|secret|token|password|passwd|access[_-]?key)[[:space:]]*[:=][[:space:]]*[^[:space:]]|(手机|手机号|电话|微信|微信号|QQ|身份证)[[:space:]]*[:：][[:space:]]*[0-9A-Za-z]|[1-9][0-9]{5}(19|20)[0-9]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])[0-9]{3}[0-9Xx]' \
  >/dev/null 2>&1; then
  log "!! 脱敏扫描命中疑似敏感字段,中止提交(人工核查: $CHANGED)"
  exit 4
fi

# 6) commit + push(无 AI 署名)
git commit -q -m "docs(分析师): 自动提交选股/复盘/经验文档 $(date +%F)" >>"$LOG" 2>&1 || { log "!! commit 失败"; exit 5; }
if git push origin HEAD:main >>"$LOG" 2>&1; then
  log "✅ 已提交并推送: $CHANGED"
else
  # push 失败:worktree 本地 commit 下轮 reset 会丢,但文档仍在主仓,下轮重新 add 提交(自愈)。
  log "!! push 失败,下轮自愈重试: $CHANGED"
  exit 6
fi
log "==== 完成 ===="
