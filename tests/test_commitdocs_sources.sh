#!/bin/bash
# 锁 commit_analysis_docs.sh「多产出来源合并」语义(守则6):
#   ① dailyjob-only 产出(午盘 日内全A_*)必须被纳入暂存 —— 修复其从不入库的缺口;
#   ② 主仓与 dailyjob 同名文件,以主仓为准(旧行为字节不变)。
# 只测第3步(拷贝合并)+第4步(git add)的核心逻辑,不触发 fetch/push/origin。
set -uo pipefail
FAIL=0
ok()  { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; FAIL=1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
P="docs/每日分析/选股"   # 用其中一个白名单目录做代表

MAIN="$TMP/main"; DJ="$TMP/dailyjob"; WT="$TMP/wt"
mkdir -p "$MAIN/$P" "$DJ/$P" "$WT/$P"
# WT 是个真 git 仓,才能 git add 验证暂存
git -C "$WT" init -q && git -C "$WT" config user.email t@t && git -C "$WT" config user.name t
git -C "$WT" config core.quotepath false   # 中文路径不转义成八进制,便于断言匹配

# 主仓:盯盘研判(权威),内容 = MAIN 版
printf 'MAIN-authoritative\n' > "$MAIN/$P/日内_2026-09-10.md"
# dailyjob:同名 日内_(内容不同,应被主仓覆盖) + 独有 日内全A(应被纳入)
printf 'DAILYJOB-stale\n'      > "$DJ/$P/日内_2026-09-10.md"
printf 'noon-fullA-output\n'   > "$DJ/$P/日内全A_2026-09-10.md"

# —— 被测逻辑(与脚本第3步一致:dailyjob 先、主仓后)——
mkdir -p "$WT/$P"
[ -d "$DJ/$P" ]   && cp -f "$DJ/$P/"*.md   "$WT/$P/" 2>/dev/null || true
[ -d "$MAIN/$P" ] && cp -f "$MAIN/$P/"*.md "$WT/$P/" 2>/dev/null || true
git -C "$WT" add -- "$P" 2>/dev/null

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

exit $FAIL
