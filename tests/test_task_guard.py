"""定时任务护栏 tools/ops/task_guard.sh 的语义锁。

为什么有这些断言（防未来改写时被无意删掉）：
- 后台定时会话在桌面 App 非活动时工具调用会被挂起数小时才执行，产出必须能自证漂移；
- 盘尾复盘与收盘选股会因各自漂移撞车（盘尾建经验新版，选股要读它），需要有界互斥；
- 护栏本身必须 best-effort——护栏坏了不许拖垮定时任务；
- 陈旧锁必须自动放行，否则一次崩溃会永久卡住后续任务。
设计见 docs/计划/定时任务时序治理.md
"""
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "tools" / "ops" / "task_guard.sh"


def run(*args, lock_dir, now=None, date=None, poll="0", extra_env=None):
    env = {
        "TASK_GUARD_LOCK_DIR": str(lock_dir),
        "TASK_GUARD_POLL_SECONDS": poll,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    if now:
        env["TASK_GUARD_NOW"] = now
    if date:
        env["TASK_GUARD_DATE"] = date
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [str(GUARD), *args], capture_output=True, text=True, env=env, cwd=str(REPO)
    )
    return proc


def kv(proc):
    out = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


@pytest.fixture
def lock_dir(tmp_path):
    d = tmp_path / "task_locks"
    d.mkdir()
    return d


# ---------- 漂移计算与标注 ----------

def test_begin_无漂移时不标late(lock_dir):
    r = run("begin", "t", "1030", lock_dir=lock_dir,
            now="2026-09-03T10:31:00", date="2026-09-03")
    o = kv(r)
    assert o["GUARD"] == "ok"
    assert o["DRIFT_FLAG"] == "ok", "1分钟漂移不该报 late"
    assert o["DRIFT_NOTE"] == "", "未 late 时不该产出标注文案"


def test_begin_漂移超30min标late并给出可直接抄进产出的标注(lock_dir):
    r = run("begin", "t", "1030", lock_dir=lock_dir,
            now="2026-09-03T13:50:09", date="2026-09-03")
    o = kv(r)
    assert o["DRIFT_FLAG"] == "late"
    assert int(o["DRIFT_SECONDS"]) == 3 * 3600 + 20 * 60 + 9
    assert o["DRIFT_HUMAN"] == "3h20m"
    # 标注必须同时含预期时刻与实际时刻，否则产出无法自证数据时点
    assert "10:30" in o["DRIFT_NOTE"] and "13:50" in o["DRIFT_NOTE"]


def test_begin_提前触发漂移为负且不标late(lock_dir):
    o = kv(run("begin", "t", "1510", lock_dir=lock_dir,
               now="2026-09-03T11:00:00", date="2026-09-03"))
    assert int(o["DRIFT_SECONDS"]) < 0
    assert o["DRIFT_FLAG"] == "ok"


# ---------- 锁的幂等与生命周期 ----------

def test_begin_幂等_重复调用不重置首次开工时刻与漂移(lock_dir):
    first = kv(run("begin", "t", "1030", lock_dir=lock_dir,
                   now="2026-09-03T10:31:00", date="2026-09-03"))
    second = kv(run("begin", "t", "1030", lock_dir=lock_dir,
                    now="2026-09-03T13:00:00", date="2026-09-03"))
    assert second["ACTUAL"] == first["ACTUAL"], "重复 begin 不该把开工时刻改成第二次的"
    assert second["DRIFT_SECONDS"] == first["DRIFT_SECONDS"]
    lock = json.loads((lock_dir / "t.2026-09-03.lock").read_text())
    assert lock["status"] == "running"
    assert lock["reentered_at"], "重复进入应留痕"


def test_end_写完成态与时长(lock_dir):
    run("begin", "t", "1030", lock_dir=lock_dir,
        now="2026-09-03T10:30:00", date="2026-09-03")
    o = kv(run("end", "t", lock_dir=lock_dir,
               now="2026-09-03T10:35:00", date="2026-09-03"))
    assert o["STATUS"] == "done"
    assert int(o["DURATION_SECONDS"]) == 300
    assert o["MISSING_BEGIN"] == "false"


def test_end_未begin也不报错并标记missing_begin(lock_dir):
    o = kv(run("end", "t", lock_dir=lock_dir, now="2026-09-03T10:35:00", date="2026-09-03"))
    assert o["GUARD"] == "ok"
    assert o["MISSING_BEGIN"] == "true", "缺 begin 要留痕，但不许失败"


# ---------- 互斥等待 ----------

def test_wait_无锁立即放行(lock_dir):
    o = kv(run("wait", "other", "30", lock_dir=lock_dir, date="2026-09-03"))
    assert o["RESULT"] == "ready" and o["REASON"] == "no_lock"


def test_wait_对方已完成立即放行(lock_dir):
    run("begin", "other", "1510", lock_dir=lock_dir,
        now="2026-09-03T15:10:00", date="2026-09-03")
    run("end", "other", lock_dir=lock_dir, now="2026-09-03T15:40:00", date="2026-09-03")
    o = kv(run("wait", "other", "30", lock_dir=lock_dir, date="2026-09-03"))
    assert o["RESULT"] == "ready" and o["REASON"] == "done"


def test_wait_对方在跑则超时返回timeout而非无限阻塞(lock_dir):
    run("begin", "other", "1510", lock_dir=lock_dir,
        now="2026-09-03T18:30:00", date="2026-09-03")
    o = kv(run("wait", "other", "0", lock_dir=lock_dir,
               now="2026-09-03T18:36:00", date="2026-09-03"))
    assert o["RESULT"] == "timeout", "选股任务必须能降级继续，不许被盘尾任务永久卡住"
    assert o["OTHER_STATUS"] == "running"


def test_wait_陈旧锁自动放行_防一次崩溃永久卡死(lock_dir):
    run("begin", "other", "1510", lock_dir=lock_dir,
        now="2026-09-03T15:10:00", date="2026-09-03")
    # 对方 running 但已过陈旧阈值（这里 60 分钟）→ 视为已挂掉
    o = kv(run("wait", "other", "30", "60", lock_dir=lock_dir,
               now="2026-09-03T18:36:00", date="2026-09-03"))
    assert o["RESULT"] == "ready" and o["REASON"] == "stale_lock"


def test_wait_锁按日隔离_昨天的锁不拦今天(lock_dir):
    run("begin", "other", "1510", lock_dir=lock_dir,
        now="2026-09-02T15:10:00", date="2026-09-02")
    o = kv(run("wait", "other", "30", lock_dir=lock_dir, date="2026-09-03"))
    assert o["RESULT"] == "ready" and o["REASON"] == "no_lock"


# ---------- best-effort：护栏坏了不许拖垮定时任务 ----------

def test_未知子命令退出0并报unavailable(lock_dir):
    r = run("bogus", lock_dir=lock_dir)
    assert r.returncode == 0
    assert kv(r)["GUARD"] == "unavailable"


def test_参数缺失退出0并报unavailable(lock_dir):
    r = run("begin", lock_dir=lock_dir)
    assert r.returncode == 0
    assert kv(r)["GUARD"] == "unavailable"


def test_python不可用时退出0并报unavailable(lock_dir):
    r = run("begin", "t", "1030", lock_dir=lock_dir,
            extra_env={"TASK_GUARD_PYTHON": "/nonexistent/python"})
    assert r.returncode == 0, "护栏自身故障绝不能让定时任务非0退出"


def test_status_未找到锁不报错(lock_dir):
    o = kv(run("status", "nope", lock_dir=lock_dir, date="2026-09-03"))
    assert o["RESULT"] == "not_found"
