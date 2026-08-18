"""定时调度单测:按 settings 注册任务(不真启动、不触网)。

锁定:间隔 >0 才注册对应任务;间隔 <=0 禁用;注册间隔与配置一致。
"""
from apscheduler.schedulers.background import BackgroundScheduler

from tools import scheduler
from tools.config import settings


def _new():
    return BackgroundScheduler()      # 仅构造,不 start,无后台线程


def test_full_job_registered(monkeypatch):
    monkeypatch.setattr(settings, "SCHED_FULL_INTERVAL_MIN", 60)
    monkeypatch.setattr(settings, "SCHED_BACKFILL_INTERVAL_MIN", 0)
    monkeypatch.setattr(settings, "SCHED_SEPA_ENABLED", False)
    ids = {j.id for j in scheduler.build_scheduler(_new()).get_jobs()}
    assert "full" in ids and "backfill" not in ids


def test_backfill_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "SCHED_FULL_INTERVAL_MIN", 60)
    monkeypatch.setattr(settings, "SCHED_BACKFILL_INTERVAL_MIN", 30)
    monkeypatch.setattr(settings, "SCHED_SEPA_ENABLED", False)
    ids = {j.id for j in scheduler.build_scheduler(_new()).get_jobs()}
    assert {"full", "backfill"} <= ids


def test_disabled_when_interval_zero(monkeypatch):
    monkeypatch.setattr(settings, "SCHED_FULL_INTERVAL_MIN", 0)
    monkeypatch.setattr(settings, "SCHED_BACKFILL_INTERVAL_MIN", 0)
    monkeypatch.setattr(settings, "SCHED_SEPA_ENABLED", False)
    assert scheduler.build_scheduler(_new()).get_jobs() == []


def test_interval_matches_config(monkeypatch):
    monkeypatch.setattr(settings, "SCHED_FULL_INTERVAL_MIN", 15)
    monkeypatch.setattr(settings, "SCHED_BACKFILL_INTERVAL_MIN", 0)
    job = scheduler.build_scheduler(_new()).get_job("full")
    assert int(job.trigger.interval.total_seconds()) == 15 * 60


def test_sepa_cron_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "SCHED_FULL_INTERVAL_MIN", 0)
    monkeypatch.setattr(settings, "SCHED_BACKFILL_INTERVAL_MIN", 0)
    monkeypatch.setattr(settings, "SCHED_SEPA_ENABLED", True)
    ids = {j.id for j in scheduler.build_scheduler(_new()).get_jobs()}
    assert ids == {"sepa_noon", "sepa_close"}
