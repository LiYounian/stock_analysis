"""消息驱动 · 每日增量 runner(一入 → 落盘 → 扩 sector_focus)。

用户口径(2026-09-16):**下午不冲突时段、每天增量**跑——定向抓各板块龙头+板块近1-2周新闻,
LLM 按 rubric **文字分级**研判(不打分),去重增量(旧新闻不重复分析)。

产出:
  data/sector_news/catalyst_<date>.json   各板块消息研判(消息面/强弱/关键事件[分级]/持续性/时效/可靠性)
  并把「消息驱动」块扩进当日 sector_focus.json(若已存在;供选股"利好板块→筛形态好的+反选剔除→深度分析")

用法:python -m tools.analysis.sector_forecast.news_catalyst_run [--date YYYY-MM-DD] [--boards 电子,计算机]
真 LLM 走网关(zsh -ic env);⚠️ 测试环境研究模拟,非投资建议。
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
    fh = logging.FileHandler(logdir / "sector_news_catalyst.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers = [fh, logging.StreamHandler()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="消息驱动板块研判每日增量 runner")
    ap.add_argument("--date", default=None)
    ap.add_argument("--boards", default=None, help="逗号分隔申万一级;缺省=种子板块")
    args = ap.parse_args(argv)
    _setup_logging()
    log = logging.getLogger("sector_forecast.news_catalyst_run")
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    try:
        from tools.collectors import calendar as cal
        if not cal.is_trading_day(date):
            log.info("跳过:%s 非交易日", date)
            return 0
    except Exception:
        pass

    from tools.config import settings
    from tools.analysis.sector_forecast import news_catalyst as NC
    from tools.analysis.sector_forecast import news_focus_block as NB

    boards = [b.strip() for b in args.boards.split(",")] if args.boards else None
    cat = NC.board_leader_catalyst(date, boards=boards)     # 一入:定向采集+rubric文字研判(去重增量)
    利好 = [sw for sw, c in cat.items() if c.get("消息标签") == "利好"]

    root = settings.PROJECT_ROOT / "data" / "sector_news"
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"catalyst_{date}.json"
    payload = {"date": date, "version": NC.CATALYST_VERSION, "rubric": NC.RUBRIC,
               "板块研判": cat, "利好板块": 利好,
               "口径": "定向龙头+板块近1-2周新闻→LLM按rubric文字分级(不打分)+去重增量;下午每天增量",
               "免责": "测试环境研究模拟,非投资建议。"}
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    log.info("板块消息研判 → %s(利好板块 %d:%s)", out, len(利好), "、".join(利好))

    # 一出(M2):扩进当日 sector_focus.json 的「消息驱动」块(若已落盘)
    try:
        p = NB.enrich_sector_focus(date, boards=boards, cat=cat)   # 复用已算 catalyst,不二次烧 LLM
        if p:
            log.info("sector_focus 扩「消息驱动」块 → %s", p)
    except Exception as e:
        log.warning("扩 sector_focus 失败(不影响 catalyst 落盘):%s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
