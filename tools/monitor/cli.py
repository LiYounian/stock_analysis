"""盯盘 CLI:load-picks / add / add-pool / rm / list / run。

用法:
    python -m tools.monitor.cli load-picks [<md路径>]      # 选股md → 当日watchlist(缺省取最新)
    python -m tools.monitor.cli add 002811 --entry 51 --stop 49 --take 55
    python -m tools.monitor.cli add-pool                   # 自选池A股并入
    python -m tools.monitor.cli rm 002811
    python -m tools.monitor.cli list
    python -m tools.monitor.cli run --interval 4           # 启动盯盘(桌面小卡片)
⚠️ 研究/模拟,非投资建议;只拉行情、只弹提示,不下单。
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime

from tools.monitor import converter, engine
from tools.monitor.notify import get_notifier
from tools.monitor.schema import CST, Trigger, WatchItem, Watchlist

logger = logging.getLogger("monitor.cli")


def _today() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d")


def _load_or_new(date: str) -> Watchlist:
    return Watchlist.load(date) or Watchlist(date=date, source={"type": "manual", "as_of": date})


def cmd_load_picks(args) -> None:
    wl = converter.from_selection_md(args.path)
    existing = Watchlist.load(wl.date)
    if existing and args.merge:
        wl = converter.merge(existing, wl)
    p = wl.save()
    print(f"已生成 watchlist:{p}  ({len(wl.items)} 只,来源 {wl.source.get('type')})")


def cmd_add(args) -> None:
    date = _today()
    wl = _load_or_new(date)
    ts: list[Trigger] = []
    if args.entry is not None:
        ts.append(Trigger(id="entry", kind="entry_limit", op="<=", value=args.entry, action="可挂单进场(不追高)"))
    if args.stop is not None:
        ts.append(Trigger(id="stop", kind="stop_loss", op="<=", value=args.stop, action="止损/放弃进场"))
    if args.take is not None:
        ts.append(Trigger(id="take", kind="take_profit", op=">=", value=args.take, action="止盈了结"))
    item = WatchItem(code=args.code, name=args.name or args.code, role="自选", triggers=ts)
    wl.items = [it for it in wl.items if it.code != args.code] + [item]
    wl.save()
    print(f"已加入 {args.code}（{len(ts)} 条触发），当前 {len(wl.items)} 只")


def cmd_add_pool(args) -> None:
    date = _today()
    wl = converter.merge(_load_or_new(date), converter.from_watchpool(date=date))
    wl.save()
    print(f"已并入自选池A股,当前 {len(wl.items)} 只")


def cmd_rm(args) -> None:
    date = _today()
    wl = _load_or_new(date)
    n0 = len(wl.items)
    wl.items = [it for it in wl.items if it.code != args.code]
    wl.save()
    print(f"已移除 {args.code}（{n0}→{len(wl.items)}）")


def cmd_list(args) -> None:
    wl = Watchlist.load(_today())
    if not wl or not wl.items:
        print("当前无 watchlist(先 load-picks 或 add)")
        return
    print(f"# {wl.date} 盯盘清单({len(wl.items)} 只,来源 {wl.source.get('type')})")
    for it in wl.items:
        conds = "; ".join(f"{c.id}{c.op}{c.value}" if c.kind != "time" else f"{c.id}@{c.at}"
                          for c in it.all_conditions())
        print(f"  {it.code} {it.name} [{it.role}] {conds or '(仅监测)'}")


def cmd_run(args) -> None:
    wl = Watchlist.load(_today())
    if not wl or not wl.items:
        print("当前无 watchlist,先 load-picks / add")
        return
    engine.run(wl, get_notifier(args.notifier), interval=args.interval,
               respect_session=not args.no_session)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tools.monitor.cli", description="定向盯盘与监控(MVP)")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("load-picks", help="选股md → 当日watchlist")
    lp.add_argument("path", nargs="?", default=None, help="md路径(缺省取最新)")
    lp.add_argument("--merge", action="store_true", help="与已有清单合并而非覆盖")
    lp.set_defaults(func=cmd_load_picks)

    ad = sub.add_parser("add", help="手动加一票")
    ad.add_argument("code")
    ad.add_argument("--name", default=None)
    ad.add_argument("--entry", type=float, default=None)
    ad.add_argument("--stop", type=float, default=None)
    ad.add_argument("--take", type=float, default=None)
    ad.set_defaults(func=cmd_add)

    ap = sub.add_parser("add-pool", help="自选池A股并入")
    ap.set_defaults(func=cmd_add_pool)

    rm = sub.add_parser("rm", help="移除一票")
    rm.add_argument("code")
    rm.set_defaults(func=cmd_rm)

    ls = sub.add_parser("list", help="查看当前清单")
    ls.set_defaults(func=cmd_list)

    rn = sub.add_parser("run", help="启动盯盘")
    rn.add_argument("--interval", type=float, default=4.0)
    rn.add_argument("--notifier", default="desktop")
    rn.add_argument("--no-session", action="store_true", help="忽略交易时段闸门(联调用)")
    rn.set_defaults(func=cmd_run)
    return p


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
