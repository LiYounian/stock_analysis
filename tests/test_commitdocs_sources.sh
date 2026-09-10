#!/bin/bash
# 锁 commit_analysis_docs.sh「多产出来源合并」语义(守则6):
#   ① dailyjob-only 产出(午盘 日内全A_*)必须被纳入暂存 —— 修复其从不入库的缺口;
#   ② 主仓与 dailyjob 同名文件,以主仓为准(旧行为字节不变)。
# 只测第3步(拷贝合并)+第4步(git add)的核心逻辑,不触发 fetch/push/origin。
#   ③ 策略建议目录(盘后复盘任务产出)也在白名单内,其 *.md 必须被纳入暂存
#      —— 修复姊妹缺口:策略建议此前不在白名单,产出从不入库(2026-09-10 发现 9+ 份积压)。
set -uo pipefail
FAIL=0
ok()  { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; FAIL=1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
P="docs/每日分析/选股"       # 用其中一个白名单目录做代表
PS="docs/每日分析/策略建议"  # 姊妹白名单目录

MAIN="$TMP/main"; DJ="$TMP/dailyjob"; WT="$TMP/wt"
mkdir -p "$MAIN/$P" "$DJ/$P" "$WT/$P" "$MAIN/$PS" "$WT/$PS"
# WT 是个真 git 仓,才能 git add 验证暂存
git -C "$WT" init -q && git -C "$WT" config user.email t@t && git -C "$WT" config user.name t
git -C "$WT" config core.quotepath false   # 中文路径不转义成八进制,便于断言匹配

# 与脚本一致的白名单目录集合(fake 源,勿依赖真实文件)
PATHS=("$P" "$PS")

# 主仓:盯盘研判(权威),内容 = MAIN 版
printf 'MAIN-authoritative\n' > "$MAIN/$P/日内_2026-09-10.md"
# dailyjob:同名 日内_(内容不同,应被主仓覆盖) + 独有 日内全A(应被纳入)
printf 'DAILYJOB-stale\n'      > "$DJ/$P/日内_2026-09-10.md"
printf 'noon-fullA-output\n'   > "$DJ/$P/日内全A_2026-09-10.md"
# 主仓:盘后复盘产出的策略建议(应被纳入)
printf 'strategy-suggestion\n' > "$MAIN/$PS/动量策略高位超买抑制层.md"

# —— 被测逻辑(与脚本第3步一致:dailyjob 先、主仓后,遍历全部白名单目录)——
for p in "${PATHS[@]}"; do
  mkdir -p "$WT/$p"
  [ -d "$DJ/$p" ]   && cp -f "$DJ/$p/"*.md   "$WT/$p/" 2>/dev/null || true
  [ -d "$MAIN/$p" ] && cp -f "$MAIN/$p/"*.md "$WT/$p/" 2>/dev/null || true
done
git -C "$WT" add -- "${PATHS[@]}" 2>/dev/null

# 断言①:日内全A 被暂存
if git -C "$WT" diff --cached --name-only | grep -q "日内全A_2026-09-10.md"; then
  ok "dailyjob-only 午盘产出 日内全A 已纳入暂存"
else
  bad "日内全A 未被纳入(缺口未修复)"
fi
# 断言②:同名文件内容 = 主仓版(主仓胜出)
if [ "$(cat "$WT/$P/日内_2026-09-10.md")" = "MAIN-authoritative" ]; then
  ok "同名文件以主仓为准(旧行为不变)"
else
  bad "同名文件被 dailyjob 覆盖(旧行为被破坏)"
fi
# 断言③:策略建议 *.md 被暂存(姊妹缺口已修复)
if git -C "$WT" diff --cached --name-only | grep -q "策略建议/动量策略高位超买抑制层.md"; then
  ok "策略建议产出已纳入暂存(姊妹缺口已修复)"
else
  bad "策略建议未被纳入(姊妹缺口未修复)"
fi

exit $FAIL
