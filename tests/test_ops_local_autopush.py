"""本地闭环单测(ops.local_autopush)+ launchd plist 合法性。

锁住本地可验证部分:全池命令构造 / 流水线→上传 顺序 / 流水线失败则不上传 /
--no-pipeline 跳过流水线仍上传 / 参数默认 / plist 结构合法(工作日 15:30)。
runner 与 upload_fn 注入,不真跑流水线、不触网。
"""
import plistlib
from pathlib import Path

from ops import local_autopush

_PLIST = Path(__file__).resolve().parents[1] / "ops" / "launchd" / "com.stock.autopush.plist"


def test_build_pipeline_cmd_full_pool():
    cmd = local_autopush.build_pipeline_cmd("/venv/py", all_pool=True)
    assert cmd == ["/venv/py", "-m", "tools.run", "all", "--all"]
    assert local_autopush.build_pipeline_cmd("/venv/py", all_pool=False) == \
        ["/venv/py", "-m", "tools.run", "all"]


def _ok_upload(date, **kw):
    return {"date": date, "shards": {}, "summary": {"total": 3, "ok": 3, "failed": 0}}


def test_pipeline_then_upload_in_order():
    calls = []

    def runner(cmd):
        calls.append(("pipeline", cmd))
        return 0, "done"

    def upload_fn(date, **kw):
        calls.append(("upload", date))
        return _ok_upload(date)

    res = local_autopush.run_local_push(
        "2026-08-08", python="/venv/py", url="u", token="t", source="s",
        key_id="k1", key="K", runner=runner, upload_fn=upload_fn)
    assert res["ok"] and res["step"] == "done"
    assert [c[0] for c in calls] == ["pipeline", "upload"]      # 先流水线后上传
    assert "--all" in calls[0][1]                                # 默认全池


def test_pipeline_failure_aborts_upload():
    uploaded = []

    def runner(cmd):
        return 1, "boom"                                         # 流水线失败

    def upload_fn(date, **kw):
        uploaded.append(date)
        return _ok_upload(date)

    res = local_autopush.run_local_push(
        "2026-08-08", python="/venv/py", url="u", token="t", source="s",
        key_id="k1", key="K", runner=runner, upload_fn=upload_fn)
    assert res["ok"] is False and res["step"] == "pipeline"
    assert uploaded == []                                        # 失败不上传


def test_no_pipeline_skips_to_upload():
    ran = []

    def runner(cmd):
        ran.append(cmd)
        return 0, ""

    res = local_autopush.run_local_push(
        "2026-08-08", python="/venv/py", url="u", token="t", source="s",
        key_id="k1", key="K", run_pipeline=False, runner=runner, upload_fn=_ok_upload)
    assert res["ok"] and ran == []                               # 跳过流水线


def test_upload_failure_reported():
    def upload_fn(date, **kw):
        return {"summary": {"total": 3, "ok": 2, "failed": 1}}
    res = local_autopush.run_local_push(
        "2026-08-08", python="/venv/py", url="u", token="t", source="s",
        key_id="k1", key="K", run_pipeline=False, upload_fn=upload_fn)
    assert res["ok"] is False and res["step"] == "upload"


def test_today_format():
    import datetime
    assert local_autopush._today() == datetime.datetime.now().strftime("%Y-%m-%d")


# —— launchd plist 合法性 ——
def test_plist_valid_and_scheduled():
    with open(_PLIST, "rb") as f:
        data = plistlib.load(f)
    assert data["Label"] == "com.stock.autopush"
    assert data["ProgramArguments"][0] == "/bin/bash"
    assert data["ProgramArguments"][1].endswith("ops/launchd/autopush.sh")
    sched = data["StartCalendarInterval"]
    assert len(sched) == 5                                       # 工作日 5 天
    assert all(d["Hour"] == 15 and d["Minute"] == 30 for d in sched)
    assert sorted(d["Weekday"] for d in sched) == [1, 2, 3, 4, 5]
