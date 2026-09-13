"""统一定时任务框架 CLI。

    python -m tools.scheduling validate            # 校验注册表(fail-loud),打印摘要
    python -m tools.scheduling list                # 列出任务(id/cron/enabled/cmd)
    python -m tools.scheduling next [--count N]     # 打印各任务未来 N 次触发时刻
    python -m tools.scheduling run                  # 启动常驻调度(默认 dry-run 影子!)
    python -m tools.scheduling run --live           # 真执行命令(慎用,须显式)

安全默认:run 不带 --live 即 **dry-run 影子模式**——到点只打印"本该跑什么",
绝不起子进程、绝不碰 live launchd。这是"只建不切"的护栏。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys

from tools.scheduling.notifier import build_notifier
from tools.scheduling.registry import RegistryError, load_registry
from tools.scheduling.runtime import build_scheduler


def _load(path: str | None):
    try:
        return load_registry(path)
    except RegistryError as e:
        print(f"注册表校验失败:{e}", file=sys.stderr)
        sys.exit(2)


def cmd_validate(args) -> int:
    reg = _load(args.registry)
    print(f"OK  注册表 {reg.path}")
    print(f"    共 {len(reg.tasks)} 条,启用 {len(reg.enabled())} 条")
    for t in reg.tasks:
        flag = "on " if t.enabled else "off"
        print(f"    [{flag}] {t.id:16s} {t.cron:18s} {' '.join(t.cmd)}")
    return 0


def cmd_list(args) -> int:
    reg = _load(args.registry)
    for t in reg.tasks:
        flag = "enabled" if t.enabled else "disabled"
        print(f"{t.id:16s} {flag:8s} cron={t.cron!r:22s} "
              f"timeout={t.timeout_sec} retries={t.retries} notify_on={t.notify_on}")
        print(f"    cmd: {' '.join(t.cmd)}  # {t.description}")
    return 0


def cmd_next(args) -> int:
    reg = _load(args.registry)
    now = _dt.datetime.now().astimezone()
    for t in reg.enabled():
        trig = t.build_trigger()
        fires, prev = [], None
        for _ in range(args.count):
            nxt = trig.get_next_fire_time(prev, now if prev is None else prev)
            if nxt is None:
                break
            fires.append(nxt.strftime("%Y-%m-%d %H:%M %a"))
            prev = nxt
        print(f"{t.id:16s} {t.cron:18s} -> {', '.join(fires)}")
    return 0


def cmd_run(args) -> int:
    reg = _load(args.registry)
    dry = not args.live
    notifier = build_notifier(args.channel)
    from apscheduler.schedulers.blocking import BlockingScheduler
    sched = build_scheduler(reg, notifier, dry_run=dry, scheduler=BlockingScheduler(),
                            misfire_grace=args.misfire_grace,
                            heartbeat_min=args.heartbeat_min, reload_min=args.reload_min)
    mode = "真执行(--live)" if not dry else "dry-run 影子(不真跑命令、不碰 live launchd)"
    logging.getLogger("scheduling").warning("调度启动:模式=%s 任务=%s", mode,
                                            [j.id for j in sched.get_jobs()])
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger("scheduling").info("调度退出")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tools.scheduling",
                                description="统一定时任务框架(架构⑥ P1-P2)")
    p.add_argument("--registry", default=None, help="注册表路径(缺省 tools/scheduling/tasks.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="校验注册表并打印摘要").set_defaults(func=cmd_validate)
    sub.add_parser("list", help="列出所有任务").set_defaults(func=cmd_list)
    pn = sub.add_parser("next", help="打印各任务未来触发时刻")
    pn.add_argument("--count", type=int, default=3)
    pn.set_defaults(func=cmd_next)
    pr = sub.add_parser("run", help="启动常驻调度(默认 dry-run 影子)")
    pr.add_argument("--live", action="store_true",
                    help="真执行命令(默认不带=影子;慎用)")
    pr.add_argument("--channel", default=None, help="告警渠道(覆盖 env STOCK_ALERT_CHANNEL)")
    pr.add_argument("--misfire-grace", type=int, default=3600)
    pr.add_argument("--heartbeat-min", type=int, default=0, help=">0 开心跳(分钟)")
    pr.add_argument("--reload-min", type=int, default=0, help=">0 开注册表热重载轮询(分钟)")
    pr.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
