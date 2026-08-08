"""编排入口:采集 → 情绪 → 组装 → 视图(按日期存储,开发期默认 10 只)。

用法:
    python -m tools.run collect      # 采集数值面(K线/基本面/公告/资金流)
    python -m tools.run message      # 采集消息面(新闻/舆情/政策)
    python -m tools.run context      # 全市场指数(沪深300+申万一级行业)→ 供板块轮动 RRG 专家
    python -m tools.run sentiment    # LLM 三层情绪打分(需 LLM 配置)
    python -m tools.run serialize    # 组装中心记录 + K线图表视图(读情绪并入决策)
    python -m tools.run events       # 采集事件精数值(业绩预告/快报+增减持),供事件驱动专家
    python -m tools.run factor       # 多因子截面打分预算(全池横截面)→ code_view,供多因子专家
    python -m tools.run council      # 横截面/事件就绪后回写各 record 的 council 块
    python -m tools.run panel        # 横向总表视图
    python -m tools.run screen       # 组合聚合 + 预设选股视图
    python -m tools.run pattern      # 形态选股(模块二)扫描:RS+硬规则AND+达标占比
    python -m tools.run all          # 全链路(采集→情绪→组装→事件→多因子→合议回写→视图),一个日期
    python -m tools.run pipeline     # 全A 两阶段流水线:全A便宜筛得达标池,再只对(达标∪自选)做新闻/LLM/合议
    # 追加 --all 用全池 32 只;默认开发子集 10 只(config/dev_sample.json)
    # pattern 额外支持 --universe [N]:从全 A 票池取前 N 只(默认 50)扫描
    # pipeline 额外支持 --universe [N]:阶段①只扫全A前 N 只(默认全量);贵活只对候选(达标∪自选)

按日期:编排开始 store.set_active_date(今天),本次所有产出落 data/<日期>/。
"""
import logging
import os
import socket
import sys

import pandas as pd

from tools.analysis import technical as ta
from tools.collectors import announcement as an
from tools.collectors import fundamental as fd
from tools.collectors import fundflow as ff
from tools.collectors import market
from tools.collectors import master_sync
from tools.collectors import news, policy, ugc
from tools.config import stock_pool
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
    logger.info("数值面补缺(%d 候选,已缓存跳过):K线 %d / 基本面 %d / 公告 %d / 资金流 %d",
                len(codes), len(need_k), len(need_f), len(need_a), len(need_ff))
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
    finally:
        socket.setdefaulttimeout(_old)


def collect_message(codes: list[str]) -> None:
    """采集消息面:每个源各自 try/except 降级,任一源(含政策)失败都不中止整批。"""
    logger.info("采集消息面 %d 只(新闻/舆情/政策)...", len(codes))
    _old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(FETCH_TIMEOUT)
    try:
        logger.info("新闻:成功 %d", len(_safe("新闻", lambda: news.fetch_news(codes)) or {}))
        logger.info("舆情(股吧):成功 %d", len(_safe("舆情(股吧)", lambda: ugc.fetch_ugc(codes)) or {}))
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
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] %s — 新闻情绪(LLM)...", i, n, code)
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
    logger.info("事件精数值采集:%d 行(降级则空,专家回退公告粗判)", got)


def run_factor(codes: list[str], as_of: str) -> None:
    """多因子截面打分预算(横截面,需全池已 serialize)→ code_view 'factor',供「多因子」专家。"""
    from tools.analysis.factor import score
    r = score.precompute(as_of=as_of, codes=codes)
    logger.info("多因子截面预算:打分 %d/%d 只,因子可得性 %s",
                r.get("打分数"), r.get("扫描数"), r.get("因子可得性"))


def run_council(codes: list[str], as_of: str) -> None:
    """横截面/事件数据就绪后,重算并回写各记录的 council 块(多因子/事件驱动不再弃权)。"""
    from tools.analysis import serialize
    n = serialize.reattach_council(codes, as_of)
    logger.info("合议块回写:%d 只(此时全专家数据就绪)", n)


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
def cmd_events(argv): codes, as_of = _prep(argv); run_events(codes, as_of)
def cmd_factor(argv): codes, as_of = _prep(argv); run_factor(codes, as_of)
def cmd_council(argv): codes, as_of = _prep(argv); run_council(codes, as_of)
def cmd_context(argv): store.set_active_date(_as_of()); collect_market_context()


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
    """全链路:采集 → 情绪 → 组装 → 合议数据(横截面/事件)→ 合议回写 → 视图,全程同一日期。

    顺序命门(横截面依赖):serialize 先产 record;factor 截面打分与事件采集**读 record/全池**,
    故排在 serialize 之后;council 块含多因子/事件驱动专家 → 必须在这两个数据就绪**之后**回写,
    否则那两个专家因数据未就绪而弃权。panel/screen 读最终 record(含完整 council)。
    """
    codes, as_of = _prep(argv)
    logger.info("===== 全链路开始(日期 %s,%d 只)=====", as_of, len(codes))
    collect_values(codes)
    collect_message(codes)
    collect_market_context()             # 全市场指数(沪深300+申万一级)→ 供板块轮动 RRG 专家
    run_sentiment(codes)                 # LLM 未配置则内部跳过
    run_serialize(codes, as_of)          # 组装 record(首次 council:多因子/事件驱动此时弃权)
    run_events(codes, as_of)             # 事件精数值(降级不炸)→ 供事件驱动专家
    run_factor(codes, as_of)             # 多因子截面预算(读全池 record)→ 供多因子专家
    run_council(codes, as_of)            # 数据就绪后回写 council 块(全专家不再弃权)
    run_panel(codes)
    run_screen(codes)
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
    if not no_llm:
        collect_message(llm_subset)       # 新闻/舆情 只对达标∪自选 ← 关键省 token
    collect_market_context()              # 全市场指数(每轮一次、非逐票)→ RRG 专家
    if no_llm:
        logger.info("数据-only 模式:跳过新闻采集 + LLM 情绪(情绪三层专家将弃权)")
    else:
        run_sentiment(llm_subset)         # LLM 情绪 只对达标∪自选 ← 关键省 token
    run_serialize(analysis_set, as_of)    # 记录含接近达标(其情绪为空,情绪专家弃权)
    run_events(analysis_set, as_of)
    run_factor(analysis_set, as_of)
    run_council(analysis_set, as_of)      # 接近达标获合议分(情绪/事件专家自然弃权)
    filled = _enrich_near_miss(as_of)     # 回填接近达标合议分 + 按合议分重排 top3
    run_panel(analysis_set)
    run_screen(analysis_set)
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


_CMDS = {"collect": cmd_collect, "message": cmd_message, "sentiment": cmd_sentiment,
         "serialize": cmd_serialize, "panel": cmd_panel, "screen": cmd_screen,
         "events": cmd_events, "factor": cmd_factor, "council": cmd_council,
         "context": cmd_context, "pipeline": cmd_pipeline,
         "pattern": cmd_pattern, "analyze": cmd_analyze, "all": cmd_all}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in _CMDS:
        print(f"用法: python -m tools.run [{'|'.join(_CMDS)}] [--all]")
        return 1
    _CMDS[argv[1]](argv)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
