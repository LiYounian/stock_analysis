"""编排入口:采集 → 情绪 → 组装 → 视图(按日期存储,开发期默认 10 只)。

用法:
    python -m tools.run collect      # 采集数值面(K线/基本面/公告/资金流)
    python -m tools.run message      # 采集消息面(新闻/舆情/政策)
    python -m tools.run context      # 全市场指数(沪深300+申万一级行业)→ 供板块轮动 RRG 专家
    python -m tools.run lhb           # 收盘后当日龙虎榜(风控微结构轴;T 落盘、T+1 生效)→ 进次日风控 veto 汇聚
    python -m tools.run sentiment    # LLM 三层情绪打分(需 LLM 配置)
    python -m tools.run serialize    # 组装中心记录 + K线图表视图(读情绪并入决策)
    python -m tools.run events       # 采集事件精数值(业绩预告/快报+增减持),供事件驱动专家
    python -m tools.run factor       # 多因子截面打分预算(全池横截面)→ code_view,供多因子专家
    python -m tools.run council      # 横截面/事件就绪后回写各 record 的 council 块
    python -m tools.run panel        # 横向总表视图
    python -m tools.run screen       # 组合聚合 + 预设选股视图
    python -m tools.run pattern      # 形态选股(模块二)扫描:RS+硬规则AND+达标占比
    python -m tools.run sepa         # SEPA+VCP 监控(午间/收盘):均线入池 + 波段收缩两表
    python -m tools.run all          # 全链路(采集[含逐笔盘口]→情绪→组装→事件→多因子→合议回写→视图),一个日期
    python -m tools.run ticks        # 单独跑逐笔盘口归档(run all 已含;此命令供 --all 全池/--date 回补)
    python -m tools.run pipeline     # 全A 两阶段流水线:全A便宜筛得达标池,再只对(达标∪自选)做新闻/LLM/合议
    python -m tools.run screenall    # 全A 多策略选股(策略0/2/4… S01/箱体3 已下线)→ 只对(各策略选出并集∪自选)做新闻/LLM/合议
    # 追加 --all 用全池 32 只;默认开发子集 10 只(config/dev_sample.json)
    # pattern 额外支持 --universe [N]:从全 A 票池取前 N 只(默认 50)扫描
    # sepa 额外支持 --universe [N] --session 午间|收盘 --no-fetch:SEPA+VCP 监控扫描
    # pipeline 额外支持 --universe [N]:阶段①只扫全A前 N 只(默认全量);贵活只对候选(达标∪自选)
    # screenall 额外支持 --universe [N]:全A前 N 只做筛选(默认全量);--no-llm 纯数据快跑(跳过新闻+LLM)

按日期:编排开始 store.set_active_date(今天),本次所有产出落 data/<日期>/。
"""
import logging
import os
import socket
import sys

import pandas as pd

from tools.analysis import technical as ta
from tools.collectors import announcement as an
from tools.collectors import baidu_news
from tools.collectors import chip, consensus
from tools.collectors import fundamental as fd
from tools.collectors import fundflow as ff
from tools.collectors import industry_history as ih
from tools.collectors import lhb as lhb_collector
from tools.collectors import market
from tools.collectors import master_sync
from tools.collectors import news, policy
from tools.collectors import smart_money as sm
from tools.collectors import ugc
from tools.config import settings, stock_pool
from tools.store import repo as store

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
logger = logging.getLogger("run")

DEV_N = 10   # 开发期默认样本数
# 单次网络请求上限(秒):被墙机上让东财等源"秒级失败降级",而非分钟级挂在超时/重试。
# 采集阶段临时设为进程级 socket 默认超时(约束 akshare/requests 这类无 timeout 参数的调用),
# 采集结束即还原,不影响后续 LLM 长调用。curl_cffi 走 libcurl、不认 socket 超时,单独传参(见各采集器)。
FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "10"))


def _safe(label: str, fn):
    """跑单个数据源采集;失败/异常 → WARNING + 返回 None + 继续(绝不让整条流水线中止)。"""
    try:
        return fn()
    except Exception as e:
        logger.warning("%s 采集失败,降级跳过(不中止流水线): %s", label, e)
        return None


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
    """采集数值面:每个源各自 try/except 降级,任一源失败都不中止整批。

    K线走「滚动主档」编排(master_sync:主档缺失/太旧→baostock 全量;否则 spot 当日增量;
    失败回退逐只 akshare)。此步**不套** FETCH_TIMEOUT 短超时——spot 单请求返回全A较大、
    baostock 为稳定数据 API,短超时会误伤;其内部 fallback 逐只路径自带短超时快速失败。
    基本面/公告/资金流仍走 FETCH_TIMEOUT 短超时快速失败降级。
    """
    logger.info("采集数值面 %d 只(K线/基本面/公告/资金流)...", len(codes))
    r = _safe("K线主档同步", lambda: master_sync.sync_master(codes)) or {}
    logger.info("K线主档同步:模式=%s 成功 %d", r.get("mode"), r.get("ok", 0))
    _old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(FETCH_TIMEOUT)          # 采集期快速失败;finally 还原
    try:
        logger.info("基本面:成功 %d", len(_safe("基本面", lambda: fd.fetch_fundamental(codes)) or {}))
        logger.info("公告:成功 %d", len(_safe("公告", lambda: an.fetch_announcements(codes)) or {}))
        logger.info("资金流:成功 %d", len(_safe("资金流", lambda: ff.fetch_fundflow(codes)) or {}))
        # 筹码本地推演(读上一步落好的主档 K线,无网络);主力行为/一致预期走东财
        logger.info("筹码:成功 %d", len(_safe("筹码", lambda: chip.fetch_chip(codes)) or {}))
        # 主力行为:龙虎榜/大宗日级 → 每日全刷;股东户数季度级 → 新鲜度门控(缓存新鲜跳过逐票拉)
        logger.info("主力行为:成功 %d", len(_safe(
            "主力行为",
            lambda: sm.fetch_smart_money(codes, holder_max_stale_days=settings.HOLDER_STALE_DAYS)) or {}))
        # 一致预期周级变 → 新鲜度门控:只对缓存陈旧/无缓存的票逐票拉(对齐 industry 的 skip-if-cached)
        need_cs = [c for c in codes if store.is_stale("consensus", c, settings.CONSENSUS_STALE_DAYS)]
        if need_cs:
            logger.info("一致预期:%d/%d 陈旧待拉,成功 %d", len(need_cs), len(codes),
                        len(_safe("一致预期", lambda: consensus.fetch_consensus(need_cs)) or {}))
        else:
            logger.info("一致预期:全部缓存新鲜(≤%s 天),跳过", settings.CONSENSUS_STALE_DAYS)
        # 行业变迁史近乎静态(多年一变),只补尚无缓存的票,避免每日重复拉巨潮
        need_ih = [c for c in codes if not _load_ok(ih.load_industry_history, c)]
        if need_ih:
            logger.info("行业变迁:补缺 %d", len(_safe("行业变迁", lambda: ih.fetch_industry_history(need_ih)) or {}))
    finally:
        socket.setdefaulttimeout(_old)


def _load_ok(loader, code: str) -> bool:
    """本地缓存是否已有该票该源(load 成功=有;FileNotFoundError/损坏=无,交给重采覆盖)。"""
    try:
        loader(code)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def collect_values_missing(codes: list[str]) -> None:
    """数值面**只补缺失**(skip-if-cached,幂等):分别取尚无缓存的子集采 K线/基本面/公告/资金流。

    供两阶段流水线阶段②——阶段①已为全A采过 K线、为候选采过基本面/公告,这里只补自选池等
    尚未覆盖的票,不重采已采的(省网络、可反复跑)。各源独立 _safe 降级,不中止整批。
    """
    need_k = [c for c in codes if not _load_ok(market.load_kline, c)]
    need_f = [c for c in codes if not _load_ok(fd.load_fundamental, c)]
    need_a = [c for c in codes if not _load_ok(an.load_announcements, c)]
    need_ff = [c for c in codes if not _load_ok(ff.load_fundflow, c)]
    need_chip = [c for c in codes if not _load_ok(chip.load_chip, c)]
    need_sm = [c for c in codes if not _load_ok(sm.load_lhb, c)]
    need_cs = [c for c in codes if not _load_ok(consensus.load_consensus, c)]
    need_ih = [c for c in codes if not _load_ok(ih.load_industry_history, c)]
    logger.info("数值面补缺(%d 候选,已缓存跳过):K线 %d / 基本面 %d / 公告 %d / 资金流 %d / "
                "筹码 %d / 主力行为 %d / 一致预期 %d / 行业变迁 %d",
                len(codes), len(need_k), len(need_f), len(need_a), len(need_ff),
                len(need_chip), len(need_sm), len(need_cs), len(need_ih))
    _old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(FETCH_TIMEOUT)
    try:
        if need_k:
            _safe("K线", lambda: market.fetch_kline(need_k))
        if need_f:
            _safe("基本面", lambda: fd.fetch_fundamental(need_f))
        if need_a:
            _safe("公告", lambda: an.fetch_announcements(need_a))
        if need_ff:
            _safe("资金流", lambda: ff.fetch_fundflow(need_ff))
        if need_chip:
            _safe("筹码", lambda: chip.fetch_chip(need_chip))
        if need_sm:
            _safe("主力行为", lambda: sm.fetch_smart_money(need_sm))
        if need_cs:
            _safe("一致预期", lambda: consensus.fetch_consensus(need_cs))
        if need_ih:
            _safe("行业变迁", lambda: ih.fetch_industry_history(need_ih))
    finally:
        socket.setdefaulttimeout(_old)


def collect_message(codes: list[str]) -> None:
    """采集消息面:每个源各自 try/except 降级,任一源(含政策)失败都不中止整批。"""
    logger.info("采集消息面 %d 只(新闻/舆情/政策)...", len(codes))
    _old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(FETCH_TIMEOUT)
    try:
        # recall=True:开启行业主题词扩召回 + LLM 宁严相关性初筛(collect_message 只被自选池/
        # screenall/两阶段的 llm_subset 调用,天然不波及全A;补"挂不到个股的行业/宏观/管制"消息)。
        logger.info("新闻:成功 %d", len(_safe("新闻", lambda: news.fetch_news(codes, recall=True)) or {}))
        logger.info("舆情(股吧):成功 %d", len(_safe("舆情(股吧)", lambda: ugc.fetch_ugc(codes)) or {}))
        # 百度个股新闻(前向情绪滚存):仅采集落盘、不接情绪评分。与上面 news/ugc 共用同一
        # codes(news_subset=自选∪每策略前N),天然不波及全A;fetch_baidu_news 自带新鲜度门控
        # (缓存≤BAIDU_NEWS_STALE_DAYS 天跳过重拉)+ 前向增量并集幂等,同日重跑不猛拉。
        # 整块 _safe 兜底、内部单票失败已降级,任何失败都不阻断闭环。开关 BAIDU_NEWS_COLLECT。
        if settings.BAIDU_NEWS_COLLECT:
            logger.info("百度新闻(前向滚存):成功 %d",
                        len(_safe("百度新闻", lambda: baidu_news.fetch_baidu_news(codes)) or {}))
        pol = _safe("政策", lambda: policy.fetch_policy())      # 政策按行业关键词(全池共用)
        logger.info("政策:%d 条", len(pol or []))
    finally:
        socket.setdefaulttimeout(_old)


def collect_market_context() -> None:
    """采集板块轮动(RRG)所需的全市场指数——**每轮一次、非逐票**:
      - 沪深300 基准指数(index_kline/000300)
      - 全部申万一级行业指数(board_kline/<申万一级>)
    落 store(复用 collectors.index / collectors.board,只调不改)。无此数据时 RRG 专家整体弃权。
    健壮性:每步 _safe 降级(采不到→WARNING+跳过,绝不中止流水线),走统一 FETCH_TIMEOUT。
    """
    from tools.collectors import board, index
    logger.info("采集板块轮动指数(沪深300 基准 + 申万一级行业)...")
    _old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(FETCH_TIMEOUT)
    try:
        idx = _safe("沪深300 基准指数", lambda: index.fetch_index(["沪深300"]))
        logger.info("基准指数:成功 %d", len(idx or {}))
        names = _safe("申万一级清单", lambda: [b["name"] for b in board.fetch_board_list()])
        if names:
            bk = _safe("申万一级行业指数", lambda: board.fetch_board_kline(names))
            logger.info("行业指数:成功 %d/%d", len(bk or {}), len(names))
        else:
            logger.warning("申万一级清单为空/失败,板块指数跳过(RRG 将弃权)")
    finally:
        socket.setdefaulttimeout(_old)


def collect_lhb(codes: list[str], as_of: str) -> None:
    """**收盘后**采当日龙虎榜(WI-6 Phase 3 · 风控微结构轴 T 落盘、T+1 生效)。

    数据源天然按**日**返回全市场(collectors.lhb.fetch_lhb 区间一次拉 → 分发到各票分区),
    这里拉 [as_of, as_of] 当日榜单、只落 codes 白名单票。**披露时点=盘后**:落 list_date=as_of,
    次日选股(as_of'=T+1)经 lhb_asof(list_date<as_of' 严格)自然生效 → 进入次日风控 veto 汇聚。
    健壮性:失败/限流/空 → WARNING 降级(collectors.lhb 自带优雅降级),绝不中止闭环。走统一超时。

    ⚠️ 定时触发(cron)由用户拍板接入,此处只提供采集步骤;on-demand 调用亦安全(幂等增量并集)。
    """
    if not codes:
        return
    logger.info("采集当日龙虎榜(风控微结构轴,%d 只白名单,日期 %s,盘后披露 T+1 生效)...", len(codes), as_of)
    _old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(FETCH_TIMEOUT)
    try:
        got = _safe("龙虎榜",
                    lambda: lhb_collector.fetch_lhb(start=as_of, end=as_of, codes=codes))
        logger.info("龙虎榜:当日上榜落盘 %d 只(白名单 %d)", len(got or {}), len(codes))
    finally:
        socket.setdefaulttimeout(_old)


def _update_lhb_scorecard(as_of: str) -> None:
    """龙虎榜轴前向观察记分卡:逐日滚存当日被龙虎榜否决降权的选股票 + 降权前后排名,
    K 线到期后自动回填 T+1/T+5 前向收益与"见光死"证实标记。真前向需滚存 ~15~20 交易日出首版结论。

    persist=False 复算全票排名,绝不覆盖闭环已落的 top-N「策略0合议」view(避免副作用)。
    仅观察、不参与选股决策;防未来函数:排名/综合分只用 as_of 及之前 K 线,前向收益仅事后回填。
    """
    from tools.backtest import lhb_forward_scorecard as fsc
    df = fsc.update(as_of)
    logger.info("龙虎榜前向记分卡:滚存 %s → 累计 %d 行 → %s", as_of, len(df), fsc._DEFAULT_OUT)


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
    # 富集前可观测:多少票拿不到真名、只能回退成代码(=本股新闻有被误判「无关」的风险)。
    nf = event.name_fallback_stats(codes)
    if nf["fallback"]:
        logger.warning("name_fallback_ratio=%.4f(%d/%d 票取不到真名,回退成代码):%s",
                       nf["ratio"], nf["fallback"], nf["total"], nf["fallback_codes"][:20])
    else:
        logger.info("name_fallback_ratio=0(%d 票全部取到真名)", nf["total"])
    try:
        store.put_view("name_fallback", nf)              # 落盘按日期视图,便于回溯/验收
    except Exception as e:
        logger.warning("name_fallback 视图落盘失败(不阻断):%s", str(e)[:80])
    ok = 0
    n = len(codes)
    fresh_stat = {"新鲜": 0, "陈旧": 0, "无数据": 0}          # 顶层新鲜度三态占比(验收观测点)
    layer_stat = {"新闻": dict(fresh_stat), "舆情": dict(fresh_stat), "政策": dict(fresh_stat)}
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] %s — 新闻情绪(LLM)...", i, n, code)
        try:
            rec = event.analyze_stock(code)
        except FileNotFoundError:
            continue
        ok += 1
        s = rec["sentiment"]
        if s.get("新鲜度") in fresh_stat:
            fresh_stat[s["新鲜度"]] += 1
        for lname, lstat in layer_stat.items():
            f = s.get("三层", {}).get(lname, {}).get("新鲜度")
            if f in lstat:
                lstat[f] += 1
        logger.info("  %s 净情绪 %s 新鲜度=%s(新闻%d/舆情%d/政策%d)", code, s["净情绪分"],
                    s.get("新鲜度"),
                    s["三层"]["新闻"]["样本数"], s["三层"]["舆情"].get("样本数", 0),
                    s["三层"]["政策"]["样本数"])
    logger.info("情绪打分完成:%d 只", ok)
    logger.info("  顶层新鲜度:新鲜%d/陈旧%d/无数据%d",
                fresh_stat["新鲜"], fresh_stat["陈旧"], fresh_stat["无数据"])
    for lname, lstat in layer_stat.items():
        logger.info("  %s层新鲜度:新鲜%d/陈旧%d/无数据%d",
                    lname, lstat["新鲜"], lstat["陈旧"], lstat["无数据"])
    # 同阶段生产「新闻+AI」统一视图(复用本阶段已建的 LLM 抽取缓存,不额外烧钱)
    from tools.analysis import news_ai
    logger.info("新闻 AI 视图:%d 只", news_ai.write_news_ai(codes))
    return ok


# ————————————————————————————————————————————————
# 财报(M2):数值三大表 + 年报PDF(无LLM)+ LLM文本层。只对子集,资源纪律。
# ————————————————————————————————————————————————
def run_financial_collect(codes: list[str]) -> None:
    """采财报三大表(数值层,无 LLM)→ 供 build_financial_block 算评级/红旗/闸门2。"""
    from tools.collectors import financial as fin
    out = _safe("财报三大表采集", lambda: fin.fetch_financial(codes)) or {}
    logger.info("财报三大表采集:%d 只", len(out))


def run_annual_report(codes: list[str]) -> None:
    """采年报 PDF → 抽审计/MD&A/风险段(无 LLM)→ 供闸门1 + LLM文本层。缺 pymupdf 自动降级。"""
    from tools.collectors import annual_report as ar
    out = _safe("年报PDF采集", lambda: ar.fetch_annual_report(codes)) or {}
    logger.info("年报PDF采集:%d 只", len(out))


def run_financial_text(codes: list[str], as_of: str) -> None:
    """财报 LLM 文本层(定性 schema_A + 归纳 schema_B)→ 落 code_view financial_text。缓存免重烧。"""
    from tools.analysis.financial import llm_text
    n = _safe("财报LLM文本层", lambda: llm_text.run_financial_text(codes, as_of)) or 0
    logger.info("财报LLM文本层:%d 只", n)


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


def run_backtest() -> None:
    """闭环收尾:跨多日累积的前瞻回测汇总(**可选增强**,失败绝不中止主闭环)。

    只**调用** tools.backtest.backtest_summary(不改其算法):读已累积的历次
    data/analysis/<date>/ 达标池,按防未来函数口径算前瞻 5/10/20 日收益 + Alpha +
    regime 分层胜率;前瞻期未到的窗口优雅标「待观察」,不报错。结果 run_and_store
    落成 store view(backtest.json + 形态选股回测汇总.json,写到最新达标日目录),
    随当日 analysis 产物被现有 upload 自动带到远端(为日后「策略体检卡」上页铺路)。

    幂等:run_and_store 用 put_view 覆盖同一天,重跑同一天覆盖当天回测结果、不重复累积。
    离线安全:fetch=False,只读已缓存 K线/基准(主闭环采集步已把当日数据落地),
    不给收尾步新增网络依赖;缺基准时 Alpha 优雅留空并带说明。
    """
    import datetime as _dt

    from tools.backtest import backtest_summary
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    r = backtest_summary.run_and_store(fetch=False, generated_at=stamp)
    logger.info("前瞻回测汇总:达标日数=%d 事件数=%d;结论:%s",
                r.get("达标日数", 0), r.get("样本数", 0), r.get("状态"))


# ————————————————————————————————————————————————
# 合议数据生产者(横截面/事件),必须在 serialize 之后、council 回写之前
# ————————————————————————————————————————————————
def _recent_quarter_ends(as_of: str, n: int = 2) -> list[str]:
    """as_of 往前最近 n 个季度末报告期 "YYYYMMDD"(供业绩预告/快报采集)。"""
    t = pd.to_datetime(as_of)
    ends = []
    for y in (t.year, t.year - 1):
        for md in ("1231", "0930", "0630", "0331"):
            d = pd.to_datetime(f"{y}{md}")
            if d <= t:
                ends.append(f"{y}{md}")
    return ends[:n]


def run_events(codes: list[str], as_of: str) -> None:
    """采集事件精数值(业绩预告/快报 + 增减持),供「事件驱动」专家。

    collectors 内部已全程 try/except 降级:东财被墙/限流/akshare 缺失 → 空,不炸流水;
    此时事件驱动专家回退 record['events'] 公告粗判(不弃权)。
    """
    from tools.collectors import event_driven as ed
    got = 0
    for period in _recent_quarter_ends(as_of, 2):
        for kind in ("yjyg", "yjkb"):
            df = ed.fetch_earnings_forecast(period, kind)
            got += 0 if df is None else len(df)
    df = ed.fetch_insider_trades("latest")
    got += 0 if df is None else len(df)
    # 董监高持股变动明细:自带「变动原因」→ 补 stock_ggcg_em 缺的「方式」(协议转让/集中竞价/大宗),
    # 供减持性质区分(战略引资 vs 二级抛售);load_insider_trades 会按 code+日期合并进「方式」列。
    mdf = ed.fetch_management_change("latest")
    got += 0 if mdf is None else len(mdf)
    logger.info("事件精数值采集:%d 行(降级则空,专家回退公告粗判)", got)


def run_factor(codes: list[str], as_of: str) -> None:
    """多因子截面打分预算(横截面,需全池已 serialize)→ code_view 'factor',供「多因子」专家。

    北向净流入趋势:best-effort 采集(源自 2024-08 停更 → 多为空 dict,资金流维度降级缺失 I4)。
    """
    from tools.analysis.factor import score
    from tools.collectors import northbound as nb
    北向 = nb.trend_map(codes, as_of=as_of)             # 停更/被墙→{},precompute 按缺失降级
    r = score.precompute(as_of=as_of, codes=codes, 北向=北向)
    logger.info("多因子截面预算:打分 %d/%d 只,北向可得 %d,因子可得性 %s",
                r.get("打分数"), r.get("扫描数"), len(北向), r.get("因子可得性"))


def run_council(codes: list[str], as_of: str) -> None:
    """横截面/事件数据就绪后,重算并回写各记录的 council 块(多因子/事件驱动不再弃权)。"""
    from tools.analysis import serialize
    n = serialize.reattach_council(codes, as_of)
    logger.info("合议块回写:%d 只(此时全专家数据就绪)", n)


def collect_ticks(codes: list[str]) -> None:
    """**收盘后**逐笔盘口微观结构归档(纳入 run all 的池级流程)。

    数据源通达信(mootdx),对票池拉当日逐笔 → 落 tick(明细)+ tick_summary(摘要)。
    ⚠️ 只在 run all 这种**票池级**流程带(池小、可控);**不进** collect_values,故
    pipeline/screenall 的**全A**路径不会被逐笔拖垮。
    ⚠️ 顺序命门:须排在 run_serialize **之前**——serialize 的 tick 块按 as_of date-pin
    读当日摘要,先采后组装才能进 record/前端卡片。
    非交易日/盘中/mootdx 不可用 → 单票空数据降级、整体 _safe,绝不中止闭环。
    单独回补/全池另用 `python -m tools.run ticks [--all|--date]`。
    """
    if not codes:
        return
    from tools.collectors import tdx_l2
    logger.info("逐笔盘口归档 %d 只(源 mootdx,盘后当日)...", len(codes))
    r = _safe("逐笔归档", lambda: tdx_l2.fetch_ticks(codes)) or {}
    logger.info("逐笔盘口归档:成功 %d/%d", len(r), len(codes))


def _chunks(seq: list, n: int):
    """把序列切成每 n 个一批(record 分批增量推用)。"""
    for i in range(0, len(seq), max(1, n)):
        yield seq[i:i + n]


def _push_incremental(as_of: str, shard_keys) -> None:
    """流式增量推(WI:模块化抗断点):best-effort 推指定分片
    (view 分片 key=`__view__:视图名`;record 分片 key=code)。每策略/每批 record 产出即推,
    任一失败不回滚已推的;末尾统一 upload 兜底补漏。走**同一 receipt_path** → 天然幂等
    (已成功分片不重传);网络/429 由 upload_date 内部退避处理。

    降级:总开关关(STREAM_PUSH=false)/无同步凭证(如直接 `run screenall` 未加载 sync.env)/
    空分片 → 静默跳过,绝不中止闭环(增量推是加速层,完整性由末尾兜底保证)。
    """
    from tools.config import settings
    if not settings.STREAM_PUSH or not shard_keys:
        return
    if not (settings.SYNC_INGEST_URL and settings.SYNC_INGEST_TOKEN and settings.SYNC_SIGNING_KEY):
        return                                    # 无凭证 → 跳过(不影响主流程,要推另跑 upload)
    try:
        from tools.sync import upload
        r = upload.upload_date(
            as_of, url=settings.SYNC_INGEST_URL, token=settings.SYNC_INGEST_TOKEN,
            source=settings.SYNC_SOURCE_ID, key_id=settings.SYNC_KEY_ID, key=settings.SYNC_SIGNING_KEY,
            receipt_path=upload._receipt_dir() / f"{as_of}.json", only_shards=set(shard_keys))
        s = r.get("summary", {}) if isinstance(r, dict) else {}
        logger.info("增量推 %d 分片 → 成功 %s / 失败 %s", len(shard_keys), s.get("ok"), s.get("failed"))
    except Exception as e:                          # noqa: BLE001 - 增量推 best-effort,末尾兜底补
        logger.warning("增量推失败(降级,末尾兜底补):%s", str(e)[:100])


# ————————————————————————————————————————————————
# CLI 命令(单步:各自设当天日期 + 开发池)
# ————————————————————————————————————————————————
def _prep(argv):
    as_of = _as_of()
    store.set_active_date(as_of)
    return _pool(argv), as_of


def cmd_ticks(argv):
    """盘后逐笔归档:收盘后拉票池当日逐笔成交,落 tick/<date>/<code>.parquet。

    python -m tools.run ticks              # 自选开发子集(默认)
    python -m tools.run ticks --all        # 全票池(逐笔量大,慎用)
    python -m tools.run ticks --date 20260827   # 回补历史某日(落该日分区)
    通达信(mootdx)源;港股跳过。单票失败降级不中断整批。
    """
    from tools.collectors import tdx_l2
    date = None
    if argv and "--date" in argv:
        i = argv.index("--date")
        date = argv[i + 1] if i + 1 < len(argv) and argv[i + 1].isdigit() else None
    # --date 回补:产物落该历史日分区(否则落今天)
    as_of = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if date and len(date) == 8 else _as_of()
    store.set_active_date(as_of)
    codes = _pool(argv)
    logger.info("逐笔归档 %d 只(日期 %s,源 mootdx)...", len(codes), as_of)
    r = _safe("逐笔归档", lambda: tdx_l2.fetch_ticks(codes, date=date)) or {}
    logger.info("逐笔归档:成功 %d/%d", len(r), len(codes))


def cmd_collect(argv): collect_values(_prep(argv)[0])
def cmd_message(argv): collect_message(_prep(argv)[0])
def cmd_sentiment(argv): run_sentiment(_prep(argv)[0])
def cmd_serialize(argv): codes, as_of = _prep(argv); run_serialize(codes, as_of)
def cmd_panel(argv): run_panel(_prep(argv)[0])
def cmd_screen(argv): run_screen(_prep(argv)[0])
def cmd_events(argv): codes, as_of = _prep(argv); run_events(codes, as_of)
def cmd_factor(argv): codes, as_of = _prep(argv); run_factor(codes, as_of)
def cmd_council(argv): codes, as_of = _prep(argv); run_council(codes, as_of)
def cmd_context(argv): store.set_active_date(_as_of()); collect_market_context()
def cmd_lhb(argv): codes, as_of = _prep(argv); collect_lhb(codes, as_of)   # 收盘后当日龙虎榜(风控轴,T+1 生效)


def cmd_pattern(argv):
    """形态选股扫描。默认开发子集;--universe [N] 从全 A 票池取前 N 只。"""
    from tools.pipeline import screen_pattern
    as_of = _as_of()
    store.set_active_date(as_of)
    if argv and "--universe" in argv:
        from tools.collectors import universe
        i = argv.index("--universe")
        n = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else 50
        codes = universe.universe_codes(limit=n)
        logger.info("票池:全 A 前 %d 只", len(codes))
    else:
        codes = _pool(argv)
    screen_pattern.run_pattern_screen(codes, as_of)


def cmd_sepa(argv):
    """SEPA+VCP 监控。默认会先 spot 增量当日 bar 再扫描;--no-fetch 只读本地主档。

    --universe N 截前 N 只;--session 午间|收盘。
    """
    from tools.pipeline import screen_sepa_vcp as sepa
    rest = argv[2:] if argv else []
    sepa._main(rest)


def cmd_strong(argv):
    """策略9 最强选股(Tushare 筹码 cyq_perf,傍晚才发布)。单独跑,供 20:00 补跑任务用。

    --no-fetch 只读本地主档(K线);--universe N 截前 N;--date 指定日。
    筹码当日未发布→写"需 Tushare"占位 view、不出;发布后补跑即出真结果。
    """
    from tools.pipeline import screen_strong
    rest = argv[2:] if argv else []
    screen_strong._main(rest)


def cmd_analyze(argv):
    """读缓存算技术指标,打印评级排行(不落盘)。"""
    codes, _ = _prep(argv)
    ranked = []
    for code in codes:
        try:
            r = ta.compute(market.load_kline_recent(code))
            if "signal" in r:
                ranked.append((code, r["signal"]))
        except FileNotFoundError:
            logger.warning("%s 无 K线缓存,跳过", code)
    ranked.sort(key=lambda kv: kv[1]["得分"], reverse=True)
    for code, s in ranked:
        logger.info("  %s %s 评级=%s(%d)", code, stock_pool.get(code).name, s["评级"], s["得分"])


def cmd_all(argv):
    """全链路:采集 → 情绪 → 组装 → 合议数据(横截面/事件)→ 合议回写 → 视图,全程同一日期。

    顺序命门(横截面依赖):serialize 先产 record;factor 截面打分与事件采集**读 record/全池**,
    故排在 serialize 之后;council 块含多因子/事件驱动专家 → 必须在这两个数据就绪**之后**回写,
    否则那两个专家因数据未就绪而弃权。panel/screen 读最终 record(含完整 council)。
    逐笔盘口(collect_ticks)在 serialize **之前**采(池级、盘后当日),使 record 的 tick 块能装上。
    """
    codes, as_of = _prep(argv)
    logger.info("===== 全链路开始(日期 %s,%d 只)=====", as_of, len(codes))
    collect_values(codes)
    collect_message(codes)
    collect_market_context()             # 全市场指数(沪深300+申万一级)→ 供板块轮动 RRG 专家
    collect_lhb(codes, as_of)            # 收盘后当日龙虎榜(风控微结构轴;T 落盘、T+1 生效)
    collect_ticks(codes)                 # 收盘后逐笔盘口归档(须在 serialize 前;serialize 的 tick 块读当日摘要)
    run_sentiment(codes)                 # LLM 未配置则内部跳过
    run_serialize(codes, as_of)          # 组装 record(首次 council:多因子/事件驱动此时弃权;挂 lhb_veto)
    run_events(codes, as_of)             # 事件精数值(降级不炸)→ 供事件驱动专家
    run_factor(codes, as_of)             # 多因子截面预算(读全池 record)→ 供多因子专家
    run_council(codes, as_of)            # 数据就绪后回写 council 块(全专家不再弃权)
    run_panel(codes)
    run_screen(codes)
    _safe("前瞻回测汇总", run_backtest)   # 收尾可选增强:跨多日累积回测(失败降级,绝不中止闭环)
    logger.info("===== 全链路完成 → data/analysis/%s/(record 含完整 council)=====", as_of)


# ————————————————————————————————————————————————
# 全A 两阶段流水线:先便宜筛(全A)、再只对候选做贵活(新闻/LLM/事件/合议)
# ————————————————————————————————————————————————
def _dedup(seq: list[str]) -> list[str]:
    """去重保序。"""
    s: set[str] = set()
    return [c for c in seq if not (c in s or s.add(c))]


def _enrich_near_miss(as_of: str) -> int:
    """council 后:回填「接近达标」各票的 `合议分`(读记录 council 综合分),并按合议分重排每板块 top3。

    只读记录 council 块(唯一权威合成产物)+ 重排展示顺序,**不改任何打分逻辑**。缺记录/缺 council
    的票 `合议分` 留 None(排末)。回写「形态选股」view。返回成功回填只数。
    """
    from tools.analysis import serialize
    view = store.get_view("形态选股", date=as_of)
    near = view.get("接近达标") or {}
    filled = 0
    for _sector, items in near.items():
        for x in items:
            try:
                rec = serialize.load_record(x["code"])
            except FileNotFoundError:
                continue
            s = ((rec.get("council") or {}).get("default") or {}).get("综合分")
            if s is not None:
                x["合议分"] = round(float(s), 4)
                filled += 1
        items.sort(key=lambda x: (x.get("合议分") is not None, x.get("合议分") or 0.0), reverse=True)
    store.put_view("形态选股", view)
    return filled


def run_two_stage(codes_all: list[str], as_of: str, no_llm: bool = False) -> dict:
    """全A 两阶段流水线:先便宜筛全A得达标池,再只对(达标∪自选)做贵活(新闻/LLM)。

    no_llm=True(数据-only 快速选股):跳过新闻采集 + LLM 情绪,全程纯数据运算(技术/拐点/资金流/
      多因子/板块轮动/形态 均无需大模型),情绪三层专家因无数据自然弃权。用于"快、不烧 token"的
      数据策略选股(用户口径:纯数据、无需大模型)。事件驱动走公告数值,仍是数据、保留。

    阶段①(可全A,便宜):形态选股两阶段扫描——内部全A 只采 K线 → 形态/RS/量能筛候选 →
      候选采护栏 → 达标池(达标清单带行业)+ 接近达标(平盘日区块②降级数据)。
    两个子集(命门:最贵的新闻/LLM 只对 llm_subset):
      · llm_subset = 达标 ∪ 自选:新闻采集 + LLM 情绪 只对这批(几十~数百);
      · analysis_set = 达标 ∪ 自选 ∪ 接近达标(展示 top3/板块):serialize/factor/council 对这批,
        让接近达标票拿到「技术/因子类」合议分——其情绪/事件专家因无新闻/LLM 数据**自然弃权**
        (council 已支持弃权,内核不改),故不额外烧 token。
    阶段②:补缺数值面(skip-if-cached)→ 新闻(llm_subset)→ LLM 情绪(llm_subset)→ 组装/事件/
      多因子/合议(analysis_set)→ 回填接近达标合议分并重排 → 视图。
    """
    from tools.pipeline import screen_pattern
    logger.info("===== 全A两阶段流水线开始(日期 %s,阶段①扫描 %d 只)=====", as_of, len(codes_all))
    # —— 阶段①前:K线主档同步(主档缺失/太旧→baostock 全量;否则 spot 当日增量;失败回退逐只)——
    # 同步后 screen_pattern 的 load-first 读主档命中,不再逐只重下(跨交易日不返工)。
    ms = _safe("K线主档同步", lambda: master_sync.sync_master(codes_all, as_of=as_of)) or {}
    logger.info("阶段①前 K线主档同步:模式=%s 成功 %d", ms.get("mode"), ms.get("ok", 0))
    # —— 阶段①:全A 便宜筛(只 K线)→ 达标池 + 接近达标 ——
    view = screen_pattern.run_pattern_screen(codes_all, as_of=as_of, fetch=True)
    qualified = [x["code"] for x in view.get("达标清单", [])]
    watch = stock_pool.get_codes()
    near_codes = [x["code"] for items in (view.get("接近达标") or {}).values() for x in items]

    llm_subset = _dedup(qualified + watch)                 # 新闻/LLM 只对这批(省 token 命门)
    analysis_set = _dedup(qualified + watch + near_codes)  # serialize/factor/council(含接近达标,无 LLM)
    logger.info("阶段①完成:全A %d / 有效 %s / 候选 %s / 达标 %d / 接近达标(展示)%d → "
                "LLM子集(达标∪自选)=%d,合议集(+接近达标)=%d",
                len(codes_all), view.get("有效样本"), view.get("候选数"),
                len(qualified), len(near_codes), len(llm_subset), len(analysis_set))
    # —— 阶段②:新闻/LLM 只对 llm_subset;组装/合议 对 analysis_set(接近达标获技术/因子类合议分)——
    collect_values_missing(analysis_set)  # 补 K线/基本面/公告/资金流(无 LLM,skip-if-cached)
    collect_lhb(analysis_set, as_of)      # 收盘后当日龙虎榜(风控微结构轴;T 落盘、T+1 生效)
    if not no_llm:
        collect_message(llm_subset)       # 新闻/舆情 只对达标∪自选 ← 关键省 token
    collect_market_context()              # 全市场指数(每轮一次、非逐票)→ RRG 专家
    if no_llm:
        logger.info("数据-only 模式:跳过新闻采集 + LLM 情绪(情绪三层专家将弃权)")
    else:
        run_sentiment(llm_subset)         # LLM 情绪 只对达标∪自选 ← 关键省 token
    collect_ticks(analysis_set)           # 逐笔盘口归档(须在 serialize 前;票池级、盘后当日)→ record tick 块
    run_serialize(analysis_set, as_of)    # 记录含接近达标(其情绪为空,情绪专家弃权)
    run_events(analysis_set, as_of)
    run_factor(analysis_set, as_of)
    run_council(analysis_set, as_of)      # 接近达标获合议分(情绪/事件专家自然弃权)
    for _b in _chunks(analysis_set, settings.STREAM_RECORD_BATCH):   # 流式增量推 record(对称 run_screen_all)
        _push_incremental(as_of, set(_b))
    filled = _enrich_near_miss(as_of)     # 回填接近达标合议分 + 按合议分重排 top3
    run_panel(analysis_set)
    run_screen(analysis_set)
    _safe("前瞻回测汇总", run_backtest)   # 收尾可选增强:跨多日累积回测(失败降级,绝不中止闭环)
    logger.info("===== 全A两阶段流水线完成 → data/analysis/%s/;LLM子集 %d 只含完整合议,"
                "接近达标合议分回填 %d 只 =====", as_of, len(llm_subset), filled)
    return {"as_of": as_of, "扫描": len(codes_all), "达标": len(qualified),
            "接近达标": len(near_codes), "LLM子集": len(llm_subset), "合议集": len(analysis_set),
            "llm_subset": llm_subset, "analysis_set": analysis_set}


def cmd_pipeline(argv):
    """全A 两阶段流水线入口:python -m tools.run pipeline [--universe N]。

    默认全A(不传 --universe);--universe N 取全A前 N 只做阶段①(小规模验证/试跑)。
    """
    as_of = _as_of()
    store.set_active_date(as_of)
    from tools.collectors import universe
    n = None
    if argv and "--universe" in argv:
        i = argv.index("--universe")
        n = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else None
    no_llm = bool(argv and "--no-llm" in argv)
    codes_all = universe.universe_codes(limit=n)
    logger.info("两阶段流水线票池(阶段①):全A%s共 %d 只%s",
                f"前{n}只" if n else "全量", len(codes_all), "(数据-only)" if no_llm else "")
    run_two_stage(codes_all, as_of, no_llm=no_llm)


# ————————————————————————————————————————————————
# 全A 多策略选股 + 只对选出票做新闻/LLM(screenall)
# ————————————————————————————————————————————————
def _picks_from_view(view: dict | None) -> list[str]:
    """从单个 screener view 抽出选出票 code。

    兼容三种落法:
      - 规则型 screener 落 `入选清单`([{code,...}]);
      - 打分型(策略0)落 `top`([{code,...}]);
      - 排行型(策略11·指标条件化状态排序)落 `排行`({维度→[{code,...}]}),
        无 `入选清单`/`top`——从各维度榜取 code,并集去重保序。
    只在前两者都缺时才走 `排行` 分支,保证原有两种 view 行为完全不变。
    view 为 None(screener 被 _safe 隔离失败)/字段缺失 → 返回空(不中止汇总)。
    """
    if not isinstance(view, dict):
        return []
    items = view.get("入选清单") or view.get("top")
    if items:
        return [x["code"] for x in items
                if isinstance(x, dict) and x.get("code")]
    # 排行型:各维度榜(dict of 维度→list)取 code,并集去重保序;元素可为 dict 或 code 串
    rank = view.get("排行")
    if isinstance(rank, dict):
        codes: list[str] = []
        for lst in rank.values():
            if not isinstance(lst, list):
                continue
            for x in lst:
                code = x.get("code") if isinstance(x, dict) else x
                if isinstance(code, str) and code:
                    codes.append(code)
        return _dedup(codes)
    return []


def run_screen_all(codes_all: list[str], as_of: str, no_llm: bool = False,
                   no_fetch: bool = False) -> dict:
    """全A 多策略选股 → 只对(各策略选出并集 ∪ 自选)做新闻/LLM/合议。

    与 run_two_stage(单形态达标池)的区别:达标池 = 各在产全A screener 选出票的并集,
    把新闻/LLM 覆盖面扩到所有策略选出的票,而非仅形态。省 token 命门同 two_stage:
    最贵的新闻/LLM 只对 llm_subset(选出并集 ∪ 自选),不对全A codes_all。

    no_llm=True(数据-only 快跑):跳过新闻采集 + LLM 情绪,情绪三层专家因无数据自然弃权。

    流程:
      ①  set_active_date + K线主档同步(主档缺失/太旧→baostock 全量;否则 spot 增量;失败回退逐只)。
      ②  跑各在产全A screener(council/s02/momentum/半导体/S03/S04/最强/反转/条件化;
          S01 趋势深跌反包 与 箱体3 已因显著负下线摘除),均 fetch=False 读步骤①主档,不重采;
          各 _safe 隔离——单个 screener 失败降级跳过,不中止其余。
      ③  union_picks = 各 screener picks 并集(去重保序)。
      ④  llm_subset = union_picks ∪ 自选池(去重)——新闻/LLM 只对这批。
      ⑤  补缺数值面(skip-if-cached)→ 新闻(no_llm 跳过)→ 板块指数 → LLM 情绪(no_llm 跳过)→
          组装/事件/多因子/合议 → 横表/选股视图,全对 llm_subset。
    """
    # 注:screen_s01(趋势深跌反包)/ screen_box(箱体3)已因全史深诊断显著负下线,
    # 不再进本编排(代码存档保留,见各文件顶部说明)。
    from tools.pipeline import (screen_conditional_rank, screen_council,
                                 screen_max_range, screen_momentum, screen_reversal_turnover,
                                 screen_s02, screen_semi_factor, screen_strong, screen_volume)
    store.set_active_date(as_of)
    logger.info("===== 全A多策略选股开始(日期 %s,全A %d 只)%s=====",
                as_of, len(codes_all), "(数据-only)" if no_llm else "")
    # —— 步骤①:K线主档同步(同 run_two_stage 开头)——各 screener 随后 fetch=False 读主档,不重采 ——
    # no_fetch=True:调用方已备好数据(本地已有全A raw/主档,或已 pull),跳过同步,避免主档覆盖低时
    # 误触发 baostock 全量回填(全A 数据常在 raw 分区,load_kline 主档缺失会回退 raw)。
    if no_fetch:
        logger.info("K线主档同步:跳过(no_fetch,直接用现有 raw/主档)")
    else:
        ms = _safe("K线主档同步", lambda: master_sync.sync_master(codes_all, as_of=as_of)) or {}
        logger.info("K线主档同步:模式=%s 成功 %d", ms.get("mode"), ms.get("ok", 0))

    # —— 步骤②:跑各在产全A screener(fetch=False 只读主档);_safe 逐个隔离,单个失败不中止其余 ——
    # 策略1·趋势深跌反包(S01)与 策略3·箱体形态(箱体3)已因显著负下线,不再编排(存档见 screen_s01/screen_box)。
    screeners = [
        ("策略0·多专家合议", lambda: screen_council.run_council_screen(codes_all, as_of=as_of, fetch=False)),
        ("策略2·放量后缩量回踩", lambda: screen_s02.run_s02_screen(codes_all, as_of=as_of, fetch=False)),
        ("策略4·动量组合", lambda: screen_momentum.run_momentum_screen(codes_all, as_of=as_of, fetch=False)),
        # 策略5·半导体多因子:限半导体池 178 只,财报三大表/fundamental 需**触网补采**
        # (fetch=True),因为主闭环采数值面 collect_values 只对 llm_subset,半导体池票
        # 大概率不在里面,需 pipeline 自采后才能算 3 因子。
        ("策略5·半导体多因子", lambda: screen_semi_factor.run_semi_factor_screen(
            codes_all, as_of=as_of, fetch=True)),
        # PR#15 提取:S03 最大范围 / S04 量价放量(纯 OHLCV,fetch=False 只读主档)
        ("S03·最大范围选股", lambda: screen_max_range.run_max_range_screen(codes_all, as_of=as_of, fetch=False)),
        ("S04·量价放量", lambda: screen_volume.run_volume_screen(codes_all, as_of=as_of, fetch=False)),
        # S05 最强:硬依赖 Tushare 筹码(cyq_perf),run_strong_screen 自门控——未配 token / 取不到 → 写"需 Tushare"占位 view、不出选股
        ("S05·最强选股", lambda: screen_strong.run_strong_screen(codes_all, as_of=as_of, fetch=False)),
        # 策略10·反转低换手组合(候选·前向观测中):纯量价横截面复合(rev5+turn20),fetch=False 只读主档。
        # 诚实边界见 docs/策略/策略总览:限可交易池+5-10日+TopK≤20;net 绝对水平存幸存者水分,以前向观测为准。
        ("策略10·反转低换手组合", lambda: screen_reversal_turnover.run_reversal_turnover_screen(
            codes_all, as_of=as_of, fetch=False)),
        # 策略11·指标条件化状态排序(状态参考·非alpha):按当日指标状态相似的历史上涨概率排 Top10(1/5/10日),
        # 成交额破同状态格并列。⚠️需先建 state_pool(数据线在 remote_fetch/主档同步 后 build_state_pool);
        # 缺池则优雅出空。fetch=False 只读主档。回测聚合无超额、1日弱区分/5-10日近噪声,页面已诚实标注。
        ("策略11·指标条件化状态排序", lambda: screen_conditional_rank.run_conditional_rank_screen(
            codes_all, as_of=as_of, fetch=False)),
    ]
    news_topk = int(os.getenv("SCREENALL_NEWS_TOPK", "5"))  # 新闻/情绪 LLM 每策略只取前 N(省 token、去边缘票噪声)
    # label → 该 screener 落盘的 view 名(供流式增量推构造分片 key `__view__:视图名`)
    _screener_view = {
        "策略0·多专家合议": "策略0合议", "策略2·放量后缩量回踩": "放量后缩量回踩",
        "策略4·动量组合": "动量组合", "策略5·半导体多因子": "半导体多因子",
        "S03·最大范围选股": "最大范围选股", "S04·量价放量": "量价放量",
        "S05·最强选股": "最强选股", "策略10·反转低换手组合": "反转低换手组合",
        "策略11·指标条件化状态排序": "指标条件化状态排序",
    }
    from tools.sync.upload import VIEW_SHARD_PREFIX as _VPREFIX
    union: list[str] = []
    news_union: list[str] = []
    per_strategy: dict[str, int] = {}
    for label, fn in screeners:
        view = _safe(f"{label} 全A筛选", fn)
        picks = _picks_from_view(view)
        per_strategy[label] = len(picks)
        union.extend(picks)
        news_union.extend(picks[:news_topk])        # 新闻/LLM 情绪 只取每策略前 N(排序型=前N强/规则型=前N只)
        logger.info("  %s 入选 %d(新闻取前%d)", label, len(picks), min(len(picks), news_topk))
        # 流式增量推:该策略 view 落盘后立即推(抗断点——某策略/网络失败不影响已推的;末尾兜底补漏)
        vname = _screener_view.get(label)
        if view is not None and vname:
            _push_incremental(as_of, {f"{_VPREFIX}{vname}"})

    union_picks = _dedup(union)                     # 各策略选出票并集(去重保序)
    watch = stock_pool.get_codes()
    llm_subset = _dedup(union_picks + watch)         # 数值/serialize/因子/合议/横表 对这批(无 LLM,覆盖全并集,记录/网页不缩)
    news_subset = _dedup(news_union + watch)         # ⭐ 新闻采集 + LLM 情绪 只对 自选∪每策略前N —— 省 token 命门
    logger.info("各策略入选:%s;union=%d,分析集(∪自选)=%d,LLM新闻子集(自选∪每策略前%d)=%d",
                per_strategy, len(union_picks), len(llm_subset), news_topk, len(news_subset))

    # —— 阶段②:新闻/LLM 只对 llm_subset ——
    collect_values_missing(llm_subset)               # 补 K线/基本面/公告/资金流(无 LLM,skip-if-cached)
    if not no_llm:
        collect_message(news_subset)                 # ⭐ 新闻/舆情 只对 自选∪每策略前N ← 关键省 token
    collect_market_context()                         # 全市场指数(每轮一次、非逐票)→ RRG 专家
    if no_llm:
        logger.info("数据-only 模式:跳过新闻采集 + LLM 情绪(情绪三层专家将弃权)")
    else:
        run_sentiment(news_subset)                   # ⭐ LLM 情绪 只对 自选∪每策略前N ← 关键省 token
    # —— 财报(M2):三大表+年报PDF(无LLM)+ LLM文本,只对 news_subset(自选∪每策略前N,资源纪律)——
    #    须在 serialize 前:serialize 的 build_financial_block 读这批已采数据+文本视图。
    run_financial_collect(news_subset)               # 三大表(数值层,无 LLM)
    run_annual_report(news_subset)                   # 年报 PDF 抽段(无 LLM,缺 pymupdf 降级)
    if not no_llm:
        run_financial_text(news_subset, as_of)       # 财报 LLM 文本层(定性+归纳,缓存)
    # 逐笔盘口归档(collect_ticks):须在 serialize 前——serialize 的 tick 块按 as_of date-pin 读当日摘要,
    # 先采后组装才进 record/个股页卡片。只对 llm_subset(选出并集∪自选,票池级、量可控),
    # 不进全A codes_all(逐笔量大)——与合作者「票池级、不进全A」设计一致,让逐笔在生产 screenall 闭环自动采。
    collect_ticks(llm_subset)
    run_serialize(llm_subset, as_of)
    run_events(llm_subset, as_of)
    run_factor(llm_subset, as_of)
    run_council(llm_subset, as_of)
    # 流式增量推:record 含完整 council 后按批推(分片 key=code;抗断点——某批/网络失败不影响其余,末尾兜底补漏)
    for _b in _chunks(llm_subset, settings.STREAM_RECORD_BATCH):
        _push_incremental(as_of, set(_b))
    run_panel(llm_subset)
    run_screen(llm_subset)
    _safe("前瞻回测汇总", run_backtest)              # 收尾可选增强(失败降级,不中止闭环)
    # —— 风控微结构轴:收盘后采当日龙虎榜(T 落盘、list_date<as_of 次日生效)。
    #    命门:生产日常走 screenall→run_screen_all,故龙虎榜的**实际每日采集入口在此**
    #    (cmd_all/run_two_stage 也接了,但那两条非日常触发路径)。collect_lhb 自带优雅降级不中止闭环。
    collect_lhb(codes_all, as_of)
    # —— 前向观察:龙虎榜轴命中票逐日滚存记分卡(降权前后排名 + K线到期自动回填 T+1/T+5 与见光死标记)。
    #    persist=False 复算全票排名不覆盖闭环已落 view;仅观察、不影响选股;失败降级不中止。
    _safe("龙虎榜前向记分卡", lambda: _update_lhb_scorecard(as_of))
    logger.info("===== 全A多策略选股完成 → data/analysis/%s/;union=%d,llm_subset=%d 只含完整合议 =====",
                as_of, len(union_picks), len(llm_subset))
    return {"as_of": as_of, "扫描": len(codes_all), "各策略入选": per_strategy,
            "union": len(union_picks), "llm_subset": len(llm_subset),
            "union_picks": union_picks, "llm_subset_codes": llm_subset}


def cmd_screenall(argv):
    """全A 多策略选股入口:python -m tools.run screenall [--universe N] [--no-llm]。

    默认全A(不传 --universe);--universe N 取全A前 N 只做筛选(小规模验证/试跑)。
    --no-llm:纯数据模式,跳过新闻采集 + LLM 情绪(不烧 token 的快跑)。
    """
    as_of = _as_of()
    store.set_active_date(as_of)
    from tools.collectors import universe
    n = None
    if argv and "--universe" in argv:
        i = argv.index("--universe")
        n = int(argv[i + 1]) if i + 1 < len(argv) and argv[i + 1].isdigit() else None
    no_llm = bool(argv and "--no-llm" in argv)
    no_fetch = bool(argv and "--no-fetch" in argv)
    codes_all = universe.universe_codes(limit=n)
    logger.info("全A多策略选股票池:全A%s共 %d 只%s%s",
                f"前{n}只" if n else "全量", len(codes_all),
                "(数据-only)" if no_llm else "", "(no-fetch)" if no_fetch else "")
    run_screen_all(codes_all, as_of, no_llm=no_llm, no_fetch=no_fetch)


def cmd_findata(argv):
    """全A 财报三大表增量回填入口:python -m tools.run findata [--universe N] [--force] [--dry-run]。

    把财报采集从"仅 picks(约 175 票)"扩到全A,让离线全A 策略0 也拿到 as_of 财报块
    → 财报质地专家不弃权、红旗判定在全A排序生效。重操作(全量约 3~6 小时),建议后台长跑;
    幂等 + 断点续采(新鲜票自动跳过),可反复跑。--dry-run 先估规模/耗时。
    透传参数见 collectors.financial_backfill._main(--max-age-days/--chunk/--codes/--date/--include-bj)。
    """
    from tools.collectors import financial_backfill
    financial_backfill._main(argv[2:])   # argv[0]=脚本 argv[1]=findata,其余透传


_CMDS = {"collect": cmd_collect, "message": cmd_message, "sentiment": cmd_sentiment,
         "serialize": cmd_serialize, "panel": cmd_panel, "screen": cmd_screen,
         "events": cmd_events, "factor": cmd_factor, "council": cmd_council,
         "context": cmd_context, "lhb": cmd_lhb, "pipeline": cmd_pipeline,
         "screenall": cmd_screenall,
         "pattern": cmd_pattern, "sepa": cmd_sepa, "strong": cmd_strong,
         "analyze": cmd_analyze, "findata": cmd_findata, "all": cmd_all,
         "ticks": cmd_ticks}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _CMDS:
        print(f"用法: python -m tools.run [{'|'.join(_CMDS)}] [--all]")
        return 1
    _CMDS[argv[1]](argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
