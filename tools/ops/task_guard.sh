#!/usr/bin/env bash
# 定时任务护栏（单一真源）：漂移标注 + 任务互斥。
# 设计与动机见 docs/计划/定时任务时序治理.md
#
#   task_guard.sh begin  <task> <期望HHMM>            # 写运行锁 + 算漂移，输出 KEY=VALUE
#   task_guard.sh end    <task>                        # 标记完成
#   task_guard.sh wait   <task> <最多分钟> [陈旧分钟]  # 有界等待另一任务跑完
#   task_guard.sh status <task>                        # 查锁
#
# 全部子命令都是 best-effort 语义：任何内部异常都退出 0 并输出 GUARD=unavailable，
# 绝不因为护栏本身出问题而拖垮定时任务（护栏缺失时任务照常跑，只是没有漂移标注与互斥）。
#
# 测试用环境变量：TASK_GUARD_NOW(ISO本地时刻) / TASK_GUARD_DATE(YYYY-MM-DD) / TASK_GUARD_POLL_SECONDS
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOCK_DIR="${TASK_GUARD_LOCK_DIR:-$ROOT/data/task_locks}"
PY="${TASK_GUARD_PYTHON:-python3}"

_run_py() {
  # $1=子命令名，其余参数透传给 python；python 侧统一处理 JSON 与时间数学
  TASK_GUARD_LOCK_DIR="$LOCK_DIR" "$PY" - "$@" <<'PY'
import json, os, sys, time, datetime as dt

LOCK_DIR = os.environ["TASK_GUARD_LOCK_DIR"]

def now():
    raw = os.environ.get("TASK_GUARD_NOW")
    return dt.datetime.fromisoformat(raw) if raw else dt.datetime.now()

def today():
    raw = os.environ.get("TASK_GUARD_DATE")
    return raw if raw else now().strftime("%Y-%m-%d")

def lock_path(task):
    return os.path.join(LOCK_DIR, f"{task}.{today()}.lock")

def read_lock(task):
    try:
        with open(lock_path(task), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None

def write_lock(task, payload):
    os.makedirs(LOCK_DIR, exist_ok=True)
    tmp = lock_path(task) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, lock_path(task))

def human(seconds):
    seconds = int(abs(seconds))
    h, m = seconds // 3600, (seconds % 3600) // 60
    return (f"{h}h{m:02d}m" if h else f"{m}m") + (f"{seconds % 60}s" if seconds < 60 else "")

def emit(**kw):
    for k, v in kw.items():
        print(f"{k}={v}")

def cmd_begin(task, expected_hhmm):
    n = now()
    expected = dt.datetime.strptime(f"{today()} {expected_hhmm}", "%Y-%m-%d %H%M")
    drift = int((n - expected).total_seconds())
    prev = read_lock(task)
    if prev and prev.get("status") == "running":
        # 幂等：重复 begin 不重置首次开工时刻与漂移，只记再进入
        prev.setdefault("reentered_at", [])
        prev["reentered_at"].append(n.isoformat(timespec="seconds"))
        write_lock(task, prev)
        drift = int(prev.get("drift_seconds", drift))
        expected_out, actual_out = prev.get("expected_at", expected.isoformat()), prev.get("started_at", n.isoformat())
    else:
        write_lock(task, {
            "task": task, "date": today(), "status": "running",
            "expected_at": expected.isoformat(timespec="seconds"),
            "started_at": n.isoformat(timespec="seconds"),
            "finished_at": None, "drift_seconds": drift,
            "session_pid": os.environ.get("TASK_GUARD_PPID", ""),
        })
        expected_out, actual_out = expected.isoformat(timespec="seconds"), n.isoformat(timespec="seconds")
    late = drift > 1800
    note = ""
    if late:
        note = (f"⚠️ 本任务预期 {expected_hhmm[:2]}:{expected_hhmm[2:]} 触发，实际 {n.strftime('%H:%M')} 执行，"
                f"漂移 {human(drift)}；数据采集时点=实际执行时点，非预期时点。")
    emit(GUARD="ok", TASK=task, EXPECTED=expected_out, ACTUAL=actual_out,
         DRIFT_SECONDS=drift, DRIFT_HUMAN=human(drift),
         DRIFT_FLAG=("late" if late else "ok"), DRIFT_NOTE=note)

def cmd_end(task):
    n = now()
    lock = read_lock(task) or {"task": task, "date": today(), "missing_begin": True,
                               "expected_at": None, "started_at": n.isoformat(timespec="seconds"),
                               "drift_seconds": None}
    lock["status"] = "done"
    lock["finished_at"] = n.isoformat(timespec="seconds")
    try:
        started = dt.datetime.fromisoformat(lock["started_at"])
        lock["duration_seconds"] = int((n - started).total_seconds())
    except Exception:
        lock["duration_seconds"] = None
    write_lock(task, lock)
    emit(GUARD="ok", TASK=task, STATUS="done",
         DURATION_SECONDS=lock.get("duration_seconds"),
         MISSING_BEGIN=str(bool(lock.get("missing_begin"))).lower())

def cmd_wait(task, max_minutes, stale_minutes="300"):
    max_s, stale_s = float(max_minutes) * 60, float(stale_minutes) * 60
    poll = float(os.environ.get("TASK_GUARD_POLL_SECONDS", "30"))
    t0 = time.time()
    while True:
        lock = read_lock(task)
        if lock is None:
            return emit(GUARD="ok", RESULT="ready", REASON="no_lock", OTHER_STATUS="absent",
                        WAITED_SECONDS=int(time.time() - t0))
        if lock.get("status") == "done":
            return emit(GUARD="ok", RESULT="ready", REASON="done", OTHER_STATUS="done",
                        WAITED_SECONDS=int(time.time() - t0))
        try:
            age = (now() - dt.datetime.fromisoformat(lock["started_at"])).total_seconds()
        except Exception:
            age = 0.0
        if age > stale_s:
            # 陈旧锁视为已挂掉：否则一次崩溃会永久卡住后续任务
            return emit(GUARD="ok", RESULT="ready", REASON="stale_lock", OTHER_STATUS="running_stale",
                        OTHER_AGE_SECONDS=int(age), WAITED_SECONDS=int(time.time() - t0))
        if time.time() - t0 >= max_s:
            return emit(GUARD="ok", RESULT="timeout", REASON="still_running", OTHER_STATUS="running",
                        OTHER_STARTED_AT=lock.get("started_at"), WAITED_SECONDS=int(time.time() - t0))
        if poll > 0:
            time.sleep(poll)

def cmd_status(task):
    lock = read_lock(task)
    if lock is None:
        return emit(GUARD="ok", RESULT="not_found", TASK=task)
    print(json.dumps(lock, ensure_ascii=False))

try:
    sub, args = sys.argv[1], sys.argv[2:]
    {"begin": cmd_begin, "end": cmd_end, "wait": cmd_wait, "status": cmd_status}[sub](*args)
except Exception as exc:  # best-effort：护栏坏了也不许拖垮定时任务
    print(f"GUARD=unavailable\nGUARD_ERROR={type(exc).__name__}: {exc}")
PY
}

case "${1:-}" in
  begin|end|wait|status) TASK_GUARD_PPID="$PPID" _run_py "$@" ;;
  *) echo "GUARD=unavailable"; echo "GUARD_ERROR=usage: task_guard.sh {begin|end|wait|status} ..." ;;
esac
exit 0
