"""执行器:把注册表编译成 APScheduler 常驻调度,并包装 锁/超时/重试/misfire/告警。

安全默认:**dry_run=True**(影子模式)——到点只记"本该跑什么",绝不真起子进程、
不碰 live launchd。要真跑须显式 dry_run=False(CLI 的 --live)。这是"只建不切"的护栏。

复用地基:
- APScheduler misfire_grace_time + coalesce + max_instances=1(漏跑补偿/防堆积/单实例)。
- add_job(replace_existing=True) → 改 cron 热生效。
- 单实例锁 = mkdir 锁(与 ops/launchd/lib/lock.sh 同思路,进程崩溃后 finally 释放)。
- job 级重试 = 失败指数退避重试(把采集层 _retry 的思想上提到调度层)。
- 告警 = notifier;失败/超时/misfire → NotifyEvent。
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from apscheduler.events import EVENT_JOB_MISSED

from tools.scheduling.models import NotifyEvent, TaskSpec
from tools.scheduling.notifier import Notifier
from tools.scheduling.registry import Registry

logger = logging.getLogger("scheduling.runtime")

# 锁根目录(与现有 launchd 锁同处 ~/.local/state/stock,互不冲突用独立子目录)
LOCK_ROOT = Path.home() / ".local" / "state" / "stock" / "scheduling_locks"
HEARTBEAT_JOB_ID = "__heartbeat__"
RELOAD_JOB_ID = "__reload__"


@dataclass
class RunResult:
    task_id: str
    ok: bool
    returncode: int | None
    duration_sec: float
    timed_out: bool = False
    skipped_locked: bool = False
    dry_run: bool = False
    error: str = ""


class CommandRunner:
    """执行一条 TaskSpec.cmd:单实例锁 + 超时 + 失败重试。dry_run 时只记录不执行。

    _spawn() 封装真正的子进程调用,单测覆盖此方法即可完全离线、且验证 dry-run 零调用。
    """

    def __init__(self, dry_run: bool = True, lock_root: Path = LOCK_ROOT) -> None:
        self.dry_run = dry_run
        self.lock_root = lock_root

    def _spawn(self, spec: TaskSpec) -> int:
        """真正起子进程,返回退出码。dry_run 下**绝不调用到这里**。"""
        proc = subprocess.run(spec.cmd, timeout=spec.timeout_sec, check=False)  # noqa: S603
        return proc.returncode

    def _acquire(self, spec: TaskSpec) -> Path | None:
        """mkdir 单实例锁;拿不到返回 None(表示已有实例在跑)。"""
        self.lock_root.mkdir(parents=True, exist_ok=True)
        lock = self.lock_root / f"{spec.id}.lock"
        try:
            lock.mkdir()
            return lock
        except FileExistsError:
            return None

    @staticmethod
    def _release(lock: Path | None) -> None:
        if lock is not None:
            try:
                lock.rmdir()
            except OSError:
                pass

    def run(self, spec: TaskSpec) -> RunResult:
        if self.dry_run:
            logger.info("[dry-run] 到点会执行 %s:%s(cron=%s)", spec.id,
                        " ".join(spec.cmd), spec.cron)
            return RunResult(spec.id, ok=True, returncode=None, duration_sec=0.0,
                             dry_run=True)

        lock = self._acquire(spec)
        if lock is None:
            logger.warning("[%s] 已有实例在跑,跳过本轮", spec.id)
            return RunResult(spec.id, ok=True, returncode=None, duration_sec=0.0,
                             skipped_locked=True)
        started = time.monotonic()
        try:
            attempts = spec.retries + 1
            last_rc: int | None = None
            for i in range(1, attempts + 1):
                try:
                    rc = self._spawn(spec)
                    last_rc = rc
                    if rc == 0:
                        return RunResult(spec.id, ok=True, returncode=0,
                                         duration_sec=time.monotonic() - started)
                    logger.warning("[%s] 第 %d/%d 次退出码 %s", spec.id, i, attempts, rc)
                except subprocess.TimeoutExpired:
                    logger.error("[%s] 第 %d/%d 次超时(>%ss)", spec.id, i, attempts,
                                 spec.timeout_sec)
                    if i >= attempts:
                        return RunResult(spec.id, ok=False, returncode=None,
                                         duration_sec=time.monotonic() - started,
                                         timed_out=True, error="timeout")
                if i < attempts:
                    delay = spec.retry_backoff_sec * (2 ** (i - 1))
                    time.sleep(delay)
            return RunResult(spec.id, ok=False, returncode=last_rc,
                             duration_sec=time.monotonic() - started,
                             error=f"returncode={last_rc}")
        finally:
            self._release(lock)


def _notify_result(notifier: Notifier, spec: TaskSpec, res: RunResult) -> None:
    """按 RunResult + spec.notify_on 决定是否外发告警。"""
    if res.skipped_locked:
        return
    if not res.ok:
        kind = "timeout" if res.timed_out else "failure"
        # timeout 也归入 failure 语义:只要声明关心 failure 就发
        if "failure" in spec.notify_on:
            notifier.notify(NotifyEvent(
                task_id=spec.id, kind=kind,
                message=f"任务执行失败:{' '.join(spec.cmd)}",
                returncode=res.returncode, duration_sec=res.duration_sec,
                detail={"error": res.error}))
    elif "success" in spec.notify_on and not res.dry_run:
        notifier.notify(NotifyEvent(
            task_id=spec.id, kind="success",
            message=f"任务成功:{' '.join(spec.cmd)}",
            returncode=res.returncode, duration_sec=res.duration_sec))


def make_job(spec: TaskSpec, runner: CommandRunner, notifier: Notifier):
    """把一条 spec 包装成 APScheduler 可调度的无参回调(捕获异常→告警,绝不让调度崩)。"""

    def _job() -> None:
        try:
            res = runner.run(spec)
        except Exception as e:  # noqa: BLE001 —— 兜底:未预期异常也要告警且不崩调度
            logger.exception("[%s] 执行器内部异常", spec.id)
            if "failure" in spec.notify_on:
                notifier.notify(NotifyEvent(task_id=spec.id, kind="failure",
                                            message=f"执行器内部异常:{e}"))
            return
        _notify_result(notifier, spec, res)

    _job.__name__ = f"job_{spec.id}"
    return _job


def _attach_misfire_listener(scheduler, registry: Registry, notifier: Notifier) -> None:
    """监听 APScheduler 漏跑事件 → 对声明关心 misfire 的任务外发告警。"""
    specs = registry.by_id()

    def _on_missed(event) -> None:
        spec = specs.get(event.job_id)
        if spec is None or "misfire" not in spec.notify_on:
            return
        notifier.notify(NotifyEvent(
            task_id=spec.id, kind="misfire",
            message=f"漏跑(超出 misfire 宽限):计划触发 {getattr(event, 'scheduled_run_time', '?')}"))

    scheduler.add_listener(_on_missed, EVENT_JOB_MISSED)


def sync_jobs(scheduler, registry: Registry, runner: CommandRunner, notifier: Notifier,
              misfire_grace: int = 3600) -> list[str]:
    """把注册表里启用的任务同步到 scheduler(add_job replace_existing);移除已删/禁用的任务。

    返回当前注册的业务任务 id 列表。改 cron 后重跑本函数即热生效。
    """
    wanted = {t.id: t for t in registry.enabled()}
    reserved = {HEARTBEAT_JOB_ID, RELOAD_JOB_ID}
    # 移除已不在启用集中的旧业务任务(保留框架自身 job)
    for job in scheduler.get_jobs():
        if job.id not in wanted and job.id not in reserved:
            scheduler.remove_job(job.id)
            logger.info("移除任务 %s(已删除或禁用)", job.id)
    for spec in wanted.values():
        scheduler.add_job(
            make_job(spec, runner, notifier), spec.build_trigger(), id=spec.id,
            misfire_grace_time=misfire_grace, coalesce=True, max_instances=1,
            replace_existing=True)
    logger.info("同步任务 %d 个:%s", len(wanted), sorted(wanted))
    return sorted(wanted)


def build_scheduler(registry: Registry, notifier: Notifier, *, dry_run: bool = True,
                    scheduler=None, misfire_grace: int = 3600,
                    heartbeat_min: int = 0, reload_min: int = 0):
    """按注册表构建 APScheduler(未 start)。

    dry_run     True=影子模式(默认,不真跑命令);False=真执行(CLI --live)。
    heartbeat_min >0 时加心跳 job(定期发存活告警,守护"常驻进程崩了没人知道")。
    reload_min    >0 时加热重载 job(定期检查 tasks.yaml mtime,变了就 reload+sync)。
    """
    if scheduler is None:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
    runner = CommandRunner(dry_run=dry_run)
    sync_jobs(scheduler, registry, runner, notifier, misfire_grace=misfire_grace)
    _attach_misfire_listener(scheduler, registry, notifier)

    if heartbeat_min and heartbeat_min > 0:
        def _hb() -> None:
            notifier.notify(NotifyEvent(task_id="scheduler", kind="heartbeat",
                                        message=f"调度常驻存活(dry_run={dry_run})"))
        scheduler.add_job(_hb, "interval", minutes=heartbeat_min, id=HEARTBEAT_JOB_ID,
                          coalesce=True, max_instances=1, replace_existing=True)

    if reload_min and reload_min > 0:
        def _reload() -> None:
            if not registry.changed():
                return
            try:
                registry.reload()
            except Exception as e:  # noqa: BLE001 —— 坏改动不该冲垮在跑的调度
                logger.error("注册表热重载失败,保留旧表:%s", e)
                notifier.notify(NotifyEvent(task_id="scheduler", kind="failure",
                                            message=f"注册表热重载失败:{e}"))
                return
            sync_jobs(scheduler, registry, runner, notifier, misfire_grace=misfire_grace)
            logger.info("注册表热重载完成")
        scheduler.add_job(_reload, "interval", minutes=reload_min, id=RELOAD_JOB_ID,
                          coalesce=True, max_instances=1, replace_existing=True)

    return scheduler
