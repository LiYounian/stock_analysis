"""进程内定时调度(APScheduler):按配置间隔自动跑流水线,产物落所选 store 后端。

把原本手动的 `python -m tools.run all` 变成按配置刷新时间的定时任务。
对齐 架构 §8.2 定时分层:本期落地 **T1 盘后全量** + **T5 兜底补数(可选)**;
更细的 T2 情绪 / T3 盘中 / T4 基本面 留待后续按同一模式扩展。

间隔全部走 settings(env 可配,分钟;<=0 表示禁用该任务)。
STORE_BACKEND=db 时,流水线产出的记录/视图直接入库(见 tools/store)。

启动(前台常驻,Ctrl-C 退出):
    SCHED_ENABLED=true SCHED_FULL_INTERVAL_MIN=60 python -m tools.scheduler
"""
from __future__ import annotations

import logging

from tools.config import settings

logger = logging.getLogger("scheduler")


def _reload_pool() -> None:
    """每轮跑前从磁盘重载票池,确保读到「票池管理」界面的最新增删(常驻进程免重启)。"""
    from tools.config import stock_pool
    stock_pool.reload()
    logger.info("票池已重载:%d 只(源 config/stock_pool.json)", len(stock_pool.get_codes()))


def _run_full() -> None:
    """T1 盘后全量:采集 → 情绪 → 组装 → 视图。异常只记日志,不让调度器崩。"""
    from tools import run
    argv = ["scheduler"] + (["--all"] if settings.SCHED_FULL_ALL else [])
    try:
        _reload_pool()
        logger.info("[T1] 全量任务开始(后端=%s,全池=%s)", settings.STORE_BACKEND,
                    settings.SCHED_FULL_ALL)
        run.cmd_all(argv)
        logger.info("[T1] 全量任务完成")
    except Exception:
        logger.exception("[T1] 全量任务失败(已捕获,不影响后续调度)")


def _run_backfill() -> None:
    """T5 兜底补数:重跑采集(幂等,补上轮失败票,如资金流限流)。"""
    from tools import run
    argv = ["scheduler"] + (["--all"] if settings.SCHED_FULL_ALL else [])
    try:
        _reload_pool()
        logger.info("[T5] 兜底补数开始")
        run.cmd_collect(argv)
        logger.info("[T5] 兜底补数完成")
    except Exception:
        logger.exception("[T5] 兜底补数失败(已捕获)")


# 任务表:id → (回调, 间隔配置项名)。加新层(T2/T3/T4)在此登记即可。
_JOBS = [
    ("full", _run_full, "SCHED_FULL_INTERVAL_MIN"),
    ("backfill", _run_backfill, "SCHED_BACKFILL_INTERVAL_MIN"),
]


def build_scheduler(scheduler=None):
    """按 settings 把启用的任务注册到 scheduler(缺省新建 BackgroundScheduler)。

    间隔 <=0 的任务跳过不注册。coalesce+max_instances=1 防止堆积/并发重入;
    misfire_grace_time 让错过的触发在宽限内补跑一次。返回 scheduler(未 start)。
    """
    if scheduler is None:
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
    grace = settings.SCHED_MISFIRE_GRACE_SEC
    for job_id, fn, interval_attr in _JOBS:
        minutes = getattr(settings, interval_attr)
        if minutes and minutes > 0:
            scheduler.add_job(fn, "interval", minutes=minutes, id=job_id,
                              misfire_grace_time=grace, coalesce=True, max_instances=1,
                              replace_existing=True)
            logger.info("注册任务 %s:每 %d 分钟", job_id, minutes)
    return scheduler


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not settings.SCHED_ENABLED:
        logger.error("调度未启用:设 SCHED_ENABLED=true 再启动(见 tools/config/settings.py)")
        return 1
    from apscheduler.schedulers.blocking import BlockingScheduler
    sched = build_scheduler(BlockingScheduler())
    jobs = sched.get_jobs()
    if not jobs:
        logger.error("无已启用任务:检查 SCHED_*_INTERVAL_MIN 是否有 >0 的项")
        return 1
    logger.info("调度启动:后端=%s,任务=%s", settings.STORE_BACKEND,
                [(j.id, str(j.trigger)) for j in jobs])
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度退出")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
