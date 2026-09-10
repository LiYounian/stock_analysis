#!/bin/bash
# 锁 ops/launchd/lib/lock.sh「陈旧锁抢占 / 活实例不误抢」语义(A3,守则6)。
#   诊断 §6.2:旧 `mkdir 锁 + trap EXIT rmdir` 在 SIGKILL 下 trap 不跑→锁残留→永久停摆。
#   新库要:死持有者/超龄持有者/老格式残留 → 清理抢占;活且未超龄的持有者 → 绝不误抢。
set -uo pipefail
FAIL=0
ok()  { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; FAIL=1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
LIB="$HERE/../ops/launchd/lib/lock.sh"
[ -f "$LIB" ] || { echo "FAIL: 找不到 lock.sh:$LIB"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
LOG="$TMP/log"; : > "$LOG"

# 在独立 bash 进程里抢锁($$ 才是该进程自身 pid;且 exit 0 只退子进程不退测试)。
# 成功:打印 "ACQUIRED <pid>" + owner 内容;跳过:acquire 内部 exit 0,子进程无输出。
run_acquire() {   # <lock> <log> [stale] [grace]
  local lock="$1" log="$2" stale="${3:-}" grace="${4:-}"
  STOCK_LOCK_MKDIR_GRACE="${grace:-${STOCK_LOCK_MKDIR_GRACE:-30}}" \
  bash -c '
    . "'"$LIB"'"
    acquire_lock_or_exit "'"$lock"'" "'"$log"'" '"$stale"'
    echo "ACQUIRED $$"
    cat "'"$lock"'/owner" 2>/dev/null
  '
}

mk_lock() {   # <lock> <pid> <start_epoch>  —— 手工造一个"持有中"的锁(不经 acquire,不自释放)
  local lock="$1" pid="$2" start="$3"
  mkdir -p "$lock"
  printf 'pid=%s\nstart=%s\nhost=t\n' "$pid" "$start" > "$lock/owner"
}

NOW="$(date +%s)"

# —— A:空目录 → 成功占锁,owner 写入自身 pid ——
L="$TMP/a.lock"
OUT="$(run_acquire "$L" "$LOG")"
if echo "$OUT" | grep -q "ACQUIRED" && echo "$OUT" | grep -q "^pid="; then
  ok "空锁可占,owner 记录 pid"
else bad "空锁应能占并写 owner(实得:$OUT)"; fi

# —— B:活且未超龄的持有者 → 绝不误抢(跳过) ——
sleep 300 & LIVE=$!
L="$TMP/b.lock"; mk_lock "$L" "$LIVE" "$NOW"
: > "$LOG"
OUT="$(run_acquire "$L" "$LOG")"
if ! echo "$OUT" | grep -q "ACQUIRED" \
   && grep -q "已有实例在跑" "$LOG" \
   && grep -q "^pid=$LIVE$" "$L/owner"; then
  ok "活实例持锁未被误抢(跳过,owner 未变)"
else bad "活实例不应被抢(OUT=$OUT / owner=$(cat "$L/owner"))"; fi
kill "$LIVE" 2>/dev/null; wait "$LIVE" 2>/dev/null

# —— C:持有者进程已死 → 判陈旧,清理抢占 ——
sleep 300 & DEAD=$!; kill "$DEAD" 2>/dev/null; wait "$DEAD" 2>/dev/null
L="$TMP/c.lock"; mk_lock "$L" "$DEAD" "$NOW"
: > "$LOG"
OUT="$(run_acquire "$L" "$LOG")"
if echo "$OUT" | grep -q "ACQUIRED" && grep -q "清理抢占" "$LOG"; then
  ok "死持有者锁被抢占"
else bad "死持有者应被抢占(OUT=$OUT / LOG=$(cat "$LOG"))"; fi

# —— D:持有者进程还活着但超龄(> stale)→ 判陈旧,抢占(挂死兜底) ——
sleep 300 & OLD=$!
L="$TMP/d.lock"; mk_lock "$L" "$OLD" "$(( NOW - 99999 ))"   # 超龄
: > "$LOG"
OUT="$(run_acquire "$L" "$LOG" 21600)"                       # stale=6h
if echo "$OUT" | grep -q "ACQUIRED" && grep -q "清理抢占" "$LOG"; then
  ok "超龄持有者(疑挂死)被抢占"
else bad "超龄持有者应被抢占(OUT=$OUT)"; fi
kill "$OLD" 2>/dev/null; wait "$OLD" 2>/dev/null

# —— E:老格式残留锁(无 owner 文件,目录很旧)→ 判陈旧,抢占 ——
L="$TMP/e.lock"; mkdir -p "$L"; touch -t 202001010000 "$L"  # mtime 拨到 2020 → 极老
: > "$LOG"
OUT="$(run_acquire "$L" "$LOG")"
if echo "$OUT" | grep -q "ACQUIRED"; then
  ok "老格式残留锁(无 owner、目录旧)被抢占"
else bad "老残留锁应被抢占(OUT=$OUT)"; fi

# —— F:无 owner 但锁刚建(宽限期内)→ 视为活实例 mid-write,不抢(闭 TOCTOU 竞态) ——
L="$TMP/f.lock"; mkdir -p "$L"                              # mtime=现在,新鲜
: > "$LOG"
OUT="$(run_acquire "$L" "$LOG" "" 30)"                       # grace=30s
if ! echo "$OUT" | grep -q "ACQUIRED" && grep -q "已有实例在跑" "$LOG"; then
  ok "新建无 owner 锁在宽限期内不误抢(闭 mkdir→写owner 竞态)"
else bad "宽限期内新锁不应被抢(OUT=$OUT)"; fi

# —— G:释放只清自己的锁 —— 直接单测 _lock_release ——
. "$LIB"
L="$TMP/g.lock"; mk_lock "$L" 99999999 "$NOW"               # 持有者 pid≠本 shell
_lock_release "$L"
if [ -d "$L" ]; then ok "释放不误删他人锁(pid≠\$\$ 保留)"; else bad "不应删他人锁"; fi
mk_lock "$L" "$$" "$NOW"                                     # 持有者=本 shell
_lock_release "$L"
if [ ! -d "$L" ]; then ok "释放清理自己的锁"; else bad "应清理自己的锁"; fi

echo "----"
[ "$FAIL" -eq 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit "$FAIL"
