"""编排入口:采集 → 分析 → 报告(P1 技术面闭环)。

用法:
    python -m tools.run collect     # 拉全池 K线到缓存
    python -m tools.run analyze     # 读缓存算技术指标,打印排行
    python -m tools.run report      # 出组合概览 + Top/Bottom 各5 单票卡
    python -m tools.run all         # 采集 → 报告
"""
import logging
import sys

from tools.analysis import technical as ta
from tools.collectors import announcement as an
from tools.collectors import fundamental as fd
from tools.collectors import fundflow as ff
from tools.collectors import market
from tools.config import stock_pool
from tools.report import builder

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
logger = logging.getLogger("run")

TOPN = 5   # 单票报告出技术评级 Top/Bottom 各 N(用户拍板)


def cmd_collect() -> None:
    codes = stock_pool.get_codes()
    logger.info("采集全池 %d 只 K线...", len(codes))
    kl = market.fetch_kline(codes)
    logger.info("K线采集:成功 %d / %d", len(kl), len(codes))
    logger.info("采集全池基本面...")
    fund = fd.fetch_fundamental(codes)
    logger.info("基本面采集:成功 %d / %d", len(fund), len(codes))
    logger.info("采集全池公告...")
    ann = an.fetch_announcements(codes)
    logger.info("公告采集:成功 %d / %d", len(ann), len(codes))
    logger.info("采集全池资金流...")
    fflow = ff.fetch_fundflow(codes)
    logger.info("资金流采集:成功 %d / %d", len(fflow), len(codes))


def _load_fundamentals() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for code in stock_pool.get_codes():
        try:
            out[code] = fd.load_fundamental(code)
        except FileNotFoundError:
            pass
    return out


def _load_announcements() -> dict[str, list]:
    out: dict[str, list] = {}
    for code in stock_pool.get_codes():
        try:
            out[code] = an.load_announcements(code)
        except FileNotFoundError:
            pass
    return out


def _load_fundflows() -> dict[str, dict]:
    """读资金流缓存并派生摘要,供聚合/报告用。"""
    out: dict[str, dict] = {}
    for code in stock_pool.get_codes():
        try:
            out[code] = ff.summarize(ff.load_fundflow(code))
        except FileNotFoundError:
            pass
    return out


def _analyze_all() -> dict[str, dict]:
    """读缓存算全池技术指标(不触网)。缓存缺失的票跳过。"""
    results: dict[str, dict] = {}
    for code in stock_pool.get_codes():
        try:
            results[code] = ta.compute(market.load_kline(code))
        except FileNotFoundError:
            logger.warning("%s 无缓存,跳过(先 collect)", code)
    return results


def cmd_analyze() -> dict[str, dict]:
    results = _analyze_all()
    ranked = sorted((r for r in results.items() if "signal" in r[1]),
                    key=lambda kv: kv[1]["signal"]["得分"], reverse=True)
    logger.info("技术评级排行:")
    for code, r in ranked:
        s = r["signal"]
        logger.info("  %s %s 评级=%s(%d)", code, stock_pool.get(code).name,
                    s["评级"], s["得分"])
    return results


def cmd_report() -> None:
    results = _analyze_all()
    funds = _load_fundamentals()
    anns = _load_announcements()
    p = builder.build_portfolio_tech_report(results, funds, anns)
    logger.info("组合技术概览 → %s", p)
    valid = [(c, r) for c, r in results.items() if "signal" in r]
    valid.sort(key=lambda kv: kv[1]["signal"]["得分"], reverse=True)
    focus = valid[:TOPN] + valid[-TOPN:]           # Top/Bottom 各 N
    for code, r in focus:
        sp = builder.build_stock_tech_report(code, r, funds.get(code), anns.get(code))
        logger.info("单票卡 %s → %s", code, sp)


def cmd_serialize() -> None:
    """组装每票结构化 JSON + K线图表视图到 data/analysis/(程序/DB/Web 可消费)。"""
    from tools.analysis import chart, serialize
    out = serialize.serialize_all()
    logger.info("结构化 JSON 完成:%d 只 → data/analysis/", len(out))
    n = chart.write_charts()                    # K线图表视图(供 web 只读,§9.3)
    logger.info("K线图表视图完成:%d 只 → data/analysis/chart/", n)


def cmd_panel() -> None:
    """拍平全池结构化 JSON 成横向总表(CSV/JSON/markdown)。"""
    from tools.analysis import panel
    out = panel.write_panel()
    logger.info("横向总表 → %s", out["csv"])


def cmd_screen() -> None:
    """组合聚合 + 预设选股,落 data/analysis/screen.json(供 Web 选股页)。"""
    import json
    from tools.analysis import portfolio, serialize
    from tools.screener import screen as sc
    recs = {}
    for code in stock_pool.get_codes():
        try:
            recs[code] = serialize.load_record(code)
        except FileNotFoundError:
            pass
    agg = portfolio.aggregate(recs)
    presets = sc.run_presets(recs)
    out = serialize._OUT_DIR / "screen.json"
    out.write_text(json.dumps({"aggregate": agg, "presets": presets},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("选股结果 → %s(主线 %s,预设 %d 组)", out, agg.get("hot_theme"), len(presets))


def cmd_sentiment() -> None:
    """情绪面(LLM):采集新闻 + 逐票抽取/归类/聚合。opt-in(有 LLM 成本,不进 all)。"""
    from tools.analysis import event
    from tools.collectors import news
    from tools.llm import client as lc
    if not lc.is_configured():
        logger.error("LLM 未配置(需环境变量 LLM_BASE_URL+LLM_API_KEY),跳过")
        return
    codes = stock_pool.get_codes()
    logger.info("采集全池新闻...")
    news.fetch_news(codes)
    logger.info("LLM 情绪分析(每票新闻抽取,缓存命中免重复)...")
    ok = 0
    for code in codes:
        try:
            rec = event.analyze_stock(code)
            ok += 1
            logger.info("  %s 净情绪 %s(样本 %d)", code,
                        rec["sentiment"]["净情绪分"], rec["sentiment"]["样本数"])
        except FileNotFoundError:
            pass
    logger.info("情绪分析完成:%d 只 → data/analysis/sentiment/", ok)


def cmd_all() -> None:
    cmd_collect()
    cmd_serialize()
    cmd_panel()
    cmd_screen()
    cmd_report()


_CMDS = {"collect": cmd_collect, "analyze": cmd_analyze,
         "report": cmd_report, "serialize": cmd_serialize,
         "panel": cmd_panel, "screen": cmd_screen,
         "sentiment": cmd_sentiment, "all": cmd_all}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _CMDS:
        print(f"用法: python -m tools.run [{'|'.join(_CMDS)}]")
        return 1
    _CMDS[argv[1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
