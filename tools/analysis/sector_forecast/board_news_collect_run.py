"""S3 · 定向采集 每日 runner——读 S2 全板块角色表 → 逐板块采集 → raw 过程文件。**无 LLM**。

设计:docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md §1 S3。
采集与研判解耦:本 runner 只抓取落 data/sector_news/raw/<date>/<板块>.json,研判交 S4。
板块清单默认读 S1 sector_universe.json;缺则回退全申万一级/种子板块。

用法:python -m tools.analysis.sector_forecast.board_news_collect_run [--date YYYY-MM-DD] [--boards 电子,计算机]
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path


def _setup_logging() -> None:
    from tools.config import settings
    logdir = settings.PROJECT_ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(logdir / "board_news_collect.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [fh, logging.StreamHandler()]


def _boards(date: str) -> list[str]:
    """板块清单:优先 S1 sector_universe.json,回退种子板块。"""
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    for base in (settings.PROJECT_ROOT, _MAIN):
        p = Path(base) / "data" / "analysis" / "sector_universe.json"
        if p.exists():
            try:
                uni = json.loads(p.read_text(encoding="utf-8"))
                bs = [x["板块"] for x in uni.get("板块清单", [])]
                if bs:
                    return bs
            except Exception:
                pass
    from tools.analysis.sector_forecast.roles import SEED_SW
    return SEED_SW


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S3 定向采集每日 runner(无 LLM)")
    ap.add_argument("--date", default=None)
    ap.add_argument("--boards", default=None, help="逗号分隔申万一级;缺省=S1清单/种子")
    ap.add_argument("--lookback-days", type=int, default=None,
                    help="新闻下限窗口天数;缺省=模块默认(live 可放宽取更多新闻)")
    ap.add_argument("--strict", action="store_true",
                    help="严格 as-of:剔除 t>date 未来新闻(回测/forward 复盘用);"
                         "默认 live 口径=最大可得新闻(allow_future=True)")
    args = ap.parse_args(argv)
    _setup_logging()
    log = logging.getLogger("sector_forecast.board_news_collect_run")
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    try:
        from tools.collectors import calendar as cal
        if not cal.is_trading_day(date):
            log.info("跳过:%s 非交易日", date)
            return 0
    except Exception:
        pass

    from tools.analysis.sector_forecast import board_news_collect as BC
    boards = [b.strip() for b in args.boards.split(",")] if args.boards else _boards(date)
    allow_future = not args.strict          # live 每日 runner 默认放开(最大可得新闻);--strict 回退防未来
    kw = {"allow_future": allow_future}
    if args.lookback_days is not None:
        kw["lookback_days"] = args.lookback_days
    log.info("S3 新闻口径:%s(allow_future=%s)",
             "最大可得·live" if allow_future else "严格 as-of·防未来", allow_future)
    paths = BC.collect_all(date, boards, **kw)
    if not paths:
        log.error("S3 未采集到任何板块(先跑 S2 角色关系表 / 每日 roster),非0退出")
        return 1
    log.info("S3 完成:%d 板块 raw → data/sector_news/raw/%s/", len(paths), date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
