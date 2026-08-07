"""编排入口:采集 → 情绪 → 组装 → 视图(按日期存储,开发期默认 10 只)。

用法:
    python -m tools.run collect      # 采集数值面(K线/基本面/公告/资金流)
    python -m tools.run message      # 采集消息面(新闻/舆情/政策)
    python -m tools.run sentiment    # LLM 三层情绪打分(需 LLM 配置)
    python -m tools.run serialize    # 组装中心记录 + K线图表视图(读情绪并入决策)
    python -m tools.run panel        # 横向总表视图
    python -m tools.run screen       # 组合聚合 + 预设选股视图
    python -m tools.run all          # 全链路(采集→情绪→组装→视图),一个日期
    # 追加 --all 用全池 32 只;默认开发子集 10 只(config/dev_sample.json)

按日期:编排开始 store.set_active_date(今天),本次所有产出落 data/<日期>/。
"""
import logging
import sys

import pandas as pd

from tools.analysis import technical as ta
from tools.collectors import announcement as an
from tools.collectors import fundamental as fd
from tools.collectors import fundflow as ff
from tools.collectors import market
from tools.collectors import news, policy, ugc
from tools.config import stock_pool
from tools.store import repo as store

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
logger = logging.getLogger("run")

DEV_N = 10   # 开发期默认样本数


def _pool(argv: list[str] | None = None) -> list[str]:
    """票池:默认开发子集 10 只;argv 含 --all 用全池 32 只。"""
    if argv and "--all" in argv:
        logger.info("票池:全池 %d 只", len(stock_pool.get_codes()))
        return stock_pool.get_codes()
    codes = stock_pool.get_dev_codes(DEV_N)
    logger.info("票池:开发子集 %d 只 %s", len(codes), codes)
    return codes


def _as_of() -> str:
    return pd.Timestamp.today().strftime("%Y-%m-%d")


# ————————————————————————————————————————————————
# 采集:数值面 + 消息面
# ————————————————————————————————————————————————
def collect_values(codes: list[str]) -> None:
    logger.info("采集数值面 %d 只(K线/基本面/公告/资金流)...", len(codes))
    logger.info("K线:成功 %d", len(market.fetch_kline(codes)))
    logger.info("基本面:成功 %d", len(fd.fetch_fundamental(codes)))
    logger.info("公告:成功 %d", len(an.fetch_announcements(codes)))
    logger.info("资金流:成功 %d", len(ff.fetch_fundflow(codes)))


def collect_message(codes: list[str]) -> None:
    logger.info("采集消息面 %d 只(新闻/舆情/政策)...", len(codes))
    logger.info("新闻:成功 %d", len(news.fetch_news(codes)))
    logger.info("舆情(股吧):成功 %d", len(ugc.fetch_ugc(codes)))
    pol = policy.fetch_policy()          # 政策按行业关键词(全池共用)
    logger.info("政策:%d 条", len(pol))


# ————————————————————————————————————————————————
# 情绪:LLM 三层打分(政策全局 + 各票新闻/舆情)
# ————————————————————————————————————————————————
def run_sentiment(codes: list[str]) -> int:
    from tools.analysis import event
    from tools.llm import client as lc
    if not lc.is_configured():
        logger.warning("LLM 未配置,跳过情绪打分(记录 sentiment 将为空)")
        return 0
    logger.info("LLM 政策打分...")
    if not event.score_policy():
        logger.warning("政策打分为空(缺政策缓存?),政策层降级")
    ok = 0
    for code in codes:
        try:
            rec = event.analyze_stock(code)
        except FileNotFoundError:
            continue
        ok += 1
        s = rec["sentiment"]
        logger.info("  %s 净情绪 %s(新闻%d/舆情%d/政策%d)", code, s["净情绪分"],
                    s["三层"]["新闻"]["样本数"], s["三层"]["舆情"].get("样本数", 0),
                    s["三层"]["政策"]["样本数"])
    logger.info("情绪打分完成:%d 只", ok)
    # 同阶段生产「新闻+AI」统一视图(复用本阶段已建的 LLM 抽取缓存,不额外烧钱)
    from tools.analysis import news_ai
    logger.info("新闻 AI 视图:%d 只", news_ai.write_news_ai(codes))
    return ok


# ————————————————————————————————————————————————
# 组装 + 视图
# ————————————————————————————————————————————————
def run_serialize(codes: list[str], as_of: str) -> None:
    from tools.analysis import chart, serialize
    out = serialize.serialize_all(as_of=as_of, codes=codes)
    logger.info("中心记录:%d 只 → data/analysis/%s/", len(out), as_of)
    n = chart.write_charts(codes=codes)
    logger.info("K线图表视图:%d 只", n)


def run_panel(codes: list[str]) -> None:
    from tools.analysis import panel
    r = panel.write_panel(codes=codes)
    logger.info("横向总表 → %s", r["view"])


def run_screen(codes: list[str]) -> None:
    from tools.analysis import portfolio, serialize
    from tools.screener import screen as sc
    recs = {}
    for code in codes:
        try:
            recs[code] = serialize.load_record(code)
        except FileNotFoundError:
            pass
    agg = portfolio.aggregate(recs)
    presets = sc.run_presets(recs)
    p = store.put_view("screen", {"aggregate": agg, "presets": presets})
    logger.info("选股视图 → %s(主线 %s,预设 %d 组)", p, agg.get("hot_theme"), len(presets))


# ————————————————————————————————————————————————
# CLI 命令(单步:各自设当天日期 + 开发池)
# ————————————————————————————————————————————————
def _prep(argv):
    as_of = _as_of()
    store.set_active_date(as_of)
    return _pool(argv), as_of


def cmd_collect(argv): collect_values(_prep(argv)[0])
def cmd_message(argv): collect_message(_prep(argv)[0])
def cmd_sentiment(argv): run_sentiment(_prep(argv)[0])
def cmd_serialize(argv): codes, as_of = _prep(argv); run_serialize(codes, as_of)
def cmd_panel(argv): run_panel(_prep(argv)[0])
def cmd_screen(argv): run_screen(_prep(argv)[0])


def cmd_analyze(argv):
    """读缓存算技术指标,打印评级排行(不落盘)。"""
    codes, _ = _prep(argv)
    ranked = []
    for code in codes:
        try:
            r = ta.compute(market.load_kline(code))
            if "signal" in r:
                ranked.append((code, r["signal"]))
        except FileNotFoundError:
            logger.warning("%s 无 K线缓存,跳过", code)
    ranked.sort(key=lambda kv: kv[1]["得分"], reverse=True)
    for code, s in ranked:
        logger.info("  %s %s 评级=%s(%d)", code, stock_pool.get(code).name, s["评级"], s["得分"])


def cmd_all(argv):
    """全链路:采集(数值+消息)→ 情绪 → 组装 → 视图,全程同一日期。"""
    codes, as_of = _prep(argv)
    logger.info("===== 全链路开始(日期 %s,%d 只)=====", as_of, len(codes))
    collect_values(codes)
    collect_message(codes)
    run_sentiment(codes)                 # LLM 未配置则内部跳过
    run_serialize(codes, as_of)          # serialize 读情绪并入买卖倾向
    run_panel(codes)
    run_screen(codes)
    logger.info("===== 全链路完成 → data/analysis/%s/ =====", as_of)


_CMDS = {"collect": cmd_collect, "message": cmd_message, "sentiment": cmd_sentiment,
         "serialize": cmd_serialize, "panel": cmd_panel, "screen": cmd_screen,
         "analyze": cmd_analyze, "all": cmd_all}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _CMDS:
        print(f"用法: python -m tools.run [{'|'.join(_CMDS)}] [--all]")
        return 1
    _CMDS[argv[1]](argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
