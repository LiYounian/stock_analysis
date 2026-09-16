"""S4 · 结构化研判 每日 runner——读 S3 raw → DeepSeek 并行研判 → catalyst_<date>.json + 扩 sector_focus。

设计:docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md §1 S4。
模型:DeepSeek-v4-pro(get_client() 默认·thinking 关·统筹 A/B 择优)。真调 LLM 走网关(zsh -ic env)。
前置:当日 S3 raw 已落(data/sector_news/raw/<date>/);缺则本 runner 无板块可判、非0退出。

用法:python -m tools.analysis.sector_forecast.board_verdict_run [--date YYYY-MM-DD] [--workers N] [--boards 电子,计算机]
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime


def _setup_logging() -> None:
    from tools.config import settings
    logdir = settings.PROJECT_ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(logdir / "board_verdict_s4.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [fh, logging.StreamHandler()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S4 结构化研判每日 runner(DeepSeek 并行)")
    ap.add_argument("--date", default=None)
    ap.add_argument("--boards", default=None, help="逗号分隔;缺省=当日 S3 raw 全部板块")
    ap.add_argument("--workers", type=int, default=None, help="并发数;缺省=SECTOR_S4_WORKERS/4")
    args = ap.parse_args(argv)
    _setup_logging()
    log = logging.getLogger("sector_forecast.board_verdict_run")
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    try:
        from tools.collectors import calendar as cal
        if not cal.is_trading_day(date):
            log.info("跳过:%s 非交易日", date)
            return 0
    except Exception:
        pass

    from tools.analysis.sector_forecast import board_verdict_s4 as S4
    boards = [b.strip() for b in args.boards.split(",")] if args.boards else None
    kw = {"boards": boards}
    if args.workers is not None:
        kw["workers"] = args.workers
    cat = S4.board_catalyst_from_raw(date, **kw)
    if not cat:
        log.error("S4 无研判产出(先跑 S3 采集落 raw),非0退出:%s", date)
        return 1
    out = S4.write_catalyst(date, cat)
    log.info("S4 catalyst 落盘 → %s", out)

    # 一出(M2):扩「消息驱动」块进当日 sector_focus.json(复用已算 cat,不重烧 LLM)
    try:
        from tools.analysis.sector_forecast import news_focus_block as NB
        p = NB.enrich_sector_focus(date, cat=cat)
        if p:
            log.info("sector_focus 扩「消息驱动」块 → %s", p)
    except Exception as e:
        log.warning("扩 sector_focus 失败(不影响 catalyst 落盘):%s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
