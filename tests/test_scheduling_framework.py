"""统一定时任务框架单测(架构⑥ P1-P2)。

锁语义(防未来 prompt/重写无意删规则):
1. 注册表解析:合法→TaskSpec;非法(缺字段/坏cron/未知字段/id重复/依赖悬空)→ fail-loud。
2. 热生效:改 cron 后 sync_jobs(replace_existing)→ trigger 变更。
3. 告警触发:任务失败/超时/内部异常→failure;misfire→misfire;成功且未声明success→不发。
4. misfire/coalesce/max_instances=1 参数正确注册。
5. **dry-run 不执行**:影子模式下命令执行器(_spawn)零调用——锁死"影子不碰生产"。
6. 凭证脱敏:WebhookNotifier 从 env 变量名解析;env 缺失 fail-soft 不崩、不外发。
全程不 start 调度、不触网、不起真子进程。
"""
from __future__ import annotations

import textwrap

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from tools.scheduling import load_registry
from tools.scheduling.models import NotifyEvent, SpecError, TaskSpec
from tools.scheduling.notifier import (
    LogNotifier,
    MultiNotifier,
    WebhookNotifier,
    build_notifier,
)
from tools.scheduling.registry import RegistryError
from tools.scheduling.runtime import (
    CommandRunner,
    build_scheduler,
    make_job,
    sync_jobs,
)


# ── 工具 ───────────────────────────────────────────────────────────────
def _write(tmp_path, body: str):
    p = tmp_path / "tasks.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _started_sched():
    """paused 启动的 BackgroundScheduler:jobs 落 jobstore,replace_existing 才生效
    (未 start 的调度 add_job 进 pending 列表,replace_existing 不去重,会留重复)。"""
    s = BackgroundScheduler()
    s.start(paused=True)
    return s


def _spec(**kw) -> TaskSpec:
    base = dict(id="t1", cmd=["python", "-m", "tools.run", "screen"],
                cron="30 10 * * mon-fri")
    base.update(kw)
    return TaskSpec(**base)


class _Recorder(LogNotifier):
    """记录收到的事件,便于断言。"""

    def __init__(self):
        self.events: list[NotifyEvent] = []

    def notify(self, event):
        self.events.append(event)

    def kinds(self):
        return [e.kind for e in self.events]


# ── 1. 注册表解析 / fail-loud ──────────────────────────────────────────
def test_registry_loads_shipped_tasks():
    reg = load_registry()  # 仓内 tasks.yaml
    ids = {t.id for t in reg.tasks}
    assert {"intraday", "pullrefresh", "commitdocs"} <= ids
    # sepa/autopush 声明存在但默认关(收编冗余但不参与影子排期)
    assert reg.by_id()["sepa"].enabled is False
    assert all(t.enabled for t in reg.enabled())


def test_registry_valid(tmp_path):
    p = _write(tmp_path, """
        tasks:
          - id: a
            cmd: ["python", "-m", "tools.run", "screen"]
            cron: "0 15 * * mon-fri"
          - id: b
            cmd: ["echo", "hi"]
            cron: "5 15 * * mon-fri"
            depends_on: [a]
    """)
    reg = load_registry(p)
    assert [t.id for t in reg.tasks] == ["a", "b"]


def test_bad_cron_fails_loud():
    with pytest.raises(SpecError):
        _spec(cron="99 99 * * *")


def test_missing_cmd_fails_loud():
    with pytest.raises(SpecError):
        TaskSpec(id="x", cmd=[], cron="0 10 * * mon-fri")


def test_unknown_field_fails_loud():
    with pytest.raises(SpecError):
        TaskSpec.from_dict({"id": "x", "cmd": ["a"], "cron": "0 10 * * mon-fri",
                            "bogus": 1})


def test_bad_notify_on_fails_loud():
    with pytest.raises(SpecError):
        _spec(notify_on=["failure", "explode"])


def test_duplicate_id_fails_loud(tmp_path):
    p = _write(tmp_path, """
        tasks:
          - {id: a, cmd: ["echo","1"], cron: "0 10 * * mon-fri"}
          - {id: a, cmd: ["echo","2"], cron: "0 11 * * mon-fri"}
    """)
    with pytest.raises(RegistryError):
        load_registry(p)


def test_dangling_depends_on_fails_loud(tmp_path):
    p = _write(tmp_path, """
        tasks:
          - {id: a, cmd: ["echo","1"], cron: "0 10 * * mon-fri", depends_on: [ghost]}
    """)
    with pytest.raises(RegistryError):
        load_registry(p)


def test_missing_file_fails_loud(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(tmp_path / "nope.yaml")


def test_top_level_not_mapping_fails_loud(tmp_path):
    p = tmp_path / "tasks.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(RegistryError):
        load_registry(p)


# ── 2. 周字段语义:mon-fri 命中工作日(锁 APScheduler 数字周的坑)──────
def test_weekday_semantics_monfri():
    import datetime as dt
    trig = _spec(cron="30 10 * * mon-fri").build_trigger()
    sunday = dt.datetime(2026, 9, 13, 0, 0).astimezone()
    nxt = trig.get_next_fire_time(None, sunday)
    assert nxt.strftime("%a") == "Mon"  # 周日之后应命中周一,而非(数字1-5的)周二


# ── 3. 热生效 ──────────────────────────────────────────────────────────
def test_cron_hot_reload_changes_trigger(tmp_path):
    p = _write(tmp_path, """
        tasks:
          - {id: a, cmd: ["echo","1"], cron: "0 10 * * mon-fri"}
    """)
    reg = load_registry(p)
    sched = _started_sched()
    runner = CommandRunner(dry_run=True)
    rec = _Recorder()
    sync_jobs(sched, reg, runner, rec)
    before = str(sched.get_job("a").trigger)
    # 改 cron 后重载 + 再同步
    _write(tmp_path, """
        tasks:
          - {id: a, cmd: ["echo","1"], cron: "45 15 * * mon-fri"}
    """)
    reg.reload()
    sync_jobs(sched, reg, runner, rec)
    after = str(sched.get_job("a").trigger)
    sched.shutdown(wait=False)
    assert before != after and "minute='45'" in after


def test_sync_removes_disabled(tmp_path):
    p = _write(tmp_path, """
        tasks:
          - {id: a, cmd: ["echo","1"], cron: "0 10 * * mon-fri"}
          - {id: b, cmd: ["echo","2"], cron: "0 11 * * mon-fri"}
    """)
    reg = load_registry(p)
    sched = _started_sched()
    sync_jobs(sched, reg, CommandRunner(dry_run=True), _Recorder())
    assert {j.id for j in sched.get_jobs()} == {"a", "b"}
    # b 改成 disabled → 再同步应被移除
    _write(tmp_path, """
        tasks:
          - {id: a, cmd: ["echo","1"], cron: "0 10 * * mon-fri"}
          - {id: b, cmd: ["echo","2"], cron: "0 11 * * mon-fri", enabled: false}
    """)
    reg.reload()
    sync_jobs(sched, reg, CommandRunner(dry_run=True), _Recorder())
    result = {j.id for j in sched.get_jobs()}
    sched.shutdown(wait=False)
    assert result == {"a"}


# ── 4. 调度参数正确 ─────────────────────────────────────────────────────
def test_job_params_registered():
    reg = load_registry()
    sched = build_scheduler(reg, _Recorder(), dry_run=True,
                            scheduler=BackgroundScheduler(), misfire_grace=1234)
    job = sched.get_job("intraday")
    assert job.misfire_grace_time == 1234
    assert job.coalesce is True
    assert job.max_instances == 1


def test_heartbeat_and_reload_jobs_optional():
    reg = load_registry()
    sched = build_scheduler(reg, _Recorder(), dry_run=True,
                            scheduler=BackgroundScheduler(),
                            heartbeat_min=5, reload_min=10)
    ids = {j.id for j in sched.get_jobs()}
    assert "__heartbeat__" in ids and "__reload__" in ids


# ── 5. dry-run 绝不执行(护栏)──────────────────────────────────────────
def test_dry_run_never_spawns(monkeypatch):
    calls = []
    monkeypatch.setattr(CommandRunner, "_spawn",
                        lambda self, spec: calls.append(spec.id) or 0)
    runner = CommandRunner(dry_run=True)
    res = runner.run(_spec())
    assert res.dry_run is True and calls == []  # 影子模式零子进程


# ── 6. 执行 / 重试 / 告警 ───────────────────────────────────────────────
def test_success_no_alert_when_not_subscribed():
    rec = _Recorder()
    runner = CommandRunner(dry_run=False)
    runner._spawn = lambda spec: 0
    spec = _spec(notify_on=["failure", "misfire"])  # 未订阅 success
    make_job(spec, runner, rec)()
    assert rec.events == []


def test_success_alerts_when_subscribed():
    rec = _Recorder()
    runner = CommandRunner(dry_run=False)
    runner._spawn = lambda spec: 0
    spec = _spec(notify_on=["success"])
    make_job(spec, runner, rec)()
    assert rec.kinds() == ["success"]


def test_failure_alerts_and_retries():
    rec = _Recorder()
    tries = []
    runner = CommandRunner(dry_run=False)
    runner._spawn = lambda spec: tries.append(1) or 1  # 恒非零
    spec = _spec(retries=2, retry_backoff_sec=0, notify_on=["failure"])
    make_job(spec, runner, rec)()
    assert len(tries) == 3          # 首次 + 2 重试
    assert rec.kinds() == ["failure"]
    assert rec.events[0].returncode == 1


def test_timeout_alerts_as_failure():
    import subprocess
    rec = _Recorder()

    def _boom(spec):
        raise subprocess.TimeoutExpired(cmd=spec.cmd, timeout=spec.timeout_sec)

    runner = CommandRunner(dry_run=False)
    runner._spawn = _boom
    spec = _spec(timeout_sec=1, retries=0, notify_on=["failure"])
    make_job(spec, runner, rec)()
    # 超时归 failure 语义(由 notify_on 的 failure 订阅门控),事件 kind 记为 timeout
    assert rec.kinds() == ["timeout"]


def test_internal_exception_alerts_not_crash():
    rec = _Recorder()
    runner = CommandRunner(dry_run=False)

    def _explode(spec):
        raise RuntimeError("unexpected")

    runner._spawn = _explode
    spec = _spec(retries=0, notify_on=["failure"])
    # 单实例锁获取正常;_spawn 抛非 Timeout 异常会冒泡到 make_job 兜底
    make_job(spec, runner, rec)()
    assert "failure" in rec.kinds()


def test_lock_prevents_overlap(tmp_path):
    runner = CommandRunner(dry_run=False, lock_root=tmp_path / "locks")
    runner._spawn = lambda spec: 0
    spec = _spec()
    lock = runner._acquire(spec)          # 手动占锁,模拟上一轮还在跑
    assert lock is not None
    res = runner.run(spec)                 # 再跑应跳过
    assert res.skipped_locked is True
    runner._release(lock)


# ── 7. 告警渠道 / 凭证脱敏 ──────────────────────────────────────────────
def test_webhook_resolves_from_env(monkeypatch):
    monkeypatch.setenv("MY_HOOK", "https://example.com/hook")
    sent = {}
    n = WebhookNotifier(kind="feishu", url_env="MY_HOOK")
    n._post = lambda url, payload: sent.update(url=url, payload=payload)
    n.notify(NotifyEvent(task_id="t", kind="failure", message="boom"))
    assert sent["url"] == "https://example.com/hook"
    assert sent["payload"]["msg_type"] == "text"  # 飞书格式


def test_webhook_missing_env_failsoft(monkeypatch):
    monkeypatch.delenv("MISSING_HOOK", raising=False)
    posted = []
    n = WebhookNotifier(kind="generic", url_env="MISSING_HOOK")
    n._post = lambda url, payload: posted.append(url)
    n.notify(NotifyEvent(task_id="t", kind="failure", message="boom"))
    assert posted == []  # 缺凭证 → 不外发、不抛


def test_build_notifier_default_is_log():
    n = build_notifier("log")
    assert isinstance(n, LogNotifier)


def test_build_notifier_multi_with_webhook(monkeypatch):
    n = build_notifier("log,feishu")
    assert isinstance(n, MultiNotifier)
    assert any(isinstance(x, WebhookNotifier) for x in n.notifiers)


def test_multinotifier_isolates_channel_errors():
    class Boom(LogNotifier):
        def notify(self, event):
            raise RuntimeError("channel down")

    rec = _Recorder()
    MultiNotifier([Boom(), rec]).notify(
        NotifyEvent(task_id="t", kind="failure", message="x"))
    assert rec.kinds() == ["failure"]  # 坏渠道不连累好渠道


def test_misfire_listener_emits(monkeypatch):
    """构造一个 EVENT_JOB_MISSED 事件,验证监听器对订阅任务外发 misfire。"""
    from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
    import datetime as dt
    reg = load_registry()
    rec = _Recorder()
    sched = build_scheduler(reg, rec, dry_run=True, scheduler=BackgroundScheduler())
    ev = JobExecutionEvent(EVENT_JOB_MISSED, "intraday", "default",
                           dt.datetime.now().astimezone())
    sched._dispatch_event(ev)  # 直接派发,不 start
    assert "misfire" in rec.kinds()
