"""S2 · 角色关系表 周度 runner——读 S1 全板块 → 建五角色关系表 + 主力(LHB) + 变更留痕。

设计:docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md §1 S2。
每周一次:data/sector_roster_table/<板块>.json(龙头/中军/补涨先锋/弹性/主力 + 变更历史)
+ changes/roster_changes_<ISO周>.md。保留每日 roles 版(data/sector_roster/)作对照,本表不动它。

用法:python -m tools.analysis.sector_forecast.sector_roster_table_run [--date YYYY-MM-DD] [--boards 电子,计算机]
建议先跑 S1(sector_universe.json)再跑本 runner(读 S1 板块清单);缺 S1 → 回退全申万一级。
非交易日回退 ≤date 最近交易日。⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta


def _setup_logging() -> None:
    from tools.config import settings
    logdir = settings.PROJECT_ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(logdir / "sector_roster_table.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [fh, logging.StreamHandler()]


def _resolve_trading_day(date: str) -> str:
    try:
        from tools.collectors import calendar as cal
        d = datetime.strptime(date, "%Y-%m-%d")
        for _ in range(10):
            s = d.strftime("%Y-%m-%d")
            if cal.is_trading_day(s):
                return s
            d -= timedelta(days=1)
    except Exception:
        pass
    return date


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S2 角色关系表 周度维护 runner")
    ap.add_argument("--date", default=None, help="口径日(默认今天;非交易日回退最近交易日)")
    ap.add_argument("--boards", default=None, help="逗号分隔申万一级;缺省=读S1清单/全申万一级")
    args = ap.parse_args(argv)
    _setup_logging()
    log = logging.getLogger("sector_forecast.sector_roster_table_run")

    date = _resolve_trading_day(args.date or datetime.now().strftime("%Y-%m-%d"))
    boards = [b.strip() for b in args.boards.split(",")] if args.boards else None
    from tools.analysis.sector_forecast import sector_roster_table as ST
    paths, md = ST.build_all_roster_tables(date, boards=boards)
    if not paths:
        log.error("无板块建表(成分空?),非0退出:%s", date)
        return 1
    log.info("S2 完成:%d 板块关系表 + 变更汇总 %s", len(paths), md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
